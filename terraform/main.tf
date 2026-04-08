terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Remote state — shared across all CI runs and local developer machines.
  # State bucket created once via: aws s3 mb s3://tfstate-search-keyword-analyzer-<account_id>
  backend "s3" {
    bucket = "tfstate-search-keyword-analyzer-107422471374"
    key    = "search-keyword-analyzer/dev/terraform.tfstate"
    region = "us-east-1"
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# ============================================================
# Variables
# ============================================================

variable "aws_region" {
  description = "AWS region for all resources."
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment (dev, staging, prod)."
  default     = "dev"
}

variable "project_name" {
  description = "Project name prefix applied to all resource names."
  default     = "search-keyword-analyzer"
}

variable "lambda_timeout_seconds" {
  description = "Lambda function timeout in seconds."
  type        = number
  default     = 300
}

variable "lambda_memory_mb" {
  description = "Lambda function memory in MB."
  type        = number
  default     = 512
}

variable "log_retention_days" {
  description = "CloudWatch log group retention in days."
  type        = number
  default     = 14
}

variable "landing_retention_days" {
  description = "Days before landing/ objects are deleted."
  type        = number
  default     = 60
}

variable "bronze_retention_days" {
  description = "Days before bronze/ objects are deleted."
  type        = number
  default     = 365
}

variable "gold_retention_days" {
  description = "Days before gold/ objects are deleted."
  type        = number
  default     = 365
}

variable "athena_results_retention_days" {
  description = "Days before athena-results/ query output is deleted."
  type        = number
  default     = 7
}

variable "athena_bytes_scanned_limit" {
  description = "Per-query Athena scan limit in bytes (default 100 MB — cost guard)."
  type        = number
  default     = 104857600
}

variable "budget_alert_email" {
  description = "Email address for monthly cost alerts (80% actual, 100% forecasted). Leave empty to skip."
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Monthly cost budget threshold in USD."
  default     = "50"
}

variable "enable_quicksight" {
  description = "Provision QuickSight data source and dataset. Requires a QuickSight subscription."
  type        = bool
  default     = false
}

variable "quicksight_username" {
  description = "QuickSight IAM user name. Required when enable_quicksight = true."
  default     = ""
}

data "aws_caller_identity" "current" {}

# ============================================================
# KMS — encryption at rest
# ============================================================

resource "aws_kms_key" "data_key" {
  description             = "Standard data key — landing, bronze/masked, gold, athena-results"
  deletion_window_in_days = 7
  enable_key_rotation     = true
}

resource "aws_kms_alias" "data_key" {
  name          = "alias/${var.project_name}-${var.environment}"
  target_key_id = aws_kms_key.data_key.key_id
}

# Dedicated PII key — Lambda can encrypt, admin can decrypt, developers cannot.
# Uses a wildcard principal so any pipeline Lambda role (naming pattern: project-lambda-*-env)
# automatically gets encrypt access without updating the key policy per new source.
resource "aws_kms_key" "pii_key" {
  description             = "PII field encryption — bronze/raw/ — admin-only decrypt"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableRootKeyAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        # Wildcard: any Lambda role named search-keyword-analyzer-lambda-*-env can encrypt.
        # This means adding a new source pipeline auto-inherits PII encrypt permission.
        Sid    = "AllowPipelineLambdaEncryptOnly"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.project_name}-lambda-*-${var.environment}"
        }
        Action   = ["kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = "*"
      },
      {
        Sid       = "AllowAdminDecrypt"
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.admin_role.arn }
        Action    = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey", "kms:ReEncrypt*"]
        Resource  = "*"
      },
    ]
  })

  depends_on = [aws_iam_role.admin_role]
}

resource "aws_kms_alias" "pii_key" {
  name          = "alias/${var.project_name}-pii-${var.environment}"
  target_key_id = aws_kms_key.pii_key.key_id
}

# ============================================================
# S3 — medallion data lake (shared across all pipeline sources)
# ============================================================

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

