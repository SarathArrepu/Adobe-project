variable "aws_region" {
  description = "AWS region for all resources."
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment (stg, dev, prod)."
  default     = "stg"
}

variable "project_name" {
  description = "Project name prefix applied to all resource names."
  default     = "adobe"
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
