terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Remote state — shared across all CI runs and local developer machines.
  # The state bucket is intentionally NOT managed by this config (bootstrap-only).
  # To initialise: terraform init
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

variable "budget_alert_email" {
  description = "Email address for monthly cost alerts (80% actual, 100% forecasted). Leave empty to skip budget creation."
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Monthly cost budget threshold in USD"
  default     = "50"
}

variable "enable_quicksight" {
  description = "Provision QuickSight data source and dataset pointing to gold_keyword_performance via Athena. Requires a QuickSight Standard/Enterprise subscription in the account."
  type        = bool
  default     = false
}

variable "quicksight_username" {
  description = "QuickSight IAM user name (not ARN) to grant read access. Required when enable_quicksight = true."
  default     = ""
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
        Effect = "Allow"
        Action = ["glue:GetDatabase", "glue:GetDatabases", "glue:GetTable", "glue:GetTables", "glue:GetPartition", "glue:GetPartitions"]
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
        Effect = "Deny"
        Action = "s3:*"
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
        Effect   = "Allow"
        Action   = ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults", "athena:StopQueryExecution", "athena:GetWorkGroup"]
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
      KMS_KEY_ARN     = aws_kms_key.data_key.arn # Standard key for masked bronze / gold
      PII_KMS_KEY_ARN = aws_kms_key.pii_key.arn  # PII key for raw bronze (encrypt only)
    }
  }
}

# Lambda is now invoked by Step Functions (not directly by S3).
# EventBridge + Step Functions replaced the raw S3→Lambda trigger — see the
# orchestration section below for aws_s3_bucket_notification and event rules.

# ---- CloudWatch (monitoring + ops dashboard) ----

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

# Operations + cost-visibility dashboard.
# Key cost drivers shown: Lambda duration ($/GB-second), Athena bytes scanned ($/TB),
# Step Functions state transitions ($/1k transitions), KMS API calls ($/10k requests).
resource "aws_cloudwatch_dashboard" "pipeline_ops" {
  dashboard_name = "${var.project_name}-${var.environment}"

  dashboard_body = jsonencode({
    widgets = [
      # ── Row 1: Lambda health ─────────────────────────────────────────────────
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
          metrics = [["AWS/Lambda", "Invocations", "FunctionName", aws_lambda_function.analyzer.function_name]]
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
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.analyzer.function_name, { stat = "Average", label = "Avg" }],
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.analyzer.function_name, { stat = "p99", label = "p99" }],
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
            ["AWS/Lambda", "Errors", "FunctionName", aws_lambda_function.analyzer.function_name, { stat = "Sum", color = "#d62728", label = "Errors" }],
            ["AWS/Lambda", "Throttles", "FunctionName", aws_lambda_function.analyzer.function_name, { stat = "Sum", color = "#ff7f0e", label = "Throttles" }],
          ]
        }
      },
      # ── Row 2: Pipeline orchestration ────────────────────────────────────────
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 8
        height = 6
        properties = {
          title  = "Step Functions Executions"
          view   = "timeSeries"
          region = var.aws_region
          period = 3600
          metrics = [
            ["AWS/States", "ExecutionsSucceeded", "StateMachineArn", aws_sfn_state_machine.pipeline.arn, { stat = "Sum", color = "#2ca02c", label = "Succeeded" }],
            ["AWS/States", "ExecutionsFailed", "StateMachineArn", aws_sfn_state_machine.pipeline.arn, { stat = "Sum", color = "#d62728", label = "Failed" }],
            ["AWS/States", "ExecutionsTimedOut", "StateMachineArn", aws_sfn_state_machine.pipeline.arn, { stat = "Sum", color = "#ff7f0e", label = "Timed Out" }],
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
          title  = "Step Functions Duration (ms)"
          view   = "timeSeries"
          region = var.aws_region
          period = 3600
          metrics = [
            ["AWS/States", "ExecutionTime", "StateMachineArn", aws_sfn_state_machine.pipeline.arn, { stat = "Average" }],
          ]
        }
      },
      # ── Row 3: Data & cost indicators ────────────────────────────────────────
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
          alarms = [aws_cloudwatch_metric_alarm.lambda_errors.arn]
        }
      },
    ]
  })
}

