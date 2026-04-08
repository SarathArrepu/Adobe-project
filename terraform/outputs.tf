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
