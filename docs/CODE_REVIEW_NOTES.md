# Code Review Notes — My Walkthrough Script
> Written in first person. Use this to talk through each file naturally during the call.

---

## `analyzer.py` — The Core Business Logic

So I'll start with the analyzer because this is where all the actual business logic lives. I created a class called `SearchKeywordAnalyzer` and the whole job of this class is to answer the client's question — which search keywords are driving revenue.

**The two data structures I chose**

Inside the class I maintain two dictionaries. The first one is `_visitor_search_attribution` — it's a plain dict that maps an IP address to a `(engine, keyword)` tuple. So for example after processing the file it looks like `{"67.98.123.1": ("google.com", "Ipod")}`. I used a tuple here because the engine and keyword always travel together as a pair — you never update just the engine without the keyword, so a tuple made sense to keep them atomic.

The second one is `_revenue_data` and I used a `defaultdict(float)` here rather than a plain dict. The key is again a tuple — `(engine_domain, keyword)` — and the value accumulates revenue. The reason I used `defaultdict(float)` is that when I see a purchase for a new keyword pair for the first time, I don't want to check `if key in dict` every time. The `defaultdict` auto-initialises missing keys to `0.0`, so I can just do `self._revenue_data[attribution] += revenue` directly without any defensive code.

**How referrer URL parsing works — `parse_search_engine()`**

This method is responsible for taking a raw referrer URL and returning the search engine domain and keyword. I designed it with two paths.

Path 1 is a flat dictionary called `_DOMAIN_PARAMS` — it maps clean domain names to their keyword parameter. So `"google.com"` maps to `["q"]`, `"search.yahoo.com"` maps to `["p"]`, and so on. The reason I used a flat dict here instead of any kind of nested loop or regex is that it's O(1) lookup. When you're processing billions of rows at 10 GB scale, that matters. Also adding a new search engine is literally one line in the dict — no regex to update, no deploy complexity.

One thing worth noting — Baidu uses three different params: `["wd", "kw", "word"]`. I store them as a list so the code tries each in order and returns the first match.

Path 2 is a `frozenset` called `COMMON_SEARCH_PARAMS`. This is the fallback for when the referrer domain isn't in my known engines dict. If the domain is unknown but carries a param like `?q=` or `?query=`, I still capture it automatically using the bare domain as the engine name. The reason I chose a frozenset is O(1) membership testing, it's immutable so nobody accidentally modifies it, and it semantically communicates "this is a constant set of known params."

The reason I need both is they solve different problems. The dict gives me precision for known engines — Yahoo uses `?p=`, not `?q=`, so without the dict I'd miss Yahoo searches. The frozenset gives me coverage for engines I've never heard of.

I also want to flag one subtle thing in this method — I used `removeprefix("www.")` to strip the `www.` from domains, not `lstrip("www.")`. The reason is `lstrip` treats its argument as a **character set**, not a string. So `"wordpress.com".lstrip("www.")` would return `"rdpress.com"` because it strips any leading `w`, `o`, `r`, `.` characters. That would silently corrupt domain names. `removeprefix` is an exact string match and is the correct tool here.

**Purchase detection — `is_purchase_event()`**

This method checks whether `event_list` contains event ID `"1"` which means a purchase. I split on comma and compare each token exactly rather than doing `"1" in event_list`. The reason is that `"1" in "10"` evaluates to `True` in Python. Event 10 is Cart Open, not a purchase — if I used the substring check, every cart-open row would incorrectly trigger revenue attribution. I even wrote a specific test for this: `test_event_10_not_confused_with_1`.

**Why `process()` streams row by row**

I used `csv.DictReader` which reads one row at a time. The file is never loaded into memory. So memory usage is only O(unique IPs + unique engine/keyword pairs) — completely independent of file size. This is what makes it scale to 10 GB files without any code changes.

**The output file**

`write_output()` produces a tab-delimited file named `YYYY-mm-dd_SearchKeywordPerformance.tab` using today's UTC date. I used UTC specifically so that a Lambda running in any AWS region produces the same filename for the same logical day.

---

## `handler.py` — The Lambda Entry Point

This file is the AWS Lambda entry point. I kept it deliberately thin — it's just orchestration, all the business logic is in the classes it calls.

**The five-step order I chose**

When a file lands in S3, the handler does this in order:
1. Parse the S3 event and download the file to `/tmp/`
2. Run DQ checks — if any ERROR is found, I raise immediately and nothing gets written
3. Run the analyzer to build the revenue attribution
4. Write the gold layer output with an insert-overwrite strategy
5. Write bronze raw and bronze masked

I put DQ first deliberately. If the file is bad, I don't want to write anything to S3 and then have to clean it up. Fail fast, no partial state.

**`unquote_plus` not `unquote`**

When S3 sends an event notification, it encodes spaces in filenames as `+`, not `%20`. The standard `unquote` function only handles `%20`. I used `unquote_plus` so both encoding styles are handled correctly.

**Why I pass `run_dq=False` to the analyzer**

