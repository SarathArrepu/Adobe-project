# Future Architecture — Scaling the Pipeline

This document describes how the current modular architecture evolves to handle more sources, larger data volumes, and richer orchestration. Each section is an independent enhancement — adopt in whatever order makes sense.

---

## Phase 1 (Current) — Modular Event-Driven Pipeline

```
terraform/pipelines.tf
  └── module "adobe_pipeline" { source = "./modules/pipeline" }

S3 landing/adobe/
    │
    ▼  EventBridge rule
Lambda (pipelines.adobe.handler.lambda_handler)
    │
    ├──► gold/           (Athena queryable, no PII)
    ├──► bronze/raw/     (admin only, PII encrypted)
    └──► bronze/masked/  (developer accessible, hashed PII)

Glue Database: stg_adobe
  ├── adobe_gold
  ├── adobe_bronze_masked
  └── adobe_bronze_raw
```

**Suitable for:** Small files (<100 MB), ad-hoc queries via Athena.

---

## Phase 2 — Multi-Source (Next step, already supported)

Add a Terraform module block in `pipelines.tf` + a Python handler per source. The module template handles all infrastructure automatically.

```
terraform/pipelines.tf
  ├── module "adobe_pipeline"      { source = "./modules/pipeline", source_name = "adobe" }
  ├── module "salesforce_pipeline" { source = "./modules/pipeline", source_name = "salesforce" }
  └── module "marketo_pipeline"    { source = "./modules/pipeline", source_name = "marketo" }

S3 landing/adobe/         S3 landing/salesforce/      S3 landing/marketo/
    │                          │                            │
    ▼ EventBridge rule         ▼ EventBridge rule           ▼ EventBridge rule
Lambda (adobe)            Lambda (salesforce)          Lambda (marketo)
    │                          │                            │
    └──────────────────────────┼────────────────────────────┘
                               ▼
              ┌────────────────────────────────┐
              │    Shared S3 Data Lake         │
              │                                │
              │  gold/                         │
              │  bronze/masked/                │
              │  bronze/raw/                   │
              └────────────────────────────────┘
                               │
              ┌────────────────────────────────┐
              │  Glue Database: stg_adobe      │
              │  ├── adobe_gold                │
              │  ├── adobe_bronze_masked       │
              │  ├── salesforce_gold           │
              │  ├── salesforce_bronze_masked  │
              │  ├── marketo_gold              │
              │  └── marketo_bronze_masked     │
              └────────────────────────────────┘
                               │
                               ▼
                     Athena cross-source queries

-- Example: join Adobe and Salesforce revenue
SELECT a.search_keyword, a.revenue AS adobe_revenue, s.revenue AS crm_revenue
FROM stg_adobe.adobe_gold a
JOIN stg_adobe.salesforce_gold s
  ON a.search_keyword = s.campaign
ORDER BY adobe_revenue DESC;
```

**To add a source:**
1. Create `src/pipelines/salesforce/__init__.py` (empty)
2. Create `src/pipelines/salesforce/handler.py` (copy adobe handler, update transformation)
3. Add `module "salesforce_pipeline"` block in `terraform/pipelines.tf`
4. Push PR → CI → `terraform apply`

---

## Phase 3 — Large Files (>1 GB): Chunked Lambda or Glue ETL

### Option A: Chunked Lambda (1–10 GB)

Split large files server-side, process in parallel Lambda invocations.

```
S3 landing/ (large TSV)
    │
    ▼
Splitter Lambda (triggered by EventBridge)
    │  Reads file header, divides into N chunks
    │  Writes chunk manifests to S3
    │
    ├──► Chunk Lambda 1  ──► bronze/ + gold/partial/
    ├──► Chunk Lambda 2  ──► bronze/ + gold/partial/
    └──► Chunk Lambda N  ──► bronze/ + gold/partial/
                │
                ▼
         Merger Lambda (triggered when all chunks complete)
                │  Reads gold/partial/*, aggregates, writes final gold/
                ▼
         Final gold/ output
```

**Why Lambda stays:** Files up to ~10 GB fit in Lambda's 10 GB `/tmp` + 15-min timeout.
**Cost:** ~$0.002 per GB — cheaper than Glue for infrequent large files.

### Option B: AWS Glue (10 GB+, recurring)

Replace Lambda with a managed Spark job. Same S3 input/output paths; same Glue Catalog tables.

