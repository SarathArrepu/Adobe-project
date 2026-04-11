# Architecture — Search Keyword Performance Analyzer

## Current State: Modular Multi-Source Pipeline

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Developer Workflow                           │
│                                                                     │
│  git push feature/X → PR → CI: tests + terraform plan → merge      │
│                                        terraform apply (main)       │
└──────────────────────────────────┬──────────────────────────────────┘
                                   │ deploys infrastructure
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         AWS Account                                 │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  S3: adobe-stg-{account}                                     │  │
│  │                                                              │  │
│  │  landing/adobe/     ◄─── aws s3 cp data.sql s3://.../       │  │
│  │       │                                                      │  │
│  │       │  S3 event (Object Created)                          │  │
│  │       ▼                                                      │  │
│  └───────┼──────────────────────────────────────────────────────┘  │
│          │                                                          │
│          ▼                                                          │
│  ┌──────────────────┐                                              │
│  │   EventBridge    │  Rule: prefix = landing/adobe/              │
│  │   (routing)      │  One rule per source — no Step Functions     │
│  └────────┬─────────┘                                              │
│           │                                                         │
│           ▼                                                         │
│  ┌──────────────────────────────────────────────────────┐      │
│  │   Lambda: adobe-adobe-stg                            │      │
│  │   Handler: adobe.handler.lambda_handler               │      │
│  │   Runtime: Python 3.12                               │      │
│  │                                                      │      │
│  │   1. Run DataQualityChecker — abort if ERROR found    │      │
│  │   2. Download landing file to /tmp                   │      │
│  │   3. Run SearchKeywordAnalyzer (attribution logic)   │      │
│  │   4. Write gold/   ──► S3 (no PII, standard KMS key)│      │
│  │   5. Write bronze/raw/   ──► S3 (PII KMS key)        │      │
│  │   6. Write bronze/masked/ ──► S3 (SHA-256, std key)  │      │
│  └──────────────────────────────────────────────────────┘      │
│           │                                                         │
│    ┌──────┼──────────────────────┐                                  │
│    ▼      ▼                     ▼                                   │
│  gold/ bronze/raw/        bronze/masked/                            │
│  (no PII) (PII KMS key — (SHA-256 hashed ip/user_agent             │
│           admin only)     standard KMS key — devs OK)              │
│    │                             │                                  │
│    └────────────┬────────────────┘                                  │
│                 ▼                                                    │
│  ┌──────────────────────────────────────────────┐                  │
│  │  AWS Glue Data Catalog                       │                  │
│  │                                              │                  │
│  │  Database: stg_adobe                         │                  │
│  │  ├── adobe_gold          (gold layer)        │                  │
│  │  ├── adobe_bronze_masked (dev accessible)    │                  │
│  │  └── adobe_bronze_raw    (admin only)        │                  │
│  │                                              │                  │
│  │  Glue Crawler (daily 2am UTC)                │                  │
│  │  └── Auto-detects new columns/tables         │                  │
│  └──────────────────┬───────────────────────────┘                  │
│                     ▼                                               │
│  ┌──────────────────────────────────┐                              │
│  │  Athena Workgroup                │                              │
│  │  adobe-stg                        │                              │
│  │  Limit: 100 MB/query scan        │                              │
│  └──────────────────────────────────┘                              │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐ │
│  │  Observability & Cost                                         │ │
│  │                                                               │ │
│  │  CloudWatch Dashboard  ──► Lambda metrics, Athena scan cost   │ │
│  │  CloudWatch Alarm      ──► Alert on Lambda errors             │ │
│  │  AWS Budgets           ──► Email at 80%/100% of $50/mo        │ │
│  │  CloudTrail            ──► KMS Decrypt audit log              │ │
│  └───────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Terraform Structure

Infrastructure is split into files by concern. Terraform merges all `.tf` files in the same directory — this is idiomatic HCL for a single root module. Shared infrastructure is created once; each pipeline source is an isolated module call.