# ---- AWS Budgets (cost guardrail) ----
#
# Sends email alerts when actual spend passes 80% and when *forecasted* spend
# passes 100% of the monthly threshold. Uses Project tag for attribution.
# Create by setting: budget_alert_email = "your@email.com" in terraform.tfvars

resource "aws_budgets_budget" "monthly" {
  count = var.budget_alert_email != "" ? 1 : 0

  name         = "${var.project_name}-monthly-${var.environment}"
  budget_type  = "COST"
  limit_amount = var.monthly_budget_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = ["user:Project$search-keyword-analyzer"]
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
#
# Tables are registered here as Apache Iceberg format.
# Terraform creates the Glue schema entries; the actual Iceberg table metadata
# (manifest files, snapshots, schema.json) is written by the first INSERT via Athena
# or the Lambda using the Iceberg SDK. S3 prefix layout:
#
#   bronze/masked/        — Iceberg table root (data + metadata subfolders)
#   bronze/raw/           — Iceberg table root (PII KMS key, admin only)
#   gold/                 — Iceberg table root
#
# Why Iceberg over plain Hive external tables?
#   - ACID transactions: concurrent Lambda writes don't corrupt the table
#   - Schema evolution: add columns without re-writing all files
#   - Time travel: query data as-of a past snapshot (audit, debugging)
#   - Row-level deletes: GDPR right-to-be-forgotten — delete a visitor's rows
#     by IP hash without rewriting entire partitions
#   - Partition pruning on hidden partitions: no manual partition management

resource "aws_glue_catalog_database" "analytics" {
  name = replace("${var.project_name}_${var.environment}", "-", "_")
}

# Bronze/masked — Iceberg, pseudonymized PII, developer + admin access
resource "aws_glue_catalog_table" "bronze_hits_masked" {
  name          = "bronze_hits_masked"
  database_name = aws_glue_catalog_database.analytics.name

  table_type = "EXTERNAL_TABLE"

  open_table_format_input {
    iceberg_input {
      metadata_operation = "CREATE"
      version            = "2"
    }
  }

  parameters = {
    "table_type"        = "ICEBERG"
    "pii_handling"      = "pseudonymized-sha256"
    "format"            = "parquet"
    "write_compression" = "snappy"
  }

  storage_descriptor {
    location = "s3://${aws_s3_bucket.data_lake.id}/bronze/masked/"

    columns {
      name = "hit_time_gmt"
      type = "bigint"
    }
    columns {
      name = "date_time"
      type = "string"
    }
    columns {
      name    = "ip"
      type    = "string"
      comment = "PII-pseudonymized: sha256 hash of original IP address"
    }
    columns {
      name    = "user_agent"
      type    = "string"
      comment = "PII-pseudonymized: sha256 hash of original user agent string"
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
    columns {
      name    = "ingestion_date"
      type    = "date"
      comment = "Hidden partition column — date the record was ingested"
    }
  }
}

# Bronze/raw — Iceberg, plaintext PII, admin role ONLY
# Objects encrypted with pii_key; developer role has no kms:Decrypt on that key.
resource "aws_glue_catalog_table" "bronze_hits_raw" {
  name          = "bronze_hits_raw"
  database_name = aws_glue_catalog_database.analytics.name

  table_type = "EXTERNAL_TABLE"

  open_table_format_input {
    iceberg_input {
      metadata_operation = "CREATE"
      version            = "2"
    }
  }

  parameters = {
    "table_type"          = "ICEBERG"
    "data_classification" = "restricted-pii"
    "pii_handling"        = "plaintext-pii-kms-encrypted"
    "format"              = "parquet"
    "write_compression"   = "snappy"
  }

  storage_descriptor {
    location = "s3://${aws_s3_bucket.data_lake.id}/bronze/raw/"

    columns {
      name = "hit_time_gmt"
      type = "bigint"
    }
    columns {
      name = "date_time"
      type = "string"
    }
    columns {
      name    = "ip"
      type    = "string"
      comment = "PII: plaintext visitor IP — admin access only via pii_key KMS"
    }
    columns {
      name    = "user_agent"
      type    = "string"
      comment = "PII: plaintext user agent — admin access only via pii_key KMS"
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
    columns {
      name    = "ingestion_date"
      type    = "date"
      comment = "Hidden partition column — date the record was ingested"
    }
  }
}

# Gold — Iceberg, no PII, developer + admin access
resource "aws_glue_catalog_table" "gold_keyword_performance" {
  name          = "gold_keyword_performance"
  database_name = aws_glue_catalog_database.analytics.name

  table_type = "EXTERNAL_TABLE"

  open_table_format_input {
    iceberg_input {
      metadata_operation = "CREATE"
      version            = "2"
    }
  }

  parameters = {
    "table_type"        = "ICEBERG"
    "format"            = "parquet"
    "write_compression" = "snappy"
  }

  storage_descriptor {
    location = "s3://${aws_s3_bucket.data_lake.id}/gold/"

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

# ---- Step Functions (orchestration) ----
#
# Replaces the raw S3→Lambda fire-and-forget trigger with a managed state machine.
# The S3 event now triggers Step Functions via EventBridge; Lambda is invoked as
# a task inside the workflow rather than directly, enabling:
#   - Retries with exponential back-off per step
#   - Timeout enforcement
#   - Failure notifications (SNS/CloudWatch)
#   - Extensibility: add Glue job, data quality check, or notification steps
#
# Orchestration flow:
#   S3 ObjectCreated (landing/)
#     └─► EventBridge Rule
#           └─► Step Functions state machine
#                 ├─ ProcessFile (Lambda, up to 3 retries)
#                 └─ on failure → CloudWatch alarm triggers

resource "aws_iam_role" "step_functions_role" {
  name = "${var.project_name}-sfn-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "step_functions_invoke_lambda" {
  name = "invoke-lambda"
  role = aws_iam_role.step_functions_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.analyzer.arn
    }]
  })
}

resource "aws_sfn_state_machine" "pipeline" {
  name     = "${var.project_name}-${var.environment}"
  role_arn = aws_iam_role.step_functions_role.arn

  definition = jsonencode({
    Comment = "Search Keyword Analyzer pipeline — processes S3 landing file through Lambda"
    StartAt = "ProcessFile"
    States = {
      ProcessFile = {
        Type     = "Task"
        Resource = aws_lambda_function.analyzer.arn
        Retry = [{
          ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "States.TaskFailed"]
          IntervalSeconds = 5
          MaxAttempts     = 3
          BackoffRate     = 2
        }]
        Catch = [{
          ErrorEquals = ["States.ALL"]
          Next        = "PipelineFailed"
        }]
        End = true
      }
      PipelineFailed = {
        Type  = "Fail"
        Error = "PipelineError"
        Cause = "Lambda processing failed after retries — check CloudWatch logs"
      }
    }
  })
}

