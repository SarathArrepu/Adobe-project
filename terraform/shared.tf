# ============================================================
# Shared infrastructure — used by all pipeline modules
# ============================================================

locals {
  glue_database_name = replace("${var.environment}_${var.project_name}", "-", "_")
}

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
        # Root delegation — IAM role policies control access for all principals.
        # Lambda roles get PII encrypt via their IAM policy (modules/pipeline/main.tf).
        Sid       = "EnableRootKeyAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
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
  name          = "${var.project_name}-${var.environment}"
  force_destroy = true
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