```
terraform/
├── main.tf              ← Provider config + S3 remote-state backend
├── variables.tf         ← All root-module variable declarations + defaults
├── shared.tf            ← One-time shared infrastructure (see below)
├── observability.tf     ← CloudWatch dashboard, Budgets, QuickSight (optional)
├── outputs.tf           ← All root-module outputs
└── modules/
    └── pipeline/        ← Reusable per-source module (see below)
        ├── main.tf      ← All per-source resources
        ├── variables.tf ← Module input variable declarations
        └── outputs.tf   ← Module output declarations

modules/
└── adobe/               ← One folder per pipeline — copy to add a new source
    ├── src/adobe/       ← Python package (analyzer.py + handler.py)
    ├── terraform/
    │   └── pipeline.tf  ← Pipeline-specific Terraform; staged to terraform/ by CI/CD
    └── tests/           ← Module unit tests

scripts/
└── build.sh             ← Packages src/shared/ + modules/*/src/ into dist/lambda.zip
```

### `main.tf` — Provider and backend

| Block | Purpose |
|---|---|
| `terraform { required_version }` | Enforces Terraform ≥ 1.5.0 |
| `terraform { required_providers }` | Pins AWS provider to `~> 5.0` |
| `terraform { backend "s3" }` | Remote state in `tfstate-search-keyword-analyzer-{account}` — shared across CI and local runs |
| `provider "aws"` | Sets default region + applies `Project`, `Environment`, `ManagedBy` tags to every resource |
| `data "aws_caller_identity"` | Looks up the current account ID, used in ARN construction throughout |

### `variables.tf` — Root variables

| Variable | Default | Purpose |
|---|---|---|
| `aws_region` | `us-east-1` | Region for all resources |
| `environment` | `stg` | Name suffix on all resources (stg/dev/prod) |
| `project_name` | `adobe` | Prefix on all resource names |
| `lambda_timeout_seconds` | `300` | Lambda execution timeout |
| `lambda_memory_mb` | `512` | Lambda memory allocation |
| `log_retention_days` | `14` | CloudWatch log group retention |
| `landing_retention_days` | `60` | Days before landing/ objects expire |
| `bronze_retention_days` | `365` | Days before bronze/ objects expire |
| `gold_retention_days` | `365` | Days before gold/ objects expire |
| `athena_results_retention_days` | `7` | Days before Athena result objects expire |
| `athena_bytes_scanned_limit` | `104857600` (100 MB) | Per-query scan cost guard |
| `budget_alert_email` | `""` | Email for budget alerts — empty = no budget resource |
| `monthly_budget_usd` | `"50"` | Monthly spend threshold in USD |
| `enable_quicksight` | `false` | Provision QuickSight data source (requires subscription) |
| `quicksight_username` | `""` | QuickSight IAM user (required when `enable_quicksight = true`) |

### `shared.tf` — Shared infrastructure (created once)

| Resource | Name pattern | Purpose |
|---|---|---|
| `aws_kms_key.data_key` | `alias/{project}-{env}` | Standard encryption — landing, masked bronze, gold, Athena results |
| `aws_kms_key.pii_key` | `alias/{project}-pii-{env}` | PII-only key — Lambda can encrypt, admin can decrypt, developers cannot |
| `aws_s3_bucket.data_lake` | `{project}-{env}-{account}` | Single shared medallion data lake |
| `aws_s3_bucket_versioning` | — | Versioning enabled for accidental-delete recovery |
| `aws_s3_bucket_server_side_encryption_configuration` | — | Default SSE-KMS with data key |
| `aws_s3_bucket_public_access_block` | — | All 4 public-access block settings enabled |
| `aws_s3_bucket_policy` | — | Hard-deny on `bronze/raw/*` and `landing/*` for developer role (overrides IAM) |
| `aws_s3_bucket_lifecycle_configuration` | — | Landing→Glacier 30d, Bronze→IA 90d→Glacier 180d, Athena results 7d TTL |
| `aws_s3_bucket_notification` | — | EventBridge notifications enabled on bucket (each pipeline adds its own rule) |
| `aws_iam_role.admin_role` | `{project}-admin-{env}` | Full access including PII KMS decrypt — assume from account root |
| `aws_iam_role.developer_role` | `{project}-developer-{env}` | Masked bronze + gold only — no PII decrypt, no landing/raw access |
| `aws_glue_catalog_database.analytics` | `{env}_{project}` (underscored) | Shared Glue database for all pipeline tables |
| `aws_glue_data_catalog_encryption_settings` | — | Glue Data Catalog SSE-KMS with data key |
| `aws_athena_workgroup.analytics` | `{project}-{env}` | Query engine — 100 MB/query scan limit, results encrypted |

### `modules/<source>/terraform/pipeline.tf` — Pipeline-specific Terraform

