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

  # Bronze columns — hit-level TSV schema Lambda writes.
  # Types follow Appendix A exactly: Int(11) → int, datetime() → timestamp,
  # Varchar(n) → varchar(n), text → string (unbounded, no length in spec).
  # ip and user_agent are PII: masked table hashes them, raw table keeps plaintext.
  bronze_columns = [
    { name = "hit_time_gmt", type = "int", comment = "Unix timestamp (Int 11)" },
    { name = "date_time", type = "timestamp", comment = "Hit datetime in report suite timezone" },
    { name = "ip", type = "varchar(20)", comment = "PII-pseudonymized: sha256 hash of original IP" },
    { name = "user_agent", type = "string", comment = "PII-pseudonymized: sha256 hash of original user agent" },
    { name = "event_list", type = "string", comment = "" },
    { name = "geo_city", type = "varchar(32)", comment = "" },
    { name = "geo_region", type = "varchar(32)", comment = "" },
    { name = "geo_country", type = "varchar(4)", comment = "" },
    { name = "pagename", type = "varchar(100)", comment = "" },
    { name = "page_url", type = "varchar(255)", comment = "" },
    { name = "product_list", type = "string", comment = "" },
    { name = "referrer", type = "varchar(255)", comment = "" },
  ]

  # Gold columns — aggregated output, no PII
  gold_columns = [
    { name = "search_engine_domain", type = "string", comment = "" },
    { name = "search_keyword", type = "string", comment = "" },
    { name = "revenue", type = "double", comment = "" },
  ]
}
