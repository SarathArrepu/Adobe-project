# ============================================================
# Lambda packaging
# All Python source files in src/ are bundled into one zip.
# Lambda handler variable on each module call selects the entry point.
# Adding a new pipeline: create src/pipelines/<source>/handler.py,
# add a module block below, run terraform apply.
# ============================================================

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/../dist/lambda.zip"
  excludes    = ["__pycache__", "tests"]
}

# ============================================================
# Adobe pipeline
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
  lambda_handler        = "pipelines.adobe.handler.lambda_handler"
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

# ============================================================
# To add a new source (e.g. Salesforce):
#   1. Create src/pipelines/salesforce/handler.py
#      (copy adobe handler, update transformation logic only)
#   2. Copy the block below, uncomment, set source_name + columns
#   3. terraform apply — Lambda, Glue tables, EventBridge rule all created
# ============================================================

# module "salesforce_pipeline" {
#   source = "./modules/pipeline"
#
#   source_name    = "salesforce"
#   lambda_handler = "pipelines.salesforce.handler.lambda_handler"
#
#   bronze_columns = [
#     { name = "contact_id", type = "string", comment = "" },
#     { name = "event_date", type = "string", comment = "" },
#     { name = "revenue",    type = "double",  comment = "" },
#   ]
#   gold_columns = [
#     { name = "campaign", type = "string", comment = "" },
#     { name = "revenue",  type = "double",  comment = "" },
#   ]
#
#   # Shared infrastructure — identical for every pipeline
#   project_name          = var.project_name
#   environment           = var.environment
#   aws_region            = var.aws_region
#   aws_account_id        = data.aws_caller_identity.current.account_id
#   s3_bucket_id          = aws_s3_bucket.data_lake.id
#   s3_bucket_arn         = aws_s3_bucket.data_lake.arn
#   kms_key_arn           = aws_kms_key.data_key.arn
#   pii_kms_key_arn       = aws_kms_key.pii_key.arn
#   glue_database_name    = aws_glue_catalog_database.analytics.name
#   athena_workgroup_name = aws_athena_workgroup.analytics.name
#   lambda_zip_path       = data.archive_file.lambda_zip.output_path
#   lambda_zip_hash       = data.archive_file.lambda_zip.output_base64sha256
#   lambda_timeout_seconds = var.lambda_timeout_seconds
#   lambda_memory_mb      = var.lambda_memory_mb
# }
