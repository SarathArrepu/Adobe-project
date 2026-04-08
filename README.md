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
│ data.sql │ ──────────► │  S3: landing/data.sql                       │
└──────────┘             │      │                                      │
                         │      │ S3 event trigger                     │
                         │      ▼                                      │
                         │  Lambda (Python 3.12)                       │
                         │      │                                      │
                         │      ├── bronze/data.sql  (raw archive)     │
                         │      └── gold/YYYY-MM-DD_SearchKeyword      │
                         │              Performance.tab  (output)      │
                         │                                             │
                         │  Glue Catalog ──► Athena (SQL queries)      │
                         │  KMS (encryption at rest, all layers)       │
                         │  CloudWatch (logs + error alarm)            │
                         └─────────────────────────────────────────────┘
```

### Medallion Lakehouse Layers

| Layer | S3 Prefix | Contents | Retention |
|---|---|---|---|
| Landing | `landing/` | Raw uploads (trigger zone) | 60 days |
| Bronze | `bronze/` | Raw archive (immutable copy) | 1 year → Glacier |
| Gold | `gold/` | Aggregated output reports | 1 year |
| Athena Results | `athena-results/` | Query scratch space | 7 days |

---

## Attribution Logic

1. **Visitor Identification** — Each unique IP address is a distinct visitor
2. **Search Engine Detection** — Referrer URL is parsed to extract engine domain and keyword
3. **Revenue Attribution** — On purchase event (`event_list` contains `1`), revenue from `product_list` is attributed to the last search engine that brought the visitor
4. **Aggregation** — Revenue summed per (engine, keyword) pair, sorted descending

---

## Project Structure

```
.
├── src/
│   ├── search_keyword_analyzer.py    # Core analyzer class
│   └── lambda_handler.py             # AWS Lambda entry point
├── tests/
│   └── test_analyzer.py              # Unit tests
├── terraform/
│   └── main.tf                       # All AWS infrastructure
├── data/
│   └── data.sql                      # Sample hit-level data
├── .github/
│   └── workflows/
│       └── ci-cd.yml                 # GitHub Actions pipeline
├── output/                           # Generated reports (gitignored)
├── dist/                             # Lambda zip artifacts (gitignored)
├── README.md
├── RUNBOOK.md                        # Full operational guide
├── DEPLOYMENT.md                     # Quick deployment reference
└── requirements.txt                  # No external dependencies
```

---

## Quick Start

### Local (no AWS required)

```bash
# Run analyzer
python src/search_keyword_analyzer.py data/data.sql

# Run tests
python -m unittest tests.test_analyzer -v
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
aws s3 cp data/data.sql s3://$BUCKET/landing/data.sql

# Wait ~20 seconds, then check output
aws s3 ls s3://$BUCKET/gold/
```

### Full Demo (one command)

```bash
chmod +x demo.sh && ./demo.sh
```

---

## GitHub Actions CI/CD

Every push to `main` runs the full pipeline automatically:

```
Push / PR / Manual trigger
        │
        ▼
  ┌─────────────┐
  │ Unit Tests  │  python -m unittest (no AWS needed)
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

```bash
git checkout main && git pull origin main
git checkout -b feature/my-change
# ... make changes ...
git push -u origin feature/my-change
gh pr create --title "My change" --body "Description"
# After review + CI pass → merge via GitHub UI
```

---

## AWS Infrastructure

All resources provisioned via Terraform (`terraform/main.tf`):

| Resource | Name | Purpose |
|---|---|---|
| S3 Bucket | `search-keyword-analyzer-dev-{account}` | Medallion data lake |
| Lambda | `search-keyword-analyzer-dev` | Processing engine |
| IAM Role | `search-keyword-analyzer-lambda-dev` | Least-privilege execution role |
| KMS Key | `alias/search-keyword-analyzer-dev` | Encryption at rest (auto-rotates) |
| Glue Database | `search_keyword_analyzer_dev` | Schema registry |
| Glue Tables | `bronze_hits`, `gold_keyword_performance` | Athena queryable tables |
| Athena Workgroup | `search-keyword-analyzer-dev` | Query engine (100 MB scan limit) |
| CloudWatch | `/aws/lambda/search-keyword-analyzer-dev` | Logs + error alarm |

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

**Console:** AWS → Athena → Workgroup: `search-keyword-analyzer-dev` → Database: `search_keyword_analyzer_dev`

```sql
-- Top keywords by revenue
SELECT * FROM gold_keyword_performance ORDER BY revenue DESC;

-- Raw hit data
SELECT pagename, COUNT(*) AS hits
FROM bronze_hits
GROUP BY pagename ORDER BY hits DESC;

-- Purchase events only
SELECT date_time, ip, geo_city, product_list
FROM bronze_hits
WHERE event_list LIKE '%1%';
```

**CLI:**
```bash
aws athena start-query-execution \
  --query-string "SELECT * FROM gold_keyword_performance ORDER BY revenue DESC" \
  --work-group "search-keyword-analyzer-dev" \
  --query-execution-context "Database=search_keyword_analyzer_dev"
```

---

## Scalability

The current application streams the file row by row (`csv.DictReader`), so memory usage is O(unique visitors), not O(file size). This handles moderate files efficiently.

For **10 GB+ files**:

| Approach | When | How |
|---|---|---|
| Chunked multiprocessing | 10–50 GB, single machine | Split file, process in parallel, merge |
| AWS Glue (PySpark) | 50+ GB, serverless | Managed Spark, auto-scaling, S3-native |
| EMR | Very large recurring jobs | Full Spark cluster, maximum control |
| Kinesis Streams | Real-time hits | Process as events arrive, no batching |

Key bottlenecks at scale: single-threaded processing, in-memory visitor attribution dict (grows with unique IPs), Lambda 15-min timeout and 10 GB `/tmp` limit.

---

## Design Decisions

1. **IP as visitor ID** — The dataset has no cookie/visitor ID. In production Adobe Analytics data, `visid_high + visid_low` would be used instead.

2. **Keyword case sensitivity** — "Ipod" and "ipod" are treated separately (raw data preserved). In production, discuss normalization with the client.

3. **Standard library only** — No pandas or external deps. Simplifies Lambda packaging and reduces cold start time.

4. **Line-by-line streaming** — File is never fully loaded into memory. Foundation for future scale improvements.

---

## Tear Down

```bash
cd terraform && terraform destroy -auto-approve
```