# S3 bucket policy — defence-in-depth PII restriction on top of IAM.
# A bucket Deny cannot be overridden by any IAM Allow (except root).
resource "aws_s3_bucket_policy" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyRawBronzeForDevelopers"
        Effect    = "Deny"
        Principal = { AWS = aws_iam_role.developer_role.arn }
        Action    = "s3:*"
        Resource  = ["${aws_s3_bucket.data_lake.arn}/bronze/raw/*"]
      },
      {
        Sid       = "DenyLandingForDevelopers"
        Effect    = "Deny"
        Principal = { AWS = aws_iam_role.developer_role.arn }
        Action    = "s3:*"
        Resource  = ["${aws_s3_bucket.data_lake.arn}/landing/*"]
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.data_lake]
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
    expiration { days = var.landing_retention_days }
  }

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
    expiration { days = var.bronze_retention_days }
  }

  rule {
    id     = "archive-gold"
    status = "Enabled"
    filter { prefix = "gold/" }
    transition {
      days          = 180
      storage_class = "STANDARD_IA"
    }
    expiration { days = var.gold_retention_days }
  }

  rule {
    id     = "expire-athena-results"
    status = "Enabled"
    filter { prefix = "athena-results/" }
    expiration { days = var.athena_results_retention_days }
  }

  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"
    filter { prefix = "" }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

# Enable EventBridge notifications on the bucket.
# Each pipeline module registers its own EventBridge rule filtering by source prefix.
# This single resource is the only S3-level config needed here.
resource "aws_s3_bucket_notification" "eventbridge" {
  bucket      = aws_s3_bucket.data_lake.id
  eventbridge = true
}

