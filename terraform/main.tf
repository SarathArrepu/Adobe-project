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

# ---- KMS (PII field-level protection) ----
# Separate key dedicated to bronze/raw/ — Lambda can encrypt, only admin role can decrypt.
# Developer role is intentionally absent from this key policy (default deny).

resource "aws_kms_key" "pii_key" {
  description             = "PII field encryption — restricts ip/user_agent access to admin role only"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Account root retains break-glass access for key administration only
        Sid       = "EnableRootKeyAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        # Lambda can encrypt/generate data keys when writing bronze/raw/ — but CANNOT decrypt
        Sid       = "AllowLambdaEncryptOnly"
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.lambda_role.arn }
        Action    = ["kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource  = "*"
      },
      {
        # Admin role has full decrypt access — required to read plaintext PII from bronze/raw/
        Sid       = "AllowAdminDecrypt"
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.admin_role.arn }
        Action    = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey", "kms:ReEncrypt*"]
        Resource  = "*"
      },
    ]
  })

  depends_on = [aws_iam_role.lambda_role, aws_iam_role.admin_role]
}

resource "aws_kms_alias" "pii_key" {
  name          = "alias/${var.project_name}-pii-${var.environment}"
  target_key_id = aws_kms_key.pii_key.key_id
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

# ---- S3 Bucket Policy (defence-in-depth PII access restriction) ----
# Provides a second enforcement layer on top of IAM role policies.
# A bucket policy Deny cannot be overridden by any IAM Allow (except root).

resource "aws_s3_bucket_policy" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Developer role must never access bronze/raw/ — plaintext PII resides here
        Sid       = "DenyRawBronzeForDevelopers"
        Effect    = "Deny"
        Principal = { AWS = aws_iam_role.developer_role.arn }
        Action    = "s3:*"
        Resource = [
          "${aws_s3_bucket.data_lake.arn}/bronze/raw/*",
        ]
      },
      {
        # Developer role must not access landing/ — raw unprocessed uploads
        Sid       = "DenyLandingForDevelopers"
        Effect    = "Deny"
        Principal = { AWS = aws_iam_role.developer_role.arn }
        Action    = "s3:*"
        Resource = [
          "${aws_s3_bucket.data_lake.arn}/landing/*",
        ]
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.data_lake]
}

resource "aws_s3_bucket_lifecycle_configuration" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id

  # landing/ — raw uploads; move to Glacier quickly, delete after 60 days
  rule {
    id     = "archive-landing"
    status = "Enabled"
    filter { prefix = "landing/" }
    transition {
      days          = 30
      storage_class = "GLACIER"
    }
    expiration {
      days = 60
    }
  }

  # bronze/ — raw archive; IA after 90d, Glacier after 180d, delete after 1yr
  rule {
    id     = "archive-bronze"
    status = "Enabled"
    filter { prefix = "bronze/" }
    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = 180
      storage_class = "GLACIER"
    }
    expiration {
      days = 365
    }
  }

  # gold/ — output reports; IA after 180d, delete after 1yr
  rule {
    id     = "archive-gold"
    status = "Enabled"
    filter { prefix = "gold/" }
    transition {
      days          = 180
      storage_class = "STANDARD_IA"
    }
    expiration {
      days = 365
    }
  }

  # athena-results/ — query scratch space; delete after 7 days
  rule {
    id     = "expire-athena-results"
    status = "Enabled"
    filter { prefix = "athena-results/" }
    expiration {
      days = 7
    }
  }

  # Clean up incomplete multipart uploads older than 7 days
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"
    filter { prefix = "" }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
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
        # Standard data key — used for landing, bronze/masked, gold, athena-results
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = aws_kms_key.data_key.arn
      },
      {
        # PII key — encrypt only. Lambda writes bronze/raw but NEVER decrypts PII.
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = aws_kms_key.pii_key.arn
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ---- IAM (admin role — full PII access, can decrypt bronze/raw/) ----

resource "aws_iam_role" "admin_role" {
  name = "${var.project_name}-admin-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
    }]
  })
}

resource "aws_iam_role_policy" "admin_s3" {
  name = "s3-admin-full-access"
  role = aws_iam_role.admin_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Full read/write across all S3 layers including bronze/raw/ (PII data)
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:DeleteObject"]
        Resource = [aws_s3_bucket.data_lake.arn, "${aws_s3_bucket.data_lake.arn}/*"]
      },
      {
        # Access to both KMS keys — allows decrypting PII from bronze/raw/
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = [aws_kms_key.data_key.arn, aws_kms_key.pii_key.arn]
      },
    ]
  })
}

resource "aws_iam_role_policy" "admin_glue_athena" {
  name = "glue-athena-admin-full"
  role = aws_iam_role.admin_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Admin can query all tables including bronze_hits_raw (plaintext PII)
        Effect   = "Allow"
        Action   = ["glue:GetDatabase", "glue:GetDatabases", "glue:GetTable", "glue:GetTables", "glue:GetPartition", "glue:GetPartitions"]
        Resource = [
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:catalog",
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:database/${replace("${var.project_name}_${var.environment}", "-", "_")}",
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/${replace("${var.project_name}_${var.environment}", "-", "_")}/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults", "athena:StopQueryExecution", "athena:GetWorkGroup"]
        Resource = ["arn:aws:athena:${var.aws_region}:${data.aws_caller_identity.current.account_id}:workgroup/${var.project_name}-${var.environment}"]
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "admin_cloudwatch" {
  role       = aws_iam_role.admin_role.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchLogsReadOnlyAccess"
}

# ---- IAM (developer role — masked data only, NO PII decryption) ----

resource "aws_iam_role" "developer_role" {
  name = "${var.project_name}-developer-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
    }]
  })
}