The handler already ran the DQ check explicitly before calling `analyzer.process()`. If I let the analyzer run it again with the default `run_dq=True`, the file gets scanned twice for no reason. So I pass `run_dq=False` to skip it. The responsibility is clear — handler owns the DQ gate, analyzer owns the transformation.

**Insert-overwrite on the gold partition**

Before writing today's output file I list all objects under `gold/dt=today/` and delete them first. This makes reruns idempotent — if Lambda is triggered twice on the same day (reprocessing, backfill, or a retry), the second run replaces the first rather than creating a second file that would show up as duplicate rows in Athena. I only delete today's partition, so previous days are never touched.

**Module-level `s3_client`**

I create the boto3 S3 client once at module level, not inside the handler function. Lambda reuses execution contexts across warm invocations — if I created the client inside the function, every invocation would re-establish a TLS connection. Creating it once at module level means warm invocations reuse the same connection.

---

## `base_handler.py` — Shared PII Masking and S3 Utilities

I put this in `src/shared/` because any future pipeline source — Salesforce, Marketo, whatever — would need the same S3 upload and PII masking utilities. The adobe-specific logic stays in the module, the plumbing is shared.

**Two KMS keys**

I use two separate KMS keys. The standard key encrypts landing, bronze/masked, gold, and Athena results. The PII key encrypts bronze/raw only. The Lambda role has encrypt-only permissions on the PII key — `kms:GenerateDataKey` but no `kms:Decrypt`. That means Lambda can write the raw file but cannot read it back. Only the admin IAM role has decrypt access on the PII key.

**`hash_pii()` — why SHA-256 and why the prefix**

I hash PII fields with SHA-256 and prepend `"sha256:"` to the result. The reason I use a deterministic hash rather than a random one is that the same IP always produces the same hash — so unique visitor counts in Athena are still accurate. The `"sha256:"` prefix tells any downstream consumer that this field has been pseudonymised, not that it's a real value.

One thing to flag — the hash output is always 71 characters: 7 for `"sha256:"` plus 64 hex digits. That's why I defined all bronze Glue columns as `string` type, not `varchar(n)`. Any `varchar` shorter than 71 would silently truncate the hash and break it.

I also documented a known limitation in the file header — plain SHA-256 without a salt is vulnerable to rainbow-table attacks on the small IPv4 address space. The upgrade path is HMAC-SHA256 with a secret salt from AWS Secrets Manager.

**`write_masked_tsv()` — in-memory buffer**

I build the masked copy in an `io.StringIO` buffer rather than writing a temp file to disk and reading it back. This avoids an unnecessary write/read cycle on `/tmp` and produces bytes that go directly to `put_object`.

**`ServerSideEncryption: "aws:kms"` hardcoded on every upload**

Every `put_object` call explicitly sets `"aws:kms"` encryption. Without this, S3 could fall back to SSE-S3 which uses an AWS-managed key. In that case, any developer with `s3:GetObject` permission could read the raw bronze file — the KMS-based PII isolation would be completely bypassed.

---

## `dq_checker.py` — Data Quality Gate

I built `DataQualityChecker` as a separate shared class because data quality validation is not specific to the Adobe pipeline — any pipeline that processes hit-level TSV data would want the same checks.

**Three severity levels**

I designed three severity levels. ERROR means the file is fundamentally unusable and the pipeline must abort — I have two ERROR checks: missing required columns and an empty file. WARN means a row-level issue that causes silent data loss, like an invalid IP or a purchase event with no product list — the pipeline can continue but those rows will produce wrong results. INFO is for things worth knowing but that don't affect correctness, like an unrecognised event ID.

**Why I check columns before the row loop**

The very first thing `run()` does is check that all required columns are present. If `ip` or `referrer` is missing, every single row would either fail or be silently skipped. There's no point scanning 10 million rows to discover the file structure is broken — I return an ERROR immediately.

**Single-pass design**

The entire DQ suite runs in one `csv.DictReader` pass. Every check runs on the same row in the same loop. At 10 GB scale, reading the file twice for DQ would double the I/O cost, so I deliberately designed it as a single pass.

**Duplicate hit detection**

I detect duplicates using a composite key of `(ip, hit_time_gmt)`. If the same IP sends a hit at the exact same Unix timestamp, that's the same request being sent twice — this happens with Adobe Analytics beacon retries. Flagging it prevents double-counting revenue.

---

## `test_analyzer.py` — Analyzer Tests

I have five test classes covering the three core methods independently and then the full end-to-end pipeline.

**The dummy file pattern in `setUp`**

Every test class creates a minimal `.tsv` file in a `tempfile.mkdtemp()` directory. I do this because `SearchKeywordAnalyzer.__init__` validates that the file exists — it raises `FileNotFoundError` immediately if it doesn't. The dummy file has just a header row, which is enough to satisfy the constructor for unit tests that only test one method. `tearDown` calls `shutil.rmtree` so the temp directory is always cleaned up even if a test fails.

**Why I wrote synthetic fixture data for `TestEndToEnd`**