# ============================================================
# IAM — Admin role (full PII access, can decrypt bronze/raw/)
# ============================================================

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
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:DeleteObject"]
        Resource = [aws_s3_bucket.data_lake.arn, "${aws_s3_bucket.data_lake.arn}/*"]
      },
      {
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
        Effect = "Allow"
        Action = ["glue:GetDatabase", "glue:GetDatabases", "glue:GetTable", "glue:GetTables", "glue:GetPartition", "glue:GetPartitions"]
        Resource = [
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:catalog",
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:database/${local.glue_database_name}",
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/${local.glue_database_name}/*",
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

# ============================================================
# IAM — Developer role (masked data only, no PII decryption)
# ============================================================

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
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${aws_s3_bucket.data_lake.arn}/bronze/masked/*",
          "${aws_s3_bucket.data_lake.arn}/gold/*",
          "${aws_s3_bucket.data_lake.arn}/athena-results/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = ["${aws_s3_bucket.data_lake.arn}/athena-results/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [aws_s3_bucket.data_lake.arn]
        Condition = {
          StringLike = { "s3:prefix" = ["bronze/masked/*", "gold/*", "athena-results/*"] }
        }
      },
      {
        Effect = "Deny"
        Action = "s3:*"
        Resource = [
          "${aws_s3_bucket.data_lake.arn}/bronze/raw/*",
          "${aws_s3_bucket.data_lake.arn}/landing/*",
        ]
      },
      {
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
        Effect = "Allow"
        Action = ["glue:GetDatabase", "glue:GetDatabases"]
        Resource = [
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:catalog",
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:database/${local.glue_database_name}",
        ]
      },
      {
        # Wildcard allow on masked and gold tables (works for any source name prefix).
        # Explicit deny below catches *_bronze_raw regardless of source name.
        Effect = "Allow"
        Action = ["glue:GetTable", "glue:GetTables", "glue:GetPartition", "glue:GetPartitions"]
        Resource = [
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/${local.glue_database_name}/*_bronze_masked",
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/${local.glue_database_name}/*_gold",
        ]
      },
      {
        # Defence-in-depth: hard deny on all raw PII tables regardless of IAM allows
        Effect = "Deny"
        Action = ["glue:GetTable", "glue:GetPartition", "glue:GetPartitions"]
        Resource = [
          "arn:aws:glue:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/${local.glue_database_name}/*_bronze_raw",
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

# ============================================================
# Glue Catalog — shared database + encryption
# ============================================================

locals {
  glue_database_name = replace("${var.project_name}_${var.environment}", "-", "_")
}

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

resource "aws_glue_catalog_database" "analytics" {
  name = local.glue_database_name
}

# ============================================================
# Athena — query engine
# ============================================================

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
    bytes_scanned_cutoff_per_query = var.athena_bytes_scanned_limit
  }
}

# ============================================================
# Lambda packaging — all source files in one zip
# Add new handlers (e.g. salesforce_handler.py) to src/ and they
# are automatically included. Lambda handler variable selects entry point.
# ============================================================

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/../dist/lambda.zip"
  excludes    = ["__pycache__", "tests"]
}

# ============================================================
# Pipeline modules
# To add a new source:
#   1. Create src/<source>_handler.py
#   2. Add a module block below (copy adobe block, change source_name,
#      lambda_handler, bronze_columns, gold_columns)
#   3. terraform apply — Lambda, Glue tables, EventBridge rule all created
# ============================================================

module "adobe_pipeline" {
  source = "./modules/pipeline"

  source_name           = "adobe"
  project_name          = var.project_name
  environment           = var.environment
  aws_region            = var.aws_region
  aws_account_id        = data.aws_caller_identity.current.account_id
  s3_bucket_id          = aws_s3_bucket.data_lake.id
  s3_bucket_arn         = aws_s3_bucket.data_lake.arn
  kms_key_arn           = aws_kms_key.data_key.arn
  pii_kms_key_arn       = aws_kms_key.pii_key.arn
  glue_database_name    = aws_glue_catalog_database.analytics.name
  athena_workgroup_name = aws_athena_workgroup.analytics.name
  lambda_handler        = "adobe_handler.lambda_handler"
  lambda_zip_path       = data.archive_file.lambda_zip.output_path
  lambda_zip_hash       = data.archive_file.lambda_zip.output_base64sha256
  lambda_timeout_seconds = var.lambda_timeout_seconds
  lambda_memory_mb      = var.lambda_memory_mb

  # Bronze columns — hit-level TSV schema Lambda writes.
  # ip and user_agent are PII: masked table hashes them, raw table keeps plaintext.
  bronze_columns = [
    { name = "hit_time_gmt",  type = "bigint", comment = "" },
    { name = "date_time",     type = "string", comment = "" },
    { name = "ip",            type = "string", comment = "PII-pseudonymized: sha256 hash of original IP" },
    { name = "user_agent",    type = "string", comment = "PII-pseudonymized: sha256 hash of original user agent" },
    { name = "event_list",    type = "string", comment = "" },
    { name = "geo_city",      type = "string", comment = "" },
    { name = "geo_region",    type = "string", comment = "" },
    { name = "geo_country",   type = "string", comment = "" },
    { name = "pagename",      type = "string", comment = "" },
    { name = "page_url",      type = "string", comment = "" },
    { name = "product_list",  type = "string", comment = "" },
    { name = "referrer",      type = "string", comment = "" },
  ]

  # Gold columns — aggregated output, no PII
  gold_columns = [
    { name = "search_engine_domain", type = "string", comment = "" },
    { name = "search_keyword",       type = "string", comment = "" },
    { name = "revenue",              type = "double",  comment = "" },
  ]
}

# ---- Example: adding Salesforce later ----
# module "salesforce_pipeline" {
#   source = "./modules/pipeline"
#   source_name    = "salesforce"
#   lambda_handler = "salesforce_handler.lambda_handler"
#   bronze_columns = [
#     { name = "contact_id", type = "string", comment = "" },
#     { name = "event_date", type = "string", comment = "" },
#     { name = "revenue",    type = "double",  comment = "" },
#   ]
#   gold_columns = [
#     { name = "campaign", type = "string", comment = "" },
#     { name = "revenue",  type = "double",  comment = "" },
#   ]
#   # All shared infrastructure vars same as adobe above
#   project_name   = var.project_name
#   environment    = var.environment
#   aws_region     = var.aws_region
#   aws_account_id = data.aws_caller_identity.current.account_id
#   s3_bucket_id   = aws_s3_bucket.data_lake.id
#   s3_bucket_arn  = aws_s3_bucket.data_lake.arn
#   kms_key_arn    = aws_kms_key.data_key.arn
#   pii_kms_key_arn       = aws_kms_key.pii_key.arn
#   glue_database_name    = aws_glue_catalog_database.analytics.name
#   athena_workgroup_name = aws_athena_workgroup.analytics.name
#   lambda_zip_path       = data.archive_file.lambda_zip.output_path
#   lambda_zip_hash       = data.archive_file.lambda_zip.output_base64sha256
# }

# ============================================================
# CloudWatch — operations dashboard
# ============================================================

resource "aws_cloudwatch_dashboard" "pipeline_ops" {
  dashboard_name = "${var.project_name}-${var.environment}"

  dashboard_body = jsonencode({
    widgets = [
      # ── Row 1: Lambda health ──────────────────────────────────────────────
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 8
        height = 6
        properties = {
          title   = "Lambda Invocations"
          view    = "timeSeries"
          region  = var.aws_region
          period  = 3600
          stat    = "Sum"
          metrics = [["AWS/Lambda", "Invocations", "FunctionName", module.adobe_pipeline.lambda_function_name]]
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 0
        width  = 8
        height = 6
        properties = {
          title  = "Lambda Duration (ms) — drives cost"
          view   = "timeSeries"
          region = var.aws_region
          period = 3600
          metrics = [
            ["AWS/Lambda", "Duration", "FunctionName", module.adobe_pipeline.lambda_function_name, { stat = "Average", label = "Avg" }],
            ["AWS/Lambda", "Duration", "FunctionName", module.adobe_pipeline.lambda_function_name, { stat = "p99", label = "p99" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 16
        y      = 0
        width  = 8
        height = 6
        properties = {
          title  = "Lambda Errors & Throttles"
          view   = "timeSeries"
          region = var.aws_region
          period = 3600
          metrics = [
            ["AWS/Lambda", "Errors", "FunctionName", module.adobe_pipeline.lambda_function_name, { stat = "Sum", color = "#d62728", label = "Errors" }],
            ["AWS/Lambda", "Throttles", "FunctionName", module.adobe_pipeline.lambda_function_name, { stat = "Sum", color = "#ff7f0e", label = "Throttles" }],
          ]
        }
      },
      # ── Row 2: Pipeline throughput ────────────────────────────────────────
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 8
        height = 6
        properties = {
          title  = "Lambda Concurrent Executions"
          view   = "timeSeries"
          region = var.aws_region
          period = 60
          metrics = [
            ["AWS/Lambda", "ConcurrentExecutions", "FunctionName", module.adobe_pipeline.lambda_function_name, { stat = "Maximum" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 6
        width  = 8
        height = 6
        properties = {
          title  = "EventBridge Invocations (pipeline triggers)"
          view   = "timeSeries"
          region = var.aws_region
          period = 3600
          metrics = [
            ["AWS/Events", "Invocations", "RuleName", "${var.project_name}-adobe-${var.environment}-landing-upload", { stat = "Sum" }],
            ["AWS/Events", "FailedInvocations", "RuleName", "${var.project_name}-adobe-${var.environment}-landing-upload", { stat = "Sum", color = "#d62728" }],
          ]
        }
      },
      # ── Row 3: Cost indicators ────────────────────────────────────────────
      {
        type   = "metric"
        x      = 0
        y      = 12
        width  = 8
        height = 6
        properties = {
          title  = "Athena Data Scanned (bytes) — $5/TB"
          view   = "timeSeries"
          region = var.aws_region
          period = 86400
          metrics = [
            ["AWS/Athena", "DataScannedInBytes", "WorkGroup", aws_athena_workgroup.analytics.name, { stat = "Sum" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 8
        y      = 12
        width  = 8
        height = 6
        properties = {
          title  = "KMS API Calls — $0.03/10k"
          view   = "timeSeries"
          region = var.aws_region
          period = 86400
          metrics = [
            ["AWS/KMS", "NumberOfRequestsSucceeded", "KeyId", aws_kms_key.data_key.key_id, { stat = "Sum", label = "data_key" }],
            ["AWS/KMS", "NumberOfRequestsSucceeded", "KeyId", aws_kms_key.pii_key.key_id, { stat = "Sum", label = "pii_key" }],
          ]
        }
      },
      {
        type   = "alarm"
        x      = 16
        y      = 12
        width  = 8
        height = 6
        properties = {
          title  = "Active Alarms"
          region = var.aws_region
          alarms = [module.adobe_pipeline.lambda_error_alarm_arn]
        }
      },
    ]
  })
}

# ============================================================
# AWS Budgets — monthly cost guardrail
# ============================================================

resource "aws_budgets_budget" "monthly" {
  count = var.budget_alert_email != "" ? 1 : 0

  name         = "${var.project_name}-monthly-${var.environment}"
  budget_type  = "COST"
  limit_amount = var.monthly_budget_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = ["user:Project$${var.project_name}"]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}

# ============================================================
# QuickSight — visualization (optional, disabled by default)
# To enable: set enable_quicksight = true and quicksight_username in terraform.tfvars
# ============================================================

resource "aws_quicksight_data_source" "athena" {
  count = var.enable_quicksight ? 1 : 0

  aws_account_id = data.aws_caller_identity.current.account_id
  data_source_id = "${var.project_name}-${var.environment}"
  name           = "Search Keyword Analyzer (${var.environment})"
  type           = "ATHENA"

  parameters {
    athena { work_group = aws_athena_workgroup.analytics.name }
  }

  permission {
    actions = [
      "quicksight:DescribeDataSource",
      "quicksight:DescribeDataSourcePermissions",
      "quicksight:PassDataSource",
      "quicksight:UpdateDataSource",
      "quicksight:DeleteDataSource",
      "quicksight:UpdateDataSourcePermissions",
    ]
    principal = "arn:aws:quicksight:${var.aws_region}:${data.aws_caller_identity.current.account_id}:user/default/${var.quicksight_username}"
  }
}

resource "aws_quicksight_data_set" "gold_performance" {
  count = var.enable_quicksight ? 1 : 0

  aws_account_id = data.aws_caller_identity.current.account_id
  data_set_id    = "${var.project_name}-gold-${var.environment}"
  name           = "Gold: Keyword Performance (${var.environment})"
  import_mode    = "DIRECT_QUERY"

  physical_table_map {
    physical_table_map_id = "gold_keyword_performance"
    relational_table {
      data_source_arn = aws_quicksight_data_source.athena[0].arn
      catalog         = "AWSDataCatalog"
      schema          = aws_glue_catalog_database.analytics.name
      name            = module.adobe_pipeline.gold_table

      input_columns {
        name = "search_engine_domain"
        type = "STRING"
      }
      input_columns {
        name = "search_keyword"
        type = "STRING"
      }
      input_columns {
        name = "revenue"
        type = "DECIMAL"
      }
    }
  }

  permissions {
    actions = [
      "quicksight:DescribeDataSet",
      "quicksight:DescribeDataSetPermissions",
      "quicksight:PassDataSet",
      "quicksight:DescribeIngestion",
      "quicksight:ListIngestions",
      "quicksight:UpdateDataSet",
      "quicksight:DeleteDataSet",
      "quicksight:CreateIngestion",
      "quicksight:CancelIngestion",
      "quicksight:UpdateDataSetPermissions",
    ]
    principal = "arn:aws:quicksight:${var.aws_region}:${data.aws_caller_identity.current.account_id}:user/default/${var.quicksight_username}"
  }
}

# ============================================================
# Outputs
# ============================================================

output "s3_bucket" {
  value = aws_s3_bucket.data_lake.id
}

output "athena_database" {
  value = aws_glue_catalog_database.analytics.name
}

output "athena_workgroup" {
  value = aws_athena_workgroup.analytics.name
}

output "admin_role_arn" {
  description = "Assume this role to query bronze_raw (plaintext PII — restricted)"
  value       = aws_iam_role.admin_role.arn
}

output "developer_role_arn" {
  description = "Assume this role for standard development — bronze/masked and gold only"
  value       = aws_iam_role.developer_role.arn
}

output "pii_kms_key_arn" {
  description = "PII KMS key — admin role can decrypt, Lambda can encrypt, developers have no access"
  value       = aws_kms_key.pii_key.arn
}

output "cloudwatch_dashboard_url" {
  description = "Operations dashboard — Lambda health, Athena scan cost, KMS API calls"
  value       = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${aws_cloudwatch_dashboard.pipeline_ops.dashboard_name}"
}

output "budget_status" {
  value = length(aws_budgets_budget.monthly) > 0 ? "Active — alerts at 80%/100% of $${var.monthly_budget_usd}/mo → ${var.budget_alert_email}" : "Not created — set budget_alert_email in terraform.tfvars"
}

output "quicksight_status" {
  value = var.enable_quicksight ? aws_quicksight_data_set.gold_performance[0].arn : "Disabled — set enable_quicksight=true in terraform.tfvars"
}

output "adobe_pipeline" {
  description = "Adobe pipeline resource names"
  value = {
    lambda_function  = module.adobe_pipeline.lambda_function_name
    bronze_masked    = module.adobe_pipeline.bronze_masked_table
    bronze_raw       = module.adobe_pipeline.bronze_raw_table
    gold             = module.adobe_pipeline.gold_table
    glue_crawler     = module.adobe_pipeline.glue_crawler_name
    trigger_command  = "aws s3 cp data/data.sql s3://${aws_s3_bucket.data_lake.id}/landing/adobe/data.sql"
  }
}

output "sample_athena_queries" {
  value = <<-EOT
    -- Workgroup: ${var.project_name}-${var.environment}
    -- Database:  ${local.glue_database_name}

    -- Top keywords by revenue (Adobe gold layer)
    SELECT search_engine_domain, search_keyword, revenue
    FROM ${local.glue_database_name}.adobe_gold
    ORDER BY revenue DESC;

    -- Hit counts by page (Adobe masked bronze — developer accessible)
    SELECT pagename, COUNT(*) AS hits
    FROM ${local.glue_database_name}.adobe_bronze_masked
    GROUP BY pagename ORDER BY hits DESC;

    -- Purchase events with hashed IP
    SELECT date_time, ip, geo_city, product_list
    FROM ${local.glue_database_name}.adobe_bronze_masked
    WHERE event_list LIKE '%1%';
  EOT
}