Each pipeline owns its own `pipeline.tf` under `modules/<source>/terraform/`. CI/CD copies this file to `terraform/<source>_pipeline.tf` before `terraform init/apply` so Terraform merges it with the shared root module. `scripts/build.sh` provides the equivalent locally.

**Only 4 variables differ per source:**

| Variable | Adobe value | What to change for a new source |
|---|---|---|
| `source_name` | `"adobe"` | Short ID used in resource names and S3 prefixes |
| `lambda_handler` | `"adobe.handler.lambda_handler"` | Python dotted path to the handler function |
| `bronze_columns` | 12 hit-level TSV columns | Schema of the raw/masked TSV your Lambda writes |
| `gold_columns` | 3 aggregated columns | Schema of the output TSV your Lambda writes |

The Lambda zip is pre-built by `scripts/build.sh` (locally) or the CI/CD "Package Lambda" job. Terraform reads `filebase64sha256` of the zip to detect code changes — no `data "archive_file"` resource.

### `observability.tf` — Operations visibility and cost controls

| Resource | Purpose |
|---|---|
| `aws_cloudwatch_dashboard.pipeline_ops` | 9-widget dashboard: Lambda invocations, duration (avg+p99), errors+throttles, concurrency, EventBridge triggers, Athena scan bytes, KMS API calls, active alarms |
| `aws_budgets_budget.monthly` | Email alerts at 80% actual and 100% forecasted of `monthly_budget_usd`; only created when `budget_alert_email` is set |
| `aws_quicksight_data_source.athena` | QuickSight Athena connection (created only when `enable_quicksight = true`) |
| `aws_quicksight_data_set.gold_performance` | Direct-query dataset over the gold table (created only when `enable_quicksight = true`) |

### `outputs.tf` — Root outputs

| Output | Value |
|---|---|
| `s3_bucket` | Bucket name — used in upload commands |
| `athena_database` | Glue database name for Athena queries |
| `athena_workgroup` | Workgroup name |
| `admin_role_arn` | ARN to assume for PII data access |
| `developer_role_arn` | ARN to assume for standard development |
| `pii_kms_key_arn` | PII KMS key ARN |
| `cloudwatch_dashboard_url` | Direct link to the ops dashboard |
| `budget_status` | Human-readable budget config summary |
| `quicksight_status` | QuickSight ARN or "Disabled" message |
| `adobe_pipeline` | Map of all Adobe pipeline resource names + a ready-to-run S3 upload command |
| `sample_athena_queries` | Ready-to-run SQL for gold, masked bronze, and purchase queries |

---

### `modules/pipeline/` — Reusable per-source module

Each call to this module creates one fully-isolated pipeline for a data source. No shared infrastructure is modified.

#### `modules/pipeline/main.tf` — Resources created per source

| Resource | Name pattern | Purpose |
|---|---|---|
| `aws_iam_role.lambda_role` | `{project}-lambda-{source}-{env}` | Lambda execution role (least-privilege) |
| `aws_iam_role_policy.lambda_s3_kms` | `s3-kms-access` | Read landing, write bronze+gold, standard KMS encrypt/decrypt, PII KMS encrypt-only |
| `aws_iam_role_policy_attachment.lambda_logs` | — | Attaches `AWSLambdaBasicExecutionRole` for CloudWatch Logs |
| `aws_lambda_function.processor` | `{project}-{source}-{env}` | The pipeline Lambda (Python 3.12) — env vars: `KMS_KEY_ARN`, `PII_KMS_KEY_ARN`, `SOURCE_NAME` |
| `aws_lambda_permission.allow_eventbridge` | — | Grants EventBridge permission to invoke the Lambda |
| `aws_cloudwatch_event_rule.landing_upload` | `{project}-{source}-{env}-landing-upload` | EventBridge rule: S3 Object Created on `landing/{source}/` prefix |
| `aws_cloudwatch_event_target.invoke_lambda` | — | Routes matched events to the Lambda with S3 key/bucket wrapped in the Records format |
| `aws_cloudwatch_log_group.lambda_logs` | `/aws/lambda/{function-name}` | 14-day log retention |
| `aws_cloudwatch_metric_alarm.lambda_errors` | `{project}-{source}-{env}-errors` | Alarm fires when Lambda error count > 0 over 5 minutes |
| `aws_glue_catalog_table.bronze_masked` | `{source}_bronze_masked` | Hive external TSV table — SHA-256 hashed PII, developer accessible |
| `aws_glue_catalog_table.bronze_raw` | `{source}_bronze_raw` | Hive external TSV table — plaintext PII, admin only, tagged `data_classification=restricted-pii` |
| `aws_glue_catalog_table.gold` | `{source}_gold` | Hive external TSV table — aggregated output, no PII |
| `aws_iam_role.glue_crawler_role` | `{project}-crawler-{source}-{env}` | Glue Crawler IAM role |
| `aws_glue_crawler.schema_discovery` | `{project}-{source}-{env}-schema` | Crawls masked bronze + gold daily at 02:00 UTC for automatic schema evolution |

