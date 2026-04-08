terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project     = "search-keyword-analyzer"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# ---- Variables ----

variable "aws_region" {
  default = "us-east-1"
}

variable "environment" {
  default = "dev"
}

variable "project_name" {
  default = "search-keyword-analyzer"
}

data "aws_caller_identity" "current" {}

# ---- KMS (encryption at rest) ----

resource "aws_kms_key" "data_key" {
  description             = "Encrypt hit-level data at rest"
  deletion_window_in_days = 7
  enable_key_rotation     = true
}

resource "aws_kms_alias" "data_key" {
  name          = "alias/${var.project_name}-${var.environment}"
  target_key_id = aws_kms_key.data_key.key_id
}

# ---- S3 (medallion lakehouse) ----
# Prefixes: landing/ -> bronze/ -> silver/ -> gold/

resource "aws_s3_bucket" "data_lake" {
  bucket        = "${var.project_name}-${var.environment}-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_versioning" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data_key.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "data_lake" {
  bucket                  = aws_s3_bucket.data_lake.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id

  rule {
    id     = "archive-landing"
    status = "Enabled"
    filter { prefix = "landing/" }
    transition {
      days          = 30
      storage_class = "GLACIER"
    }
  }

  rule {
    id     = "archive-bronze"
    status = "Enabled"
    filter { prefix = "bronze/" }
    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }
  }
}

# ---- IAM (least-privilege Lambda role) ----

resource "aws_iam_role" "lambda_role" {
  name = "${var.project_name}-lambda-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "lambda_s3" {
  name = "s3-data-lake-access"
  role = aws_iam_role.lambda_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:CopyObject"]
        Resource = "${aws_s3_bucket.data_lake.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.data_lake.arn
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = aws_kms_key.data_key.arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ---- Lambda (processing engine) ----

# Package source code — exclude __pycache__ and test files
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/../dist/lambda.zip"
  excludes    = ["__pycache__"]
}

resource "aws_lambda_function" "analyzer" {
  function_name    = "${var.project_name}-${var.environment}"
  role             = aws_iam_role.lambda_role.arn
  handler          = "lambda_handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 300
  memory_size      = 512
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  environment {
    variables = {
      ENVIRONMENT = var.environment
      LOG_LEVEL   = "INFO"
    }
  }
}

resource "aws_lambda_permission" "s3_trigger" {
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.analyzer.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.data_lake.arn
}

resource "aws_s3_bucket_notification" "landing_trigger" {
  bucket = aws_s3_bucket.data_lake.id
  lambda_function {
    lambda_function_arn = aws_lambda_function.analyzer.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "landing/"
  }
  depends_on = [aws_lambda_permission.s3_trigger]
}

# ---- CloudWatch (monitoring) ----

resource "aws_cloudwatch_log_group" "lambda_logs" {
  name              = "/aws/lambda/${aws_lambda_function.analyzer.function_name}"
  retention_in_days = 14
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "${var.project_name}-errors-${var.environment}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Lambda processing errors detected"
  dimensions = {
    FunctionName = aws_lambda_function.analyzer.function_name
  }
}

# ---- Athena (query engine for Gold layer) ----

resource "aws_athena_workgroup" "analytics" {
  name = "${var.project_name}-${var.environment}"
  configuration {
    result_configuration {
      output_location = "s3://${aws_s3_bucket.data_lake.id}/athena-results/"
      encryption_configuration {
        encryption_option = "SSE_KMS"
        kms_key_arn       = aws_kms_key.data_key.arn
      }
    }
    bytes_scanned_cutoff_per_query = 104857600
  }
}

# ---- Glue Catalog (schema registry for Athena) ----

resource "aws_glue_catalog_database" "analytics" {
  name = replace("${var.project_name}_${var.environment}", "-", "_")
}

# Bronze table — raw hit-level data
resource "aws_glue_catalog_table" "bronze_hits" {
  name          = "bronze_hits"
  database_name = aws_glue_catalog_database.analytics.name

  table_type = "EXTERNAL_TABLE"
  parameters = {
    "classification"         = "csv"
    "skip.header.line.count" = "1"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.data_lake.id}/bronze/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe"
      parameters = {
        "field.delim"            = "\t"
        "serialization.format"   = "\t"
        "skip.header.line.count" = "1"
      }
    }

    columns {
      name = "hit_time_gmt"
      type = "bigint"
    }
    columns {
      name = "date_time"
      type = "string"
    }
    columns {
      name = "user_agent"
      type = "string"
    }
    columns {
      name = "ip"
      type = "string"
    }
    columns {
      name = "event_list"
      type = "string"
    }
    columns {
      name = "geo_city"
      type = "string"
    }
    columns {
      name = "geo_region"
      type = "string"
    }
    columns {
      name = "geo_country"
      type = "string"
    }
    columns {
      name = "pagename"
      type = "string"
    }
    columns {
      name = "page_url"
      type = "string"
    }
    columns {
      name = "product_list"
      type = "string"
    }
    columns {
      name = "referrer"
      type = "string"
    }
  }
}

# Gold table — aggregated keyword performance output
resource "aws_glue_catalog_table" "gold_keyword_performance" {
  name          = "gold_keyword_performance"
  database_name = aws_glue_catalog_database.analytics.name

  table_type = "EXTERNAL_TABLE"
  parameters = {
    "classification"         = "csv"
    "skip.header.line.count" = "1"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.data_lake.id}/gold/"
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe"
      parameters = {
        "field.delim"            = "\t"
        "serialization.format"   = "\t"
        "skip.header.line.count" = "1"
      }
    }

    columns {
      name = "search_engine_domain"
      type = "string"
    }
    columns {
      name = "search_keyword"
      type = "string"
    }
    columns {
      name = "revenue"
      type = "double"
    }
  }
}

# ---- Outputs ----

output "s3_bucket" {
  value = aws_s3_bucket.data_lake.id
}

output "lambda_function" {
  value = aws_lambda_function.analyzer.function_name
}

output "athena_database" {
  value = aws_glue_catalog_database.analytics.name
}

output "athena_workgroup" {
  value = aws_athena_workgroup.analytics.name
}

output "trigger_command" {
  description = "Upload a file to trigger the pipeline"
  value       = "aws s3 cp data.sql s3://${aws_s3_bucket.data_lake.id}/landing/data.sql"
}