```
S3 landing/
    │
    ▼  EventBridge
AWS Glue Job (PySpark)          ← same transformation logic, pandas-style API
    │  Auto-scales workers
    │  Reads TSV in parallel partitions
    │
    ├──► gold/           (Parquet, columnar — 10x query speed vs TSV)
    └──► bronze/masked/  (Parquet)

Glue Catalog (stg_adobe):
    ├── adobe_gold          (format: Parquet, partitioned by ingestion_date)
    └── adobe_bronze_masked (format: Parquet)
```

**When to switch:** File size consistently >10 GB, or daily jobs take >10 min on Lambda.
**Migration path:** Same S3 paths, same Glue Catalog tables — Athena queries unchanged.

---

## Phase 4 — Multi-Step Orchestration: Step Functions (if needed)

Add Step Functions when you need **chaining, conditional branching, or parallel fan-out** between steps.

**Do NOT add for a single-Lambda pipeline — it adds latency and cost with no benefit.**

```
Trigger: EventBridge → Step Functions state machine

┌─────────────────────────────────────────────────────────────┐
│                   Step Functions                            │
│                                                             │
│  ┌───────────┐    ┌─────────────┐    ┌──────────────────┐  │
│  │  Validate │───►│  Transform  │───►│  Run Glue        │  │
│  │  Lambda   │    │  Lambda     │    │  Crawler         │  │
│  └───────────┘    └──────┬──────┘    └──────────────────┘  │
│       │                  │                    │             │
│  On fail:          Parallel writes:      On success:        │
│  Dead Letter       gold/ + bronze/       SNS notification   │
│  Queue             masked/ + raw/        to Slack/email     │
│                                                             │
│  Retry: 3 attempts, exponential backoff                     │
│  Catch: all errors → PipelineFailed state → alert          │
└─────────────────────────────────────────────────────────────┘
```

**Add Step Functions when you have:**
- Multiple sequential Lambda steps (validate → transform → notify)
- Need visual execution history and debug timeline
- Complex retry/fallback logic per step
- Fan-out patterns (one input → multiple parallel transforms)

**Terraform change:** Add Step Functions resources to `terraform/pipelines.tf` alongside the module call.

---

## Phase 5 — Real-Time Streaming (Kinesis)

Replace batch S3 uploads with event-by-event streaming for latency-sensitive use cases.

```
Website / App
    │  HTTP events (clicks, purchases)
    │
    ▼
Amazon Kinesis Data Streams
    │  Retention: 7 days
    │  Shards: auto-scaled
    │
    ▼
Lambda (stream consumer)       ← same transformation logic as batch handler
    │  Batch size: 100 records
    │  Parallelism: 1 concurrent consumer per shard
    │
    ├──► DynamoDB (session state — replaces in-memory dict)
    │       Visitor → last search engine attribution
    │
    └──► Kinesis Firehose ──► S3 gold/ (micro-batches every 60s)
                          └──► S3 bronze/masked/ (micro-batches)

Glue Catalog + Athena: unchanged
Latency: seconds vs minutes (batch)
```

**When to use:** Revenue attribution needs to be available within seconds of a purchase event.

---

## Phase 6 — Data Quality Gate

Add automated schema validation and row-count checks before data reaches gold/.

```
S3 landing/
    │
    ▼  EventBridge
Validator Lambda
    │  Checks: schema match, null rates, row count vs yesterday
    │
    ├── PASS ──► Transform Lambda (current pipeline)
    │
    └── FAIL ──► S3 quarantine/          ← blocked from gold/
                 SNS alert to team
                 CloudWatch metric: DataQualityFailures
```

**Terraform:** New Lambda + EventBridge rule added in `terraform/pipelines.tf`; no changes to existing pipeline or shared infrastructure.
**Python:** New `src/pipelines/adobe/validate_handler.py` following the same template pattern.

---

## Decision Matrix

| Scenario | Recommended Approach |
|---|---|
| New data source, same file format | Add module block in `pipelines.tf` + handler (Phase 2) |
| File size 1–10 GB | Chunked Lambda (Phase 3A) |
| File size 10+ GB, recurring | AWS Glue PySpark (Phase 3B) |
| Multi-step pipeline with branching | Step Functions (Phase 4) |
| Sub-minute latency required | Kinesis + streaming Lambda (Phase 5) |
| Schema drift / bad data risk | Validation Lambda gate (Phase 6) |
| File size < 100 MB, single source | Current architecture (Phase 1) ✓ |
