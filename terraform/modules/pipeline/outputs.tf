output "lambda_function_name" {
  description = "Name of the Lambda function for this source."
  value       = aws_lambda_function.processor.function_name
}

output "lambda_function_arn" {
  description = "ARN of the Lambda function."
  value       = aws_lambda_function.processor.arn
}

output "lambda_role_arn" {
  description = "IAM role ARN used by the Lambda function."
  value       = aws_iam_role.lambda_role.arn
}

output "bronze_masked_table" {
  description = "Glue table name for the masked bronze layer (developer accessible)."
  value       = aws_glue_catalog_table.bronze_masked.name
}

output "bronze_raw_table" {
  description = "Glue table name for the raw bronze layer (admin only — plaintext PII)."
  value       = aws_glue_catalog_table.bronze_raw.name
}

output "gold_table" {
  description = "Glue table name for the gold layer (no PII)."
  value       = aws_glue_catalog_table.gold.name
}

output "glue_crawler_name" {
  description = "Name of the Glue Crawler for automatic schema evolution."
  value       = aws_glue_crawler.schema_discovery.name
}

output "lambda_error_alarm_arn" {
  description = "ARN of the CloudWatch alarm for Lambda errors."
  value       = aws_cloudwatch_metric_alarm.lambda_errors.arn
}

output "trigger_command" {
  description = "AWS CLI command to trigger this pipeline."
  value       = "aws s3 cp <your-file> s3://${var.s3_bucket_id}/landing/${var.source_name}/<filename>"
}
