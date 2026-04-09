# Search Keyword Performance Analyzer

> Adobe Data Engineer Assessment — Python + AWS Lambda + Terraform + GitHub Actions

## Business Problem

The client wants to understand **how much revenue is driven by external search engines** (Google, Yahoo, Bing/MSN) and **which search keywords perform best** based on revenue.

This application processes Adobe Analytics hit-level data, attributes purchase revenue to the originating search engine and keyword, and outputs a ranked report.

## Results (from provided sample data)

| Search Engine Domain | Search Keyword | Revenue |
|---|---|---|
| google.com | Ipod | $290.00 |
| bing.com | Zune | $250.00 |
| google.com | ipod | $190.00 |

Three visitors arrived from search engines. One Yahoo visitor ("cd player") browsed but did not purchase — correctly excluded from results.

---

## Architecture

```
                    ┌─────────────────────────────────────────────────┐
                    │              GitHub Actions CI/CD                │
                    │                                                  │
                    │  Push/PR → Test → Package → Terraform Apply      │
                    └──────────────────────┬──────────────────────────┘
                                           │ deploys
                                           ▼
┌──────────┐   upload    ┌─────────────────────────────────────────────┐
│ data.sql │ ──────────► │  S3: landing/adobe/data.sql                 │
└──────────┘             │      │                                      │
                         │      │ EventBridge (Object Created)         │
                         │      ▼                                      │
                         │  Lambda: pipelines.adobe.handler            │
                         │      │                                      │
                         │      ├── DQ checks (abort if ERROR)         │
                         │      ├── bronze/raw/    (PII KMS key)       │
                         │      ├── bronze/masked/ (SHA-256 hashed PII)│
                         │      └── gold/          (no PII, output)    │
                         │                                             │
                         │  Glue Catalog (stg_adobe) ──► Athena        │
                         │  KMS (two keys: data + PII)                 │
                         │  CloudWatch (logs + error alarm)            │
                         └─────────────────────────────────────────────┘
```

### Medallion Lakehouse Layers

| Layer | S3 Prefix | Contents | Retention |
|---|---|---|---|
| Landing | `landing/adobe/` | Raw uploads (trigger zone) | 60 days |
| Bronze Raw | `bronze/raw/` | Original data, plaintext PII, PII KMS key | 1 year → Glacier |
| Bronze Masked | `bronze/masked/` | SHA-256 hashed ip/user_agent | 1 year → Glacier |
| Gold | `gold/` | Aggregated output, no PII | 1 year |
| Athena Results | `athena-results/` | Query scratch space | 7 days |

---

## Attribution Logic

1. **Visitor Identification** — Each unique IP address is a distinct visitor
2. **Search Engine Detection** — Referrer URL is parsed to extract engine domain and keyword
3. **Revenue Attribution** — On purchase event (`event_list` contains `1`), revenue from `product_list` is attributed to the last search engine that brought the visitor
4. **Aggregation** — Revenue summed per (engine, keyword) pair, sorted descending

---

## Data Quality Checks

Before any data is written to S3, `DataQualityChecker` validates the input file. On any **ERROR**-level issue the Lambda aborts and nothing is stored.

| Check | Severity | Description |
|---|---|---|
| `MISSING_REQUIRED_COLUMNS` | ERROR | Required columns absent from header — pipeline cannot run |
| `EMPTY_FILE` | ERROR | No data rows found |
| `MISSING_APPENDIX_A_COLUMNS` | WARN | Optional Appendix A columns absent (enrichment fields will be missing) |
| `MISSING_IP` | WARN | Empty IP — row cannot be session-stitched, will be skipped |
| `INVALID_HIT_TIME` | WARN | `hit_time_gmt` is not a valid Unix timestamp (non-integer or out of 2000–2100 range) |
| `INVALID_IP_FORMAT` | WARN | IP is not a valid IPv4 address |
| `DUPLICATE_HIT` | WARN | Same `(hit_time_gmt, ip)` seen more than once — possible replay |
| `PURCHASE_NO_PRODUCT` | WARN | Purchase event (1) present but `product_list` is empty — revenue will be zero |
| `PRODUCT_REVENUE_NO_PURCHASE` | WARN | `product_list` has revenue > 0 but no purchase event — revenue silently dropped |
| `NEGATIVE_REVENUE` | WARN | Revenue field is negative |
| `MALFORMED_PRODUCT_LIST` | WARN | `product_list` cannot be parsed (too few fields or non-numeric revenue) |
| `UNKNOWN_EVENT_ID` | INFO | `event_list` contains an unrecognised event ID |

