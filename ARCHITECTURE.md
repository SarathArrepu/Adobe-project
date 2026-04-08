# Architecture — Search Keyword Performance Analyzer

## Current State: Single-Source Pipeline (Adobe)

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
│  │  S3: search-keyword-analyzer-dev-{account}                   │  │
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
│  ┌──────────────────────────────────────────────────────────┐      │
│  │   Lambda: search-keyword-analyzer-adobe-dev              │      │
│  │   Handler: adobe_handler.lambda_handler                  │      │
│  │   Runtime: Python 3.12                                   │      │
│  │                                                          │      │
│  │   1. Download landing file to /tmp                       │      │
│  │   2. Run SearchKeywordAnalyzer (attribution logic)       │      │
│  │   3. Write gold/   ──► S3 (no PII, standard KMS key)    │      │
│  │   4. Write bronze/raw/   ──► S3 (PII KMS key)           │      │
│  │   5. Write bronze/masked/ ──► S3 (SHA-256 hash, std key)│      │
│  └──────────────────────────────────────────────────────────┘      │
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
│  │  Database: search_keyword_analyzer_dev       │                  │
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
│  │  search-keyword-analyzer-dev     │                              │
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
| Landing | `landing/` | Raw uploads (trigger zone) | 60 days → Glacier | Lambda only |
| Bronze Raw | `bronze/raw/` | Original data, plaintext PII | 1 year → Glacier | Admin only |
| Bronze Masked | `bronze/masked/` | SHA-256 hashed ip/user_agent | 1 year → Glacier | Developer + Admin |
| Gold | `gold/` | Aggregated output, no PII | 1 year | Everyone |
| Athena Results | `athena-results/` | Query scratch space | 7 days | Developer + Admin |

---

## Adding a New Data Source

1. **Create the handler** — copy `src/adobe_handler.py` → `src/<source>_handler.py`, update transformation logic
2. **Add Terraform module block** — copy the `module "adobe_pipeline"` block in `terraform/main.tf`, change `source_name`, `lambda_handler`, `bronze_columns`, `gold_columns`
3. **Run CI** — push to a PR → CI runs `terraform apply` → new Lambda, Glue tables, EventBridge rule created automatically
4. **Upload data** — `aws s3 cp <file> s3://<bucket>/landing/<source>/<file>`

No shared infrastructure changes needed. Each source is fully isolated.

---

## Infrastructure Inventory

| Resource | Name | Purpose |
|---|---|---|
| S3 Bucket | `search-keyword-analyzer-dev-{account}` | Shared medallion data lake |
| KMS Key | `alias/search-keyword-analyzer-dev` | Standard encryption (all layers) |
| KMS Key | `alias/search-keyword-analyzer-pii-dev` | PII-only key (admin decrypt) |
| Lambda | `search-keyword-analyzer-adobe-dev` | Adobe pipeline processor |
| EventBridge Rule | `…-adobe-…-landing-upload` | Routes S3 events to Lambda |
| Glue Database | `search_keyword_analyzer_dev` | Schema registry |
| Glue Tables | `adobe_bronze_masked`, `adobe_bronze_raw`, `adobe_gold` | Athena queryable |
| Glue Crawler | `…-adobe-…-schema` | Daily schema evolution |
| Athena Workgroup | `search-keyword-analyzer-dev` | Query engine (100 MB scan limit) |
| CloudWatch Dashboard | `search-keyword-analyzer-dev` | Ops metrics |
| AWS Budgets | `search-keyword-analyzer-monthly-dev` | Cost alerts at $40/$50 |
| IAM | `admin-role`, `developer-role`, `lambda-adobe-role` | RBAC |