# EventBridge rule: S3 landing/ uploads → Step Functions (replaces direct S3→Lambda trigger)
# Note: S3 must have EventBridge notifications enabled; the bucket notification resource
# below switches from Lambda to EventBridge as the delivery target.

resource "aws_iam_role" "eventbridge_role" {
  name = "${var.project_name}-eb-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "eventbridge_start_sfn" {
  name = "start-step-functions"
  role = aws_iam_role.eventbridge_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "states:StartExecution"
      Resource = aws_sfn_state_machine.pipeline.arn
    }]
  })
}

resource "aws_cloudwatch_event_rule" "s3_landing_upload" {
  name        = "${var.project_name}-landing-upload-${var.environment}"
  description = "Fires when a file is uploaded to the landing/ prefix"
  event_pattern = jsonencode({
    source      = ["aws.s3"]
    detail-type = ["Object Created"]
    detail = {
      bucket = { name = [aws_s3_bucket.data_lake.id] }
      object = { key = [{ prefix = "landing/" }] }
    }
  })
}

resource "aws_cloudwatch_event_target" "trigger_pipeline" {
  rule     = aws_cloudwatch_event_rule.s3_landing_upload.name
  arn      = aws_sfn_state_machine.pipeline.arn
  role_arn = aws_iam_role.eventbridge_role.arn

  # Pass the S3 event detail directly as the state machine input
  input_transformer {
    input_paths = {
      bucket = "$.detail.bucket.name"
      key    = "$.detail.object.key"
    }
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

# Enable EventBridge notifications on the S3 bucket (required for the rule above)
resource "aws_s3_bucket_notification" "landing_trigger" {
  bucket      = aws_s3_bucket.data_lake.id
  eventbridge = true
}

# ---- Glue Crawler (schema evolution) ----
#
# The aws_glue_catalog_table blocks above define the initial schema.
# As the source adds columns (or new S3 prefixes appear), the crawler auto-detects
# changes and updates the Glue Catalog without requiring a Terraform change.
#
# Policy:  UPDATE_IN_DATABASE — new columns are merged into existing tables.
#          LOG (delete) — removed columns are logged, not dropped, avoiding
#          accidental schema loss from a transient empty S3 partition.
#
# Scope: crawler intentionally excludes bronze/raw/ (PII key — crawler role
# only has access to the standard data key).

resource "aws_iam_role" "glue_crawler_role" {
  name = "${var.project_name}-crawler-${var.environment}"
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
        # Read access to developer-accessible prefixes only (not bronze/raw/)
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          aws_s3_bucket.data_lake.arn,
          "${aws_s3_bucket.data_lake.arn}/bronze/masked/*",
          "${aws_s3_bucket.data_lake.arn}/gold/*",
        ]
      },
      {
        # Standard data key — decrypt masked and gold files for schema inference
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = aws_kms_key.data_key.arn
      },
    ]
  })
}