WARN and INFO issues are logged but do not abort the pipeline. The provided `data.sql` passes all ERROR-level checks (0 errors, 0 warnings).

---

## Project Structure

```
.
├── src/
│   ├── shared/
│   │   ├── __init__.py
│   │   ├── search_keyword_analyzer.py   # Core attribution logic
│   │   ├── dq_checker.py                # Data quality checks (10 checks, ERROR/WARN/INFO)
│   │   └── base_handler.py              # Shared S3/KMS/PII utilities
│   └── pipelines/
│       └── adobe/
│           ├── __init__.py
│           └── handler.py               # Adobe Lambda entry point
├── tests/
│   ├── test_analyzer.py                 # Analyzer unit tests (26 tests)
│   └── test_dq_checker.py               # DQ checker unit tests (32 tests)
├── notebooks/
│   └── search_keyword_analysis.ipynb    # Revenue charts (bar, pie, grouped bar)
├── terraform/
│   ├── main.tf                          # Provider config + S3 remote-state backend
│   ├── variables.tf                     # All root-module variable declarations + defaults
│   ├── shared.tf                        # One-time shared infra: S3 bucket, KMS (2 keys),
│   │                                    #   IAM admin/developer roles, Glue DB, Athena workgroup
│   ├── pipelines.tf                     # Lambda zip packaging + one module block per source
│   │                                    #   (add a new module block here to add a new source)
│   ├── observability.tf                 # CloudWatch dashboard + Budgets + QuickSight (optional)
│   ├── outputs.tf                       # All root outputs incl. sample Athena queries
│   └── modules/
│       └── pipeline/                    # Reusable per-source module — instantiated once per source
│           ├── main.tf                  # Lambda + IAM role + EventBridge rule/target +
│           │                            #   CloudWatch logs/alarm + 3 Glue tables + Crawler
│           ├── variables.tf             # 18 input variables (source_name, columns, KMS ARNs…)
│           └── outputs.tf               # 9 outputs (Lambda name, Glue table names, alarm ARN…)
├── data/
│   └── data.sql                         # Sample hit-level data
├── .github/
│   └── workflows/
│       └── ci-cd.yml                    # GitHub Actions pipeline
├── output/                              # Generated reports (gitignored)
├── dist/                                # Lambda zip artifacts (gitignored)
├── scripts/
│   ├── demo.sh                          # One-command full demo
│   └── update_ppt.py                    # Presentation helper
├── docs/
│   ├── ARCHITECTURE.md                  # Current architecture detail
│   ├── FUTURE_ARCHITECTURE.md           # Scaling roadmap
│   ├── RUNBOOK.md                       # Full operational guide
│   ├── DEPLOYMENT.md                    # Quick deployment reference
│   ├── architecture.html                # Visual architecture diagram
│   └── Adobe_Assessment_Presentation.pptx
├── README.md
└── requirements.txt                     # No external dependencies
```

---

## Quick Start

### Local (no AWS required)

```bash
# Run all tests (58 tests: 26 analyzer + 32 DQ checker)
PYTHONPATH=src python -m unittest tests.test_analyzer tests.test_dq_checker -v

# Run the analyzer locally and write output
mkdir -p output
PYTHONPATH=src python src/shared/search_keyword_analyzer.py data/data.sql -o output/
```

### AWS Deployment

**Prerequisites:** AWS CLI configured, Terraform installed

```bash
cd terraform
terraform init
terraform apply
```

Then trigger the pipeline:

```bash
# Get bucket name from terraform output
BUCKET=$(cd terraform && terraform output -raw s3_bucket)

# Upload data file → triggers Lambda automatically
aws s3 cp data/data.sql s3://$BUCKET/landing/adobe/data.sql

# Wait ~20 seconds, then check output
aws s3 ls s3://$BUCKET/gold/
```