#### `modules/pipeline/variables.tf` — Module inputs

| Variable | Required | Purpose |
|---|---|---|
| `source_name` | Yes | Short source ID (`"adobe"`) — drives all resource names and S3 prefixes |
| `project_name` | Yes | Passed from root — used in resource name prefix |
| `environment` | Yes | Passed from root |
| `aws_region` | Yes | Passed from root |
| `aws_account_id` | Yes | Passed from root (`data.aws_caller_identity`) |
| `s3_bucket_id` | Yes | Shared S3 bucket name |
| `s3_bucket_arn` | Yes | Shared S3 bucket ARN for IAM policy resources |
| `kms_key_arn` | Yes | Standard data KMS key ARN |
| `pii_kms_key_arn` | Yes | PII-only KMS key ARN |
| `glue_database_name` | Yes | Shared Glue database to register tables into |
| `athena_workgroup_name` | Yes | Shared Athena workgroup |
| `lambda_handler` | Yes | Python dotted-path handler string |
| `lambda_zip_path` | Yes | Local path to `dist/lambda.zip` |
| `lambda_zip_hash` | Yes | SHA-256 of the zip (forces Lambda update when code changes) |
| `lambda_timeout_seconds` | No (300) | Timeout in seconds |
| `lambda_memory_mb` | No (512) | Memory in MB |
| `bronze_columns` | Yes | `list({name, type, comment})` — TSV schema for bronze tables |
| `gold_columns` | Yes | `list({name, type, comment})` — TSV schema for gold table |

#### `modules/pipeline/outputs.tf` — Module outputs (consumed by root `outputs.tf`)

| Output | Value |
|---|---|
| `lambda_function_name` | Lambda function name |
| `lambda_function_arn` | Lambda function ARN |
| `lambda_role_arn` | Lambda execution IAM role ARN |
| `bronze_masked_table` | Glue table name for masked bronze |
| `bronze_raw_table` | Glue table name for raw bronze |
| `gold_table` | Glue table name for gold |
| `glue_crawler_name` | Glue Crawler name |
| `lambda_error_alarm_arn` | CloudWatch error alarm ARN |
| `trigger_command` | Ready-to-run `aws s3 cp` command to trigger this pipeline |

**Adding a new pipeline** = add one `module` block in `pipelines.tf` + create `src/pipelines/<source>/handler.py`. Nothing in `shared.tf`, `main.tf`, or the module itself changes.

---

## Python Package Structure

```
src/
└── shared/                          ← reused by all pipelines
    ├── __init__.py
    ├── dq_checker.py                ← data quality checks (ERROR/WARN/INFO)
    └── base_handler.py              ← S3/KMS utilities (archive_raw, archive_masked, hash_pii)

modules/adobe/src/
└── adobe/                           ← Adobe-specific package
    ├── __init__.py
    ├── analyzer.py                  ← SearchKeywordAnalyzer (attribution logic)
    └── handler.py                   ← lambda_handler entry point
```

Lambda handler string: `adobe.handler.lambda_handler`

The Lambda zip is staged by merging `src/shared/` → `shared/` and `modules/*/src/` → root, so all packages are importable at the top level inside the zip.

---

## Data Flow Detail

```
Input: hit-level TSV (landing/adobe/data.sql)
│
│  Columns: hit_time_gmt, date_time, ip, user_agent, event_list,
│           geo_city, geo_region, geo_country, pagename, page_url,
│           product_list, referrer
│
├──► gold/  (SearchKeywordAnalyzer output)
│     Columns: search_engine_domain, search_keyword, revenue
│     PII: NONE — engine/keyword/revenue only
│     KMS key: standard data key
│     Access: everyone (admin, developer)
│
├──► bronze/raw/  (original file, unmodified)
│     Columns: all 12 columns (ip and user_agent in plaintext)
│     PII: PRESENT — plaintext ip + user_agent
│     KMS key: dedicated PII key (admin-only kms:Decrypt)
│     Access: admin role only
│
└──► bronze/masked/  (pseudonymized copy)
      Columns: all 12 columns (ip and user_agent SHA-256 hashed)
      PII: PSEUDONYMIZED — sha256: prefix marks hashed fields
      KMS key: standard data key
      Access: developer + admin roles
```