resource "aws_glue_crawler" "schema_discovery" {
  name          = "${var.project_name}-${var.environment}"
  role          = aws_iam_role.glue_crawler_role.arn
  database_name = aws_glue_catalog_database.analytics.name

  # Run daily at 2am UTC — outside business hours, after overnight ingestion
  schedule = "cron(0 2 * * ? *)"

  # Crawl only developer-accessible layers. bronze/raw/ is excluded:
  # (a) the crawler role cannot decrypt the PII key, (b) schema is identical to masked.
  s3_target {
    path = "s3://${aws_s3_bucket.data_lake.id}/bronze/masked/"
    # Exclude Iceberg metadata files — only Parquet data files contain schema
    exclusions = ["metadata/**", "**.json", "**.avro"]
  }

  s3_target {
    path       = "s3://${aws_s3_bucket.data_lake.id}/gold/"
    exclusions = ["metadata/**", "**.json", "**.avro"]
  }

  schema_change_policy {
    # Merge new columns into existing tables — handles source schema additions
    update_behavior = "UPDATE_IN_DATABASE"
    # Log removed columns rather than dropping them — prevents data loss on empty partitions
    delete_behavior = "LOG"
  }

  configuration = jsonencode({
    Version = 1.0
    CrawlerOutput = {
      Partitions = { AddOrUpdateBehavior = "InheritFromTable" }
      Tables     = { AddOrUpdateBehavior = "MergeNewColumns" }
    }
    # Group files by S3 prefix into one table per prefix (not one per file)
    Grouping = {
      TableGroupingPolicy = "CombineCompatibleSchemas"
    }
  })
}

