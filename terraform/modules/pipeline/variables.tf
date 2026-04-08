# ---- Pipeline Module Variables ----
# Every pipeline (adobe, salesforce, etc.) declares these.
# Only source_name, lambda_handler, bronze_columns, and gold_columns differ per source.

variable "source_name" {
  description = "Short identifier for the data source (e.g. 'adobe', 'salesforce'). Used in resource names, S3 prefixes, and Glue table names."
  type        = string
}

variable "project_name" {
  description = "Project-level name prefix shared across all sources."
  type        = string
}

variable "environment" {
  description = "Deployment environment (dev, staging, prod)."
  type        = string
}

variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
}

variable "aws_account_id" {
  description = "AWS account ID."
  type        = string
}

# ---- Shared infrastructure (passed in from root module) ----

variable "s3_bucket_id" {
  description = "ID (name) of the shared data lake S3 bucket."
  type        = string
}

variable "s3_bucket_arn" {
  description = "ARN of the shared data lake S3 bucket."
  type        = string
}

variable "kms_key_arn" {
  description = "Standard data KMS key ARN — used for landing, masked bronze, gold, and Athena results."
  type        = string
}

variable "pii_kms_key_arn" {
  description = "Dedicated PII KMS key ARN — Lambda can encrypt, admin role can decrypt, developers cannot."
  type        = string
}

variable "glue_database_name" {
  description = "Glue Catalog database to register tables into."
  type        = string
}

variable "athena_workgroup_name" {
  description = "Athena workgroup for query cost controls."
  type        = string
}

# ---- Lambda packaging ----

variable "lambda_handler" {
  description = "Lambda handler string in 'module.function' format (e.g. 'adobe_handler.lambda_handler')."
  type        = string
}

variable "lambda_zip_path" {
  description = "Local path to the Lambda deployment zip file."
  type        = string
}

variable "lambda_zip_hash" {
  description = "Base64-encoded SHA256 hash of the zip file for change detection."
  type        = string
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

# ---- Schema definitions ----
# Define columns matching the TSV files your Lambda writes.
# bronze_columns: all fields including PII (ip, user_agent) — module creates both masked and raw tables.
# gold_columns:   aggregated output fields only, no PII.

variable "bronze_columns" {
  description = "Column definitions for bronze layer tables (masked and raw). Each object: {name, type, comment}."
  type = list(object({
    name    = string
    type    = string
    comment = string
  }))
}

variable "gold_columns" {
  description = "Column definitions for the gold layer table. Each object: {name, type, comment}."
  type = list(object({
    name    = string
    type    = string
    comment = string
  }))
}