---

## Security Model

```
                 ┌─────────────┐  ┌───────────────┐  ┌──────────┐
                 │  Admin Role │  │ Developer Role│  │  Lambda  │
                 └──────┬──────┘  └───────┬───────┘  └────┬─────┘
                        │                 │                │
KMS PII Key       Decrypt ✓          Decrypt ✗       Encrypt ✓
KMS Data Key      Decrypt ✓          Decrypt ✓       Decrypt ✓
                        │                 │                │
bronze/raw/         Read ✓           Read ✗ (3 layers) Write ✓
bronze/masked/      Read ✓           Read ✓            Write ✓
gold/               Read ✓           Read ✓            Write ✓
landing/            Read ✓           Read ✗            Read ✓

3 denial layers on bronze/raw/ for developers:
  1. IAM role policy: explicit Deny on s3:*
  2. S3 bucket policy: Deny overrides any IAM Allow
  3. KMS PII key: no kms:Decrypt → S3 refuses to serve object
```

---

## Medallion Layer Summary

| Layer | S3 Prefix | Contents | Retention | Access |
|---|---|---|---|---|
| Landing | `landing/adobe/` | Raw uploads (trigger zone) | 60 days → Glacier | Lambda only |
| Bronze Raw | `bronze/raw/` | Original data, plaintext PII | 1 year → Glacier | Admin only |
| Bronze Masked | `bronze/masked/` | SHA-256 hashed ip/user_agent | 1 year → Glacier | Developer + Admin |
| Gold | `gold/` | Aggregated output, no PII | 1 year | Everyone |
| Athena Results | `athena-results/` | Query scratch space | 7 days | Developer + Admin |

---

## Adding a New Data Source

1. **Copy the adobe module folder**
   ```bash
   cp -r modules/adobe modules/<source>
   ```

2. **Rename the Python package**
   ```bash
   mv modules/<source>/src/adobe modules/<source>/src/<source>
   ```

3. **Update the module** — edit `modules/<source>/src/<source>/analyzer.py` (transformation logic) and `modules/<source>/src/<source>/handler.py` (imports).

4. **Update Terraform** — in `modules/<source>/terraform/pipeline.tf` change `source_name`, `lambda_handler`, `bronze_columns`, and `gold_columns`.

5. **Build and deploy**
   ```bash
   ./scripts/build.sh
   terraform -chdir=terraform apply
   ```

CI/CD auto-discovers the new module (loops `modules/*/`). No changes to shared Terraform files needed. Each source gets isolated Lambda, Glue tables (`<source>_bronze_masked`, `<source>_bronze_raw`, `<source>_gold`), and EventBridge rule.

---

## Infrastructure Inventory

| Resource | Name | Purpose |
|---|---|---|
| S3 Bucket | `adobe-stg-{account}` | Shared medallion data lake |
| KMS Key | `alias/adobe-stg` | Standard encryption (all layers) |
| KMS Key | `alias/adobe-pii-stg` | PII-only key (admin decrypt) |
| Lambda | `adobe-adobe-stg` | Adobe pipeline processor |
| IAM Role | `adobe-lambda-adobe-stg` | Lambda execution (per pipeline) |
| IAM Role | `adobe-admin-stg` | Admin access (full PII) |
| IAM Role | `adobe-developer-stg` | Developer access (masked + gold) |
| EventBridge Rule | `adobe-adobe-stg-landing-upload` | Routes S3 events to Lambda |
| Glue Database | `stg_adobe` | Schema registry |
| Glue Tables | `adobe_bronze_masked`, `adobe_bronze_raw`, `adobe_gold` | Athena queryable |
| Glue Crawler | `adobe-adobe-stg-schema` | Daily schema evolution |
| Athena Workgroup | `adobe-stg` | Query engine (100 MB scan limit) |
| CloudWatch Dashboard | `adobe-stg` | Ops metrics |
| AWS Budgets | `adobe-monthly-stg` | Cost alerts at $40/$50 |
