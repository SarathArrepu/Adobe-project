# ---- Pipeline Module ----
#
# Reusable module. Creates one complete, isolated pipeline per data source.
# Shared infrastructure (S3, KMS, IAM admin/dev roles) lives in terraform/shared.tf.
# Each pipeline is wired up in terraform/pipelines.tf by calling this module.
#
# To add a NEW source:
#   1. Create src/pipelines/<source>/__init__.py  (empty)
#   2. Create src/pipelines/<source>/handler.py   (copy adobe handler, update transformation logic)
#   3. In terraform/pipelines.tf, add a module "<source>_pipeline" block
#      (copy the adobe_pipeline block, update source_name + lambda_handler + columns)
#   4. terraform apply  →  Lambda, Glue tables, EventBridge rule all created automatically.
#
# Resources created per source:
#   - IAM role (Lambda execution, least-privilege)
#   - Lambda function
#   - Lambda permission (allow EventBridge to invoke)
#   - EventBridge rule + target (S3 landing/{source}/ → this Lambda)
#   - CloudWatch log group + error alarm
#   - Glue tables: {source}_bronze_masked, {source}_bronze_raw, {source}_gold
#   - Glue Crawler (daily schema evolution)

locals {
  name_prefix    = "${var.project_name}-${var.source_name}-${var.environment}"
  landing_prefix = "landing/${var.source_name}/"
  glue_db        = var.glue_database_name
}

# ---- IAM: Lambda execution role (one per source) ----

