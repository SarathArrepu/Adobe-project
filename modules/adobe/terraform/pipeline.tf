# ============================================================
# Adobe pipeline — pipeline-specific Terraform configuration.
#
# To add a new pipeline:
#   1. cp -r modules/adobe  modules/<source>
#   2. Rename modules/<source>/src/adobe/ → modules/<source>/src/<source>/
#   3. Update source_name, lambda_handler, bronze_columns, gold_columns below
#   4. scripts/build.sh && terraform -chdir=terraform apply
#
# Shared infrastructure (S3, KMS, Glue, Athena, EventBridge) is in terraform/shared.tf.
# The reusable pipeline Terraform module is in terraform/modules/pipeline/.
# Neither of those files should be copied or modified for a new pipeline.
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

  # Lambda entry point: <package_name>.handler.lambda_handler
  # The package name matches the src/<name>/ folder inside this module.
  lambda_handler = "adobe.handler.lambda_handler"

  # Lambda zip is pre-built by scripts/build.sh (or the CI/CD package job).
  # Terraform reads the hash to detect changes and trigger a function update.
  lambda_zip_path = "${path.module}/../dist/lambda.zip"
  lambda_zip_hash = filebase64sha256("${path.module}/../dist/lambda.zip")

  lambda_timeout_seconds = var.lambda_timeout_seconds
  lambda_memory_mb       = var.lambda_memory_mb

  # Bronze columns — hit-level TSV schema, column order matches source file exactly.
  # All types are string to avoid length truncation (SHA-256 hashes are 71 chars,
  # exceeding any varchar limit that would fit a raw IP).
  # ip and user_agent are PII: masked table hashes them, raw table keeps plaintext.
  bronze_columns = [
    { name = "hit_time_gmt", type = "int",       comment = "Unix timestamp (Int 11)" },
    { name = "date_time",    type = "timestamp",  comment = "Hit datetime in report suite timezone" },
    { name = "user_agent",   type = "string",     comment = "PII — sha256 hash in masked layer, plaintext in raw" },
    { name = "ip",           type = "string",     comment = "PII — sha256 hash in masked layer, plaintext in raw" },
    { name = "event_list",   type = "string",     comment = "Comma-separated Adobe Analytics event IDs; '1' = purchase" },
    { name = "geo_city",     type = "string",     comment = "" },
    { name = "geo_region",   type = "string",     comment = "" },
    { name = "geo_country",  type = "string",     comment = "" },
    { name = "pagename",     type = "string",     comment = "" },
    { name = "page_url",     type = "string",     comment = "" },
    { name = "product_list", type = "string",     comment = "Format: Category;Name;Qty;Revenue;CustomEvent;MerchEVar" },
    { name = "referrer",     type = "string",     comment = "" },
  ]

  # Gold columns — aggregated output, no PII
  gold_columns = [
    { name = "search_engine_domain", type = "string", comment = "" },
    { name = "search_keyword", type = "string", comment = "" },
    { name = "revenue", type = "double", comment = "" },
  ]
}
