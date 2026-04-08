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