resource "aws_iam_role" "lambda_role" {
  name = "${var.project_name}-lambda-${var.source_name}-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "lambda_s3_kms" {
  name = "s3-kms-access"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadLanding"
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${var.s3_bucket_arn}/landing/${var.source_name}/*",
          "${var.s3_bucket_arn}/landing/*",
        ]
      },
      {
        Sid    = "WriteBronzeGold"
        Effect = "Allow"
        Action = ["s3:PutObject"]
        Resource = [
          "${var.s3_bucket_arn}/bronze/raw/${var.source_name}/*",
          "${var.s3_bucket_arn}/bronze/masked/${var.source_name}/*",
          "${var.s3_bucket_arn}/gold/${var.source_name}/*",
          # Support flat key names (legacy: landing/data.sql → gold/data.tab)
          "${var.s3_bucket_arn}/bronze/raw/*",
          "${var.s3_bucket_arn}/bronze/masked/*",
          "${var.s3_bucket_arn}/gold/*",
        ]
      },
      {
        Sid      = "StandardKms"
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = var.kms_key_arn
      },
      {
        Sid      = "PiiKmsEncryptOnly"
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = var.pii_kms_key_arn
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ---- Lambda function ----

resource "aws_lambda_function" "processor" {
  function_name    = local.name_prefix
  role             = aws_iam_role.lambda_role.arn
  handler          = var.lambda_handler
  runtime          = "python3.12"
  timeout          = var.lambda_timeout_seconds
  memory_size      = var.lambda_memory_mb
  filename         = var.lambda_zip_path
  source_code_hash = var.lambda_zip_hash

  environment {
    variables = {
      ENVIRONMENT     = var.environment
      SOURCE_NAME     = var.source_name
      LOG_LEVEL       = "INFO"
      KMS_KEY_ARN     = var.kms_key_arn
      PII_KMS_KEY_ARN = var.pii_kms_key_arn
    }
  }
}

# Allow EventBridge to invoke this Lambda
resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.processor.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.landing_upload.arn
}

# ---- EventBridge: route landing/{source}/ uploads to this Lambda ----
# S3 bucket must have EventBridge notifications enabled (set once in root module).

resource "aws_cloudwatch_event_rule" "landing_upload" {
  name        = "${local.name_prefix}-landing-upload"
  description = "Routes s3://${var.s3_bucket_id}/landing/${var.source_name}/ uploads to the ${var.source_name} Lambda"

  event_pattern = jsonencode({
    source      = ["aws.s3"]
    detail-type = ["Object Created"]
    detail = {
      bucket = { name = [var.s3_bucket_id] }
      object = { key = [{ prefix = local.landing_prefix }] }
    }
  })
}

resource "aws_cloudwatch_event_target" "invoke_lambda" {
  rule = aws_cloudwatch_event_rule.landing_upload.name
  arn  = aws_lambda_function.processor.arn

  input_transformer {
    input_paths = {
      bucket = "$.detail.bucket.name"
      key    = "$.detail.object.key"
    }
    # Wrap into the Records format Lambda handler expects
    input_template = <<-EOT
      {
        "Records": [{
          "s3": {
            "bucket": { "name": "<bucket>" },
            "object": { "key": "<key>" }
          }
        }]
      }
    EOT
  }
}

# ---- CloudWatch: logs + error alarm ----

resource "aws_cloudwatch_log_group" "lambda_logs" {
  name              = "/aws/lambda/${aws_lambda_function.processor.function_name}"
  retention_in_days = 14
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "${local.name_prefix}-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Lambda errors detected in ${var.source_name} pipeline"

  dimensions = {
    FunctionName = aws_lambda_function.processor.function_name
  }
}

# ---- Glue tables ----
# Three tables per source:
#   {source}_bronze_masked — SHA-256 hashed PII, developer accessible
#   {source}_bronze_raw    — plaintext PII, admin/PII KMS key only
#   {source}_gold          — aggregated output, no PII

locals {
  serde_params = {
    "field.delim"            = "\t"
    "serialization.format"   = "\t"
    "skip.header.line.count" = "1"
  }
  input_format  = "org.apache.hadoop.mapred.TextInputFormat"
  output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"
  serde_lib     = "org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe"
}

resource "aws_glue_catalog_table" "bronze_masked" {
  name          = "${var.source_name}_bronze_masked"
  database_name = local.glue_db
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    "EXTERNAL"       = "TRUE"
    "classification" = "tsv"
    "pii_handling"   = "pseudonymized-sha256"
  }

  storage_descriptor {
    location      = "s3://${var.s3_bucket_id}/bronze/masked/"
    input_format  = local.input_format
    output_format = local.output_format

    ser_de_info {
      serialization_library = local.serde_lib
      parameters            = local.serde_params
    }

    dynamic "columns" {
      for_each = var.bronze_columns
      content {
        name    = columns.value.name
        type    = columns.value.type
        comment = columns.value.comment
      }
    }
  }
}

resource "aws_glue_catalog_table" "bronze_raw" {
  name          = "${var.source_name}_bronze_raw"
  database_name = local.glue_db
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    "EXTERNAL"            = "TRUE"
    "classification"      = "tsv"
    "data_classification" = "restricted-pii"
    "pii_handling"        = "plaintext-pii-kms-encrypted"
  }

  storage_descriptor {
    location      = "s3://${var.s3_bucket_id}/bronze/raw/"
    input_format  = local.input_format
    output_format = local.output_format

    ser_de_info {
      serialization_library = local.serde_lib
      parameters            = local.serde_params
    }

    dynamic "columns" {
      for_each = var.bronze_columns
      content {
        name    = columns.value.name
        type    = columns.value.type
        comment = columns.value.comment
      }
    }
  }
}

resource "aws_glue_catalog_table" "gold" {
  name          = "${var.source_name}_gold"
  database_name = local.glue_db
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    "EXTERNAL"       = "TRUE"
    "classification" = "tsv"
  }

  storage_descriptor {
    location      = "s3://${var.s3_bucket_id}/gold/"
    input_format  = local.input_format
    output_format = local.output_format

    ser_de_info {
      serialization_library = local.serde_lib
      parameters            = local.serde_params
    }

    dynamic "columns" {
      for_each = var.gold_columns
      content {
        name    = columns.value.name
        type    = columns.value.type
        comment = columns.value.comment
      }
    }
  }
}

# ---- Glue Crawler (daily schema evolution) ----
# Crawls masked bronze + gold. Skips raw (PII KMS key, same schema as masked).

resource "aws_iam_role" "glue_crawler_role" {
  name = "${var.project_name}-crawler-${var.source_name}-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "glue.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "glue_service" {
  role       = aws_iam_role.glue_crawler_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
}

resource "aws_iam_role_policy" "glue_crawler_s3" {
  name = "s3-schema-discovery"
  role = aws_iam_role.glue_crawler_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          var.s3_bucket_arn,
          "${var.s3_bucket_arn}/bronze/masked/*",
          "${var.s3_bucket_arn}/gold/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = var.kms_key_arn
      },
    ]
  })
}

resource "aws_glue_crawler" "schema_discovery" {
  name          = "${local.name_prefix}-schema"
  role          = aws_iam_role.glue_crawler_role.arn
  database_name = local.glue_db
  schedule      = "cron(0 2 * * ? *)"

  s3_target {
    path       = "s3://${var.s3_bucket_id}/bronze/masked/"
    exclusions = ["metadata/**", "**.json", "**.avro"]
  }

  s3_target {
    path       = "s3://${var.s3_bucket_id}/gold/"
    exclusions = ["metadata/**", "**.json", "**.avro"]
  }

  schema_change_policy {
    update_behavior = "UPDATE_IN_DATABASE"
    delete_behavior = "LOG"
  }

  configuration = jsonencode({
    Version = 1.0
    CrawlerOutput = {
      Partitions = { AddOrUpdateBehavior = "InheritFromTable" }
      Tables     = { AddOrUpdateBehavior = "MergeNewColumns" }
    }
    Grouping = {
      TableGroupingPolicy = "CombineCompatibleSchemas"
    }
  })
}