# ---- QuickSight (visualization) ----
#
# Connects QuickSight directly to the Athena workgroup and gold_keyword_performance
# table, enabling filters by engine, keyword, and date without any exports.
#
# Prerequisites (one-time manual steps, NOT managed by Terraform):
#   1. Subscribe to QuickSight Standard/Enterprise in the AWS console
#   2. Grant QuickSight IAM permissions to access Athena + S3
#      (QuickSight console → Manage QuickSight → Security & permissions)
#   3. Set variables: enable_quicksight = true, quicksight_username = "your-iam-username"
#
# DIRECT_QUERY mode: queries execute live against Athena — no SPICE ingestion cost.
# Data is always current without refresh scheduling.

resource "aws_quicksight_data_source" "athena" {
  count = var.enable_quicksight ? 1 : 0

  aws_account_id = data.aws_caller_identity.current.account_id
  data_source_id = "${var.project_name}-${var.environment}"
  name           = "Search Keyword Analyzer (${var.environment})"
  type           = "ATHENA"

  parameters {
    athena {
      work_group = aws_athena_workgroup.analytics.name
    }
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

# Gold layer dataset — revenue by search engine and keyword.
# DIRECT_QUERY: no SPICE cost, always reflects latest Lambda output.
# Add a date range filter in QuickSight UI to slice by ingestion_date.
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
      name            = "gold_keyword_performance"

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

output "state_machine_arn" {
  description = "Step Functions pipeline — triggered by S3 landing/ uploads via EventBridge"
  value       = aws_sfn_state_machine.pipeline.arn
}

output "cloudwatch_dashboard_url" {
  description = "Operations dashboard — Lambda health, Step Functions, Athena scan cost, KMS API calls"
  value       = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${aws_cloudwatch_dashboard.pipeline_ops.dashboard_name}"
}

output "budget_status" {
  description = "Monthly cost budget (set budget_alert_email variable to activate)"
  value       = length(aws_budgets_budget.monthly) > 0 ? "Active — alerts at 80% actual and 100% forecasted of $${var.monthly_budget_usd}/mo → ${var.budget_alert_email}" : "Not created — set budget_alert_email in terraform.tfvars to enable"
}

output "quicksight_dataset_arn" {
  description = "QuickSight dataset ARN for gold_keyword_performance (requires enable_quicksight=true)"
  value       = var.enable_quicksight ? aws_quicksight_data_set.gold_performance[0].arn : "QuickSight disabled — set enable_quicksight=true and quicksight_username in terraform.tfvars"
}

output "glue_crawler_name" {
  description = "Glue Crawler for automatic schema evolution — runs daily at 2am UTC"
  value       = aws_glue_crawler.schema_discovery.name
}

output "iceberg_table_init_sql" {
  description = "Run these in Athena after first deploy to initialize Iceberg table metadata"
  value       = <<-EOT
    -- Run once in Athena workgroup: ${aws_athena_workgroup.analytics.name}
    -- Database: ${aws_glue_catalog_database.analytics.name}

    CREATE TABLE IF NOT EXISTS ${aws_glue_catalog_database.analytics.name}.bronze_hits_masked
    WITH (table_type='ICEBERG', location='s3://${aws_s3_bucket.data_lake.id}/bronze/masked/', format='PARQUET', write_compression='SNAPPY', partitioning=ARRAY['day(ingestion_date)'])
    AS SELECT * FROM ${aws_glue_catalog_database.analytics.name}.bronze_hits_masked WHERE 1=0;

    CREATE TABLE IF NOT EXISTS ${aws_glue_catalog_database.analytics.name}.gold_keyword_performance
    WITH (table_type='ICEBERG', location='s3://${aws_s3_bucket.data_lake.id}/gold/', format='PARQUET', write_compression='SNAPPY')
    AS SELECT * FROM ${aws_glue_catalog_database.analytics.name}.gold_keyword_performance WHERE 1=0;
  EOT
}