resource "aws_iam_role_policy" "developer_s3" {
  name = "s3-developer-masked-only"
  role = aws_iam_role.developer_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Read access limited to masked bronze and gold — no raw bronze or landing
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${aws_s3_bucket.data_lake.arn}/bronze/masked/*",
          "${aws_s3_bucket.data_lake.arn}/gold/*",
          "${aws_s3_bucket.data_lake.arn}/athena-results/*",
        ]
      },
      {
        # Athena requires write to athena-results/ to store query output
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = ["${aws_s3_bucket.data_lake.arn}/athena-results/*"]
      },
      {
        # ListBucket scoped to allowed prefixes only
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [aws_s3_bucket.data_lake.arn]
        Condition = {
          StringLike = { "s3:prefix" = ["bronze/masked/*", "gold/*", "athena-results/*"] }
        }
      },
      {
        # Explicit IAM-level deny on raw PII data (defence-in-depth alongside S3 bucket policy)
        Effect   = "Deny"
        Action   = "s3:*"
        Resource = [
          "${aws_s3_bucket.data_lake.arn}/bronze/raw/*",
          "${aws_s3_bucket.data_lake.arn}/landing/*",
        ]
      },
      {
        # Standard data key only — allows S3 file decryption for masked/gold layers
        # pii_key is intentionally absent: developer cannot decrypt bronze/raw/ objects
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = aws_kms_key.data_key.arn
      },
    ]
  })
}

resource "aws_iam_role_policy" "developer_glue_athena" {
  name = "glue-athena-developer-masked-only"
  role = aws_iam_role.developer_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Catalog and database level (required to resolve table names in Athena)
        Effect = "Allow"
        Action = ["glue:GetDatabase", "glue:GetDatabases"]
        Resource = [
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:catalog",
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:database/${replace("${var.project_name}_${var.environment}", "-", "_")}",
        ]
      },
      {
        # Table access scoped to masked and gold tables ONLY — bronze_hits_raw is excluded
        Effect = "Allow"
        Action = ["glue:GetTable", "glue:GetTables", "glue:GetPartition", "glue:GetPartitions"]
        Resource = [
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/${replace("${var.project_name}_${var.environment}", "-", "_")}/bronze_hits_masked",
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/${replace("${var.project_name}_${var.environment}", "-", "_")}/gold_keyword_performance",
        ]
      },
      {
        Effect = "Allow"
        Action = ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults", "athena:StopQueryExecution", "athena:GetWorkGroup"]
        Resource = ["arn:aws:athena:${var.aws_region}:${data.aws_caller_identity.current.account_id}:workgroup/${var.project_name}-${var.environment}"]
      },
    ]
  })
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
      ENVIRONMENT     = var.environment
      LOG_LEVEL       = "INFO"
      KMS_KEY_ARN     = aws_kms_key.data_key.arn     # Standard key for masked bronze / gold
      PII_KMS_KEY_ARN = aws_kms_key.pii_key.arn      # PII key for raw bronze (encrypt only)
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

# ---- Glue Catalog encryption ----

resource "aws_glue_data_catalog_encryption_settings" "catalog" {
  data_catalog_encryption_settings {
    connection_password_encryption {
      aws_kms_key_id                       = aws_kms_key.data_key.arn
      return_connection_password_encrypted = true
    }
    encryption_at_rest {
      catalog_encryption_mode = "SSE-KMS"
      sse_aws_kms_key_id      = aws_kms_key.data_key.arn
    }
  }
}

# ---- Glue Catalog (schema registry for Athena) ----

resource "aws_glue_catalog_database" "analytics" {
  name = replace("${var.project_name}_${var.environment}", "-", "_")
}

# Bronze/masked table — pseudonymized hit-level data (ip and user_agent SHA-256 hashed)
# Developer role has Glue access to this table. PII values are irreversible hashes.
resource "aws_glue_catalog_table" "bronze_hits_masked" {
  name          = "bronze_hits_masked"
  database_name = aws_glue_catalog_database.analytics.name

  table_type = "EXTERNAL_TABLE"
  parameters = {
    "classification"         = "csv"
    "skip.header.line.count" = "1"
    "pii_handling"           = "pseudonymized-sha256"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.data_lake.id}/bronze/masked/"
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

# Bronze/raw table — original hit-level data with plaintext PII
# Admin role ONLY. Developer role lacks kms:Decrypt on pii_key and has no Glue permission here.
resource "aws_glue_catalog_table" "bronze_hits_raw" {
  name          = "bronze_hits_raw"
  database_name = aws_glue_catalog_database.analytics.name

  table_type = "EXTERNAL_TABLE"
  parameters = {
    "classification"         = "csv"
    "skip.header.line.count" = "1"
    "pii_handling"           = "encrypted-at-rest-pii-kms-key"
    "data_classification"    = "restricted-pii"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.data_lake.id}/bronze/raw/"
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
      name    = "hit_time_gmt"
      type    = "bigint"
    }
    columns {
      name    = "date_time"
      type    = "string"
    }
    columns {
      name    = "user_agent"
      type    = "string"
      comment = "PII: quasi-identifier — plaintext in raw layer, admin access only"
    }
    columns {
      name    = "ip"
      type    = "string"
      comment = "PII: direct visitor identifier — plaintext in raw layer, admin access only"
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

output "admin_role_arn" {
  description = "Assume this role to query bronze_hits_raw (plaintext PII — restricted)"
  value       = aws_iam_role.admin_role.arn
}

output "developer_role_arn" {
  description = "Assume this role for standard development — bronze/masked and gold layers only"
  value       = aws_iam_role.developer_role.arn
}

output "pii_kms_key_arn" {
  description = "PII KMS key — admin role can decrypt, Lambda can encrypt, developers have no access"
  value       = aws_kms_key.pii_key.arn
}