### Full Demo (one command)

```bash
chmod +x scripts/demo.sh && ./scripts/demo.sh
```

---

## GitHub Actions CI/CD

Every push to `main` runs the full pipeline automatically:

```
Push / PR / Manual trigger
        │
        ▼
  ┌─────────────┐
  │ Unit Tests  │  python3 -m unittest (no AWS needed)
  └──────┬──────┘
         │ pass
         ▼
  ┌─────────────┐
  │Package Lambda│  zip src/ → GitHub artifact (30-day retention)
  └──────┬──────┘
         │
    ┌────┴──────┐
    ▼           ▼
 [PR only]  [push to main]
 Terraform   Terraform
   Plan       Apply
(PR comment) (live deploy)
```

### Branch Protection

- Direct pushes to `main` are **blocked**
- All changes require a **Pull Request** with 1 approval
- **Unit Tests** and **Package Lambda** CI jobs must pass before merge
- Force pushes and branch deletion are disabled

### Developer Workflow

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full git workflow rules and hook setup. Summary:

```bash
# 1. Start from a fresh main
git checkout main && git pull --rebase origin main

# 2. Create a feature branch
git checkout -b feature/my-change

# 3. Stay in sync — run this regularly while working
git fetch origin && git rebase origin/main

# 4. Commit (pre-commit hook verifies you're not behind remote)
git add <files> && git commit -m "feat: description"

# 5. Push (pre-push hook checks for conflicts with main before allowing)
git push -u origin feature/my-change

# 6. Open PR
gh pr create --title "My change" --body "Description"
# After review + CI pass → merge via GitHub UI
```

> **One-time hook setup per clone:** `git config core.hooksPath .githooks`

---

## AWS Infrastructure

Shared infrastructure lives in `terraform/shared.tf`. Per-pipeline resources are created by `terraform/modules/pipeline/` called from `terraform/pipelines.tf`.

> **Full Terraform documentation** — every file, variable, resource, and module output is documented in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#terraform-structure).

| Resource | Name | Purpose |
|---|---|---|
| S3 Bucket | `search-keyword-analyzer-dev-{account}` | Shared medallion data lake |
| Lambda | `search-keyword-analyzer-adobe-dev` | Adobe processing engine |
| IAM Role (Lambda) | `search-keyword-analyzer-lambda-adobe-dev` | Least-privilege execution role |
| IAM Role (Admin) | `search-keyword-analyzer-admin-dev` | Full PII access |
| IAM Role (Developer) | `search-keyword-analyzer-developer-dev` | Masked bronze + gold access |
| KMS Key | `alias/search-keyword-analyzer-dev` | Standard encryption (all layers) |
| KMS Key | `alias/search-keyword-analyzer-pii-dev` | PII-only key (admin decrypt only) |
| Glue Database | `stg_adobe` | Schema registry for all pipelines |
| Glue Tables | `adobe_bronze_masked`, `adobe_bronze_raw`, `adobe_gold` | Athena queryable tables |
| Glue Crawler | `search-keyword-analyzer-adobe-dev-schema` | Daily schema evolution |
| Athena Workgroup | `search-keyword-analyzer-dev` | Query engine (100 MB scan limit) |
| CloudWatch | `/aws/lambda/search-keyword-analyzer-adobe-dev` | Logs + error alarm |

### Security & PII Protection

#### Encryption

| Layer | Mechanism | Key |
|---|---|---|
| S3 files (all layers) | SSE-KMS | `alias/search-keyword-analyzer-dev` |
| `bronze/raw/` (PII data) | SSE-KMS with **dedicated PII key** | `alias/search-keyword-analyzer-pii-dev` |
| Glue Data Catalog | SSE-KMS | `alias/search-keyword-analyzer-dev` |
| Athena query results | SSE-KMS | `alias/search-keyword-analyzer-dev` |
| KMS key rotation | Annual, automatic | Both keys |

#### PII Handling

The dataset contains `ip` (direct PII) and `user_agent` (quasi-identifier). The pipeline applies **three enforcement layers** to prevent developer exposure:

```
bronze/raw/     — plaintext ip/user_agent, PII KMS key (admin role only)
bronze/masked/  — SHA-256 hashed ip/user_agent, standard KMS key (developer accessible)
gold/           — no PII at all (engine / keyword / revenue only)
```

| Role | bronze/raw | bronze/masked | gold | PII KMS Decrypt |
|---|---|---|---|---|
| Admin | Read/Write | Read/Write | Read/Write | Yes |
| Developer | **Denied** (3 layers) | Read | Read | No |
| Lambda | Write-only | Write | Write | No (encrypt only) |

#### Additional Controls

- S3 public access fully blocked (all 4 settings)
- Lambda IAM role uses least-privilege — PII key allows encrypt but not decrypt
- S3 bucket policy hard-denies developer role on `bronze/raw/*` (overrides any IAM Allow)
- GitHub secrets for CI/CD credentials (never hardcoded)
- All KMS API calls (especially `kms:Decrypt` on PII key) logged in CloudTrail for audit

---

## Querying Results with Athena

**Console:** AWS → Athena → Workgroup: `search-keyword-analyzer-dev` → Database: `stg_adobe`

```sql
-- Top keywords by revenue
SELECT * FROM stg_adobe.adobe_gold ORDER BY revenue DESC;

-- Unique visitors by page (masked bronze — no real IPs)
SELECT pagename, COUNT(*) AS hits
FROM stg_adobe.adobe_bronze_masked
GROUP BY pagename ORDER BY hits DESC;

-- Purchase events only
SELECT date_time, ip, geo_city, product_list
FROM stg_adobe.adobe_bronze_masked
WHERE event_list LIKE '%1%';
```

**CLI:**
```bash
aws athena start-query-execution \
  --query-string "SELECT * FROM stg_adobe.adobe_gold ORDER BY revenue DESC" \
  --work-group "adobe-stg" \
  --query-execution-context "Database=stg_adobe"
```

---

## Adding a New Pipeline

The module in `terraform/modules/pipeline/` is reusable. To add a new source (e.g. `<source>`):

1. Create `src/pipelines/<source>/__init__.py` (empty)
2. Create `src/pipelines/<source>/handler.py` (copy adobe handler, update transformation logic)
3. Add to `terraform/pipelines.tf`:
```hcl
module "<source>_pipeline" {
  source         = "./modules/pipeline"
  source_name    = "<source>"
  lambda_handler = "pipelines.<source>.handler.lambda_handler"
  bronze_columns = [ ... ]
  gold_columns   = [ ... ]
  # all shared vars identical to adobe_pipeline block
}
```
4. `terraform apply` — Lambda, Glue tables (`<source>_bronze_masked`, `<source>_bronze_raw`, `<source>_gold`), EventBridge rule all created automatically.

---

## Scalability

The current application streams the file row by row (`csv.DictReader`), so memory usage is O(unique visitors), not O(file size).

For **10 GB+ files**:

| Approach | When | How |
|---|---|---|
| Chunked multiprocessing | 10–50 GB, single machine | Split file, process in parallel, merge |
| AWS Glue (PySpark) | 50+ GB, serverless | Managed Spark, auto-scaling, S3-native |
| EMR | Very large recurring jobs | Full Spark cluster, maximum control |
| Kinesis Streams | Real-time hits | Process as events arrive, no batching |

---

## Design Decisions

1. **IP as visitor ID** — The dataset has no cookie/visitor ID. In production Adobe Analytics data, `visid_high + visid_low` would be used instead.

2. **Keyword case sensitivity** — "Ipod" and "ipod" are treated separately (raw data preserved). In production, discuss normalization with the client.

3. **Standard library only** — No pandas or external deps. Simplifies Lambda packaging and reduces cold start time.

4. **Line-by-line streaming** — File is never fully loaded into memory. Foundation for future scale improvements.

5. **Modular Terraform** — Each pipeline is an isolated module call. Shared infra (S3, KMS, IAM) lives in `shared.tf`. Adding a source requires no shared infrastructure changes.

---

## Tear Down

```bash
cd terraform && terraform destroy -auto-approve
```