The end-to-end test uses a hand-crafted fixture rather than the real `data.sql` file. The reason is CI environments don't have the data file, and I wanted the test suite to pass on a completely fresh clone. The fixture has three visitors — one from Google, one from Bing, one from Yahoo — and only the Google and Bing visitors make purchases. That exercises the important path: Yahoo visitor arrived via search but never bought anything, so they should produce no revenue entry.

**`@unittest.skipUnless` on the integration test**

The `TestWithProvidedDataFile` class runs against the real `data/adobe/data.sql` file and asserts total revenue is $730. I decorated it with `@unittest.skipUnless` so it skips gracefully when the file isn't present. This means CI always passes, and the integration test runs when you have the data locally. The assertions are: 3 keyword entries, total revenue $730, all three engines are in `{google.com, bing.com, search.yahoo.com}`.

---

## `terraform/shared.tf` — Shared Infrastructure

This file provisions everything that is shared across all pipeline sources — S3 bucket, both KMS keys, IAM roles, Glue database, and Athena workgroup. I separated it from the pipeline module so that adding a new source never requires touching this file.

**Two KMS keys and why I set up the PII key policy the way I did**

The PII key policy explicitly names the admin IAM role as the only principal that can call `kms:Decrypt`. Lambda roles get `kms:Encrypt` and `kms:GenerateDataKey` from their IAM policy — they can write raw bronze but cannot decrypt it. I also set `enable_key_rotation = true` on both keys so AWS automatically rotates the key material annually.

**Defence-in-depth on raw bronze — two layers**

I protect `bronze/raw/` with both an IAM policy deny on the developer role and an S3 bucket policy deny. The reason I have both is that an S3 bucket `Deny` statement cannot be overridden by any IAM `Allow` except root — it's a hard stop. IAM policies alone can sometimes be overridden by misconfigured role attachments. The bucket policy is the safety net. Same pattern applies to `landing/`.

**S3 lifecycle rules**

I set up tiered storage transitions on each prefix. Landing data moves to Glacier after 30 days because it's raw inbound — once processed it's just an audit log. Bronze moves to Standard-IA after 90 days and Glacier after 180. Gold stays in Standard-IA longer since it's what analysts actually query. I also added an `abort-incomplete-multipart` rule after 7 days — without this, failed multipart uploads leave orphaned parts that you keep paying for.

**EventBridge notification on the bucket**

I enable EventBridge at the bucket level with a single resource. Each pipeline module then registers its own EventBridge rule filtered to its own `landing/{source}/` prefix. `shared.tf` doesn't know or care about individual sources — loose coupling. Adding Salesforce as a new source doesn't touch this file at all.

**Athena cost guardrail**

I set `bytes_scanned_cutoff_per_query` on the Athena workgroup. Without this, someone running a full-table scan on a 10 GB bronze table would rack up unexpected charges. The cutoff kills the query before it completes.

---

## `terraform/modules/pipeline/main.tf` — Reusable Pipeline Module

This is a reusable Terraform module. Every time I call it from `pipelines.tf` with a different `source_name`, it spins up a complete, isolated pipeline — Lambda, IAM role, EventBridge rule, CloudWatch logs and alarm, and three Glue tables.

**Least-privilege IAM for the Lambda role**

I was deliberate about what permissions the Lambda role gets. It can read from `landing/{source}/*` only. It can write to `bronze/raw/`, `bronze/masked/`, and `gold/`. For the gold insert-overwrite it needs `s3:ListBucket` and `s3:DeleteObject`. On KMS, it has full encrypt/decrypt on the standard key — needed to write and later read masked and gold data. On the PII key, it has encrypt-only — `kms:GenerateDataKey` but no `kms:Decrypt`. Lambda can archive raw data but cannot read its own archive back. That's intentional.

**`input_transformer` on the EventBridge target**

EventBridge delivers events in its own JSON shape. My Lambda handler expects the standard S3 event shape — `Records[0].s3.bucket.name` and `Records[0].s3.object.key`. The `input_transformer` block reshapes the EventBridge event into that format before it reaches Lambda. The handler has no idea it's being called by EventBridge — it just sees an S3 event. That means the same handler code could be triggered by SNS, direct invocation, or a test harness without any changes.

**Why Glue columns are all `string` type, not `varchar`**

When I hash an IP address with SHA-256, the result is always 71 characters. If I used `varchar(20)` — which would be fine for a real IP — the hash would be silently truncated and the column would become useless. So all bronze columns are `string` type, which is unbounded.

**Why I removed the Glue Crawler**

I initially had a crawler but removed it. Crawlers cost money per DPU-hour, they take 10–15 minutes to run, and most importantly a crawler re-inferring schema after a file format change can overwrite the correct column types I've defined in Terraform with incorrect inferred ones. Since my schemas are fully known at deploy time, I define them statically in Terraform and skip the crawler entirely. For gold partition discovery I use Athena partition projection instead — it auto-generates `dt=` partition values from a date range without any crawl.

**CloudWatch alarm threshold set to zero**

I set the Lambda error alarm threshold to `0`. Any error in a data pipeline means missed data — even one failed invocation could mean a full day's revenue attribution is lost. So I alert on the first error, not after several.
