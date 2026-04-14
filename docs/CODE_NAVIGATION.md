# Code Navigation Runbook — Adobe Analytics Pipeline

Quick reference for code reviews. Each section answers a common question with
the exact file and line number to jump to.

---

## Test Units Overview

### Pipeline-specific — `modules/adobe/tests/test_analyzer.py`

5 test classes, all testing `SearchKeywordAnalyzer`:

| Class | What it tests | # tests |
|---|---|---|
| `TestParseSearchEngine` | Referrer URL → `(engine, keyword)` parsing | 8 |
| `TestParseRevenue` | `product_list` → revenue float extraction | 6 |
| `TestIsPurchaseEvent` | `event_list` contains `"1"` exact token match | 7 |
| `TestEndToEnd` | Full `process() → get_results() → write_output()` chain using synthetic fixture | 4 |
| `TestWithProvidedDataFile` | Integration: runs against real `data/adobe/data.sql`, expects $730 total revenue | 1 (skipped if file absent) |

### Shared — `tests/test_shared/test_dq_checker.py`

11 unit classes + 1 integration class, all testing `DataQualityChecker`:

| Class | DQ Check | Severity | # tests |
|---|---|---|---|
| `TestMissingRequiredColumns` | Required column absent vs optional column absent | ERROR / WARN | 3 |
| `TestEmptyFile` | File has header only, no data rows | ERROR | 2 |
| `TestMissingIP` | `ip` field is empty string | WARN | 2 |
| `TestInvalidHitTime` | `hit_time_gmt` not a valid Unix timestamp | WARN | 4 |
| `TestInvalidIPFormat` | `ip` fails IPv4 regex (e.g. `999.999.999.999`) | WARN | 3 |
| `TestDuplicateHit` | Same `(ip, hit_time_gmt)` pair appears twice | WARN | 2 |
| `TestUnknownEventID` | `event_list` contains an unrecognised event code | INFO | 3 |
| `TestPurchaseNoProduct` | `event_list="1"` but `product_list` is empty | WARN | 3 |
| `TestProductRevenueNoPurchase` | Revenue present in `product_list` but no purchase event | WARN | 3 |
| `TestNegativeRevenue` | Revenue value is negative | WARN | 2 |
| `TestMalformedProductList` | Too few semicolon-delimited fields, or non-numeric revenue | WARN | 3 |
| `TestWithProvidedDataFile` | Integration: real `data.sql` passes all checks, 21 rows | 2 (skipped if absent) |

**DQ severity rule:**
- **ERROR** — file-level problem, pipeline must abort (2 checks: missing columns, empty file)
- **WARN** — row-level problem, causes silent data loss or wrong revenue (9 checks)
- **INFO** — noteworthy but no correctness impact (1 check: unknown event ID)

---

## Project Layout

```
adobe-assessment/
├── modules/adobe/
│   ├── src/adobe/
│   │   ├── analyzer.py          ← Pipeline-specific: revenue attribution logic
│   │   └── handler.py           ← Pipeline-specific: Lambda entry point / orchestration
│   └── tests/
│       └── test_analyzer.py     ← Tests for analyzer.py
├── src/shared/
│   ├── base_handler.py          ← Shared: S3 upload + PII masking utilities
│   └── dq_checker.py            ← Shared: data quality validation
└── tests/test_shared/
    └── test_dq_checker.py       ← Tests for dq_checker.py
```

---

## "Where is the test for the handler?"

**Short answer:** There is no dedicated unit test file for `handler.py`. Handler
orchestration is covered end-to-end through the analyzer integration tests.

| What is tested | File | Class |
|---|---|---|
| Full pipeline: process → results → output file | [modules/adobe/tests/test_analyzer.py](../modules/adobe/tests/test_analyzer.py) | `TestEndToEnd` (line 228) |
| Against real `data/adobe/data.sql` | [modules/adobe/tests/test_analyzer.py](../modules/adobe/tests/test_analyzer.py) | `TestWithProvidedDataFile` (line 352) |
| DQ gate (blocks pipeline on ERROR) | [tests/test_shared/test_dq_checker.py](../tests/test_shared/test_dq_checker.py) | `TestMissingRequiredColumns` (line ~30) |

**Why no handler unit test?** `handler.py` is AWS Lambda glue — it calls boto3
`download_file` / `upload_file` / `delete_objects` which require mocked AWS
clients to test in isolation. The business logic (attribution, DQ, masking) is
fully tested in the modules it delegates to.

---

## "Where is the referrer URL parsing logic?"

**File:** [modules/adobe/src/adobe/analyzer.py](../modules/adobe/src/adobe/analyzer.py)

**Method:** `SearchKeywordAnalyzer.parse_search_engine()` — **line ~130**

### Two-path matching

**Path 1 — Known engines (O(1) dict lookup):**

```
_DOMAIN_PARAMS dict — line ~74 in analyzer.py
```
Maps clean domain → keyword param name(s):
```python
"google.com":       ["q"]        # row 2:  ?q=Ipod      → ("google.com", "Ipod")
"bing.com":         ["q"]        # row 3:  ?q=Zune      → ("bing.com",   "Zune")
"search.yahoo.com": ["p"]        # row 4:  ?p=cd+player → ("search.yahoo.com", "cd player")
"baidu.com":        ["wd","kw","word"]   # tries each param in order
```

**Path 2 — Unknown engines (frozenset fallback):**

```
COMMON_SEARCH_PARAMS frozenset — line ~100 in analyzer.py
```
If domain is NOT in `_DOMAIN_PARAMS`, checks whether any URL param is in this
frozenset (`"q"`, `"query"`, `"search"`, `"p"`, `"text"`, etc.).

| Referrer | Param | In frozenset? | Result |
|---|---|---|---|
| `?q=headphones` (unknown engine) | `q` | YES | captured — bare domain used as engine |
| `?a=confirm` (esshopzilla checkout) | `a` | NO | `None` — not a search, prior attribution kept |
| `?k=Ipod` (internal site search) | `k` | NO | `None` — internal nav, not external search |

### Tests for referrer parsing

**File:** [modules/adobe/tests/test_analyzer.py](../modules/adobe/tests/test_analyzer.py) — class `TestParseSearchEngine` (line 34)

| Test method | What it checks |
|---|---|
| `test_google_referrer` (line 62) | `?q=Ipod` → `("google.com", "Ipod")` |
| `test_yahoo_referrer` (line 70) | `?p=cd+player` → `("search.yahoo.com", "cd player")` |
| `test_bing_referrer` (line 78) | `?q=Zune` → `("bing.com", "Zune")` |
| `test_non_search_referrer` (line 86) | `esshopzilla.com/product/` → `None` |
| `test_empty_referrer` (line 92) | `""`, `None`, `"   "` all → `None` |
| `test_google_without_keyword` (line 98) | Google URL with no `?q=` → `None` |
| `test_encoded_keyword` (line 104) | `%20` decoded → `"ipod nano case"` |
| `test_yahoo_plus_encoded_spaces` (line 111) | `+` decoded → `"cd player portable"` |

---

## "Where is the revenue attribution / purchase detection?"

**File:** [modules/adobe/src/adobe/analyzer.py](../modules/adobe/src/adobe/analyzer.py)

| Logic | Method | Approx line |
|---|---|---|
| Is this row a purchase? | `is_purchase_event()` | ~175 |
| Extract revenue from product_list | `parse_revenue()` | ~155 |
| Main attribution loop (IP → engine → revenue) | `process()` | ~195 |
| Final sorted results | `get_results()` | ~240 |
| Write tab-delimited output file | `write_output()` | ~255 |

**Key design — why `split(",")` not `"1" in event_list`:**
Event "1" (purchase) vs "10" (Cart Open): `"1" in "10"` → `True` (wrong).
Splitting on comma and comparing exact tokens fixes this. Tested in
`test_event_10_not_confused_with_1` (line 213 in `test_analyzer.py`).

**Revenue parsing — product_list format:**
```
"Electronics;Zune - 32GB;1;250;"
              ^category  ^qty ^revenue index 3 → 250.0
```
Tested in `TestParseRevenue` (line 119 in `test_analyzer.py`).

---

## "Where is the DQ / data quality logic?"

**File:** [src/shared/dq_checker.py](../src/shared/dq_checker.py)

| Check | Method | Severity |
|---|---|---|
| Required columns present | `run()` → `_check_required_columns` | ERROR |
| File has at least 1 data row | `run()` → `_check_empty_file` | ERROR |
| IP field not empty | `_check_missing_ip` | WARN |
| hit_time_gmt is valid Unix timestamp | `_check_hit_time` | WARN |
| IP matches IPv4 regex | `_check_ip_format` | WARN |
| Duplicate (ip, hit_time_gmt) pair | `_check_duplicate_hit` | WARN |
| Unrecognised event ID | `_check_unknown_event` | INFO |
| Purchase event with no product_list | `_check_purchase_no_product` | WARN |
| Revenue in product_list without purchase event | `_check_product_revenue_no_purchase` | WARN |
| Negative revenue | `_check_negative_revenue` | WARN |
| Malformed product_list (too few fields, non-numeric revenue) | `_check_malformed_product` | WARN |

**Tests:** [tests/test_shared/test_dq_checker.py](../tests/test_shared/test_dq_checker.py) — 30 tests across 11 unit classes + 1 integration class.

Pattern used in every test:
```python
_make_row(ip="")          # override one column, rest are valid defaults
_make_row(hit_time_gmt="not-a-number")
```

---

## "Where is the PII masking / bronze layer logic?"

**File:** [src/shared/base_handler.py](../src/shared/base_handler.py)

| Logic | Function | Line |
|---|---|---|
| SHA-256 hash a PII field | `hash_pii()` | ~51 |
| Build masked TSV in memory (no temp file) | `write_masked_tsv()` | ~85 |
| Upload bytes to S3 with KMS encryption | `put_s3_object()` | ~143 |
| Copy raw file to `bronze/raw/` (PII KMS key) | `archive_raw()` | ~169 |
| Copy masked file to `bronze/masked/` (standard key) | `archive_masked()` | ~205 |

**Two KMS keys — why:**
- `bronze/raw/` uses `PII_KMS_KEY_ARN` → only admin IAM role can decrypt (real IPs)
- `bronze/masked/` uses `KMS_KEY_ARN` → developer IAM role can decrypt (hashed IPs)

**Hash output length:** `"sha256:"` (7) + 64 hex chars = **71 chars total** — why all
bronze Glue columns must be `string` not `varchar(n)`.

---

## "Where is the Lambda entry point / orchestration?"

**File:** [modules/adobe/handler.py](../modules/adobe/src/adobe/handler.py) — `lambda_handler()` line 50

**Processing order:**
```
1. Parse S3 event → download file to /tmp/
2. DQ checks  (DataQualityChecker)     — abort if ERROR
3. Attribution (SearchKeywordAnalyzer) — build revenue map
4. Gold layer  — delete today's partition, write aggregated .tab file
5. Bronze raw  — copy original file (PII KMS key)
6. Bronze masked — copy hashed file (standard KMS key)
7. Return 200 with S3 paths + revenue summary
```

**Insert-overwrite gold partition (idempotent reruns):**
Lines ~148–163 in `handler.py` — list all `gold/dt=today/` objects → delete → write new file.

---

## "Where are the tests and how do I run them?"

```bash
# All shared utility tests
python -m pytest tests/test_shared/

# All analyzer / pipeline tests
python -m pytest modules/adobe/tests/

# Single test class
python -m pytest modules/adobe/tests/test_analyzer.py::TestParseSearchEngine -v

# Integration tests only (need data/adobe/data.sql present)
python -m pytest modules/adobe/tests/test_analyzer.py::TestWithProvidedDataFile -v
```

**Integration tests skip gracefully** when `data/adobe/data.sql` is absent
(CI environments, fresh clones) via `@unittest.skipUnless`.

---

## Quick grep cheatsheet

```bash
# Find where referrer is parsed
grep -n "parse_search_engine" modules/adobe/src/adobe/analyzer.py

# Find where purchase event is checked
grep -n "is_purchase_event\|PURCHASE_EVENT" modules/adobe/src/adobe/analyzer.py

# Find where PII is hashed
grep -n "hash_pii\|sha256" src/shared/base_handler.py

# Find all DQ check methods
grep -n "def _check_" src/shared/dq_checker.py

# Find where Lambda handler starts
grep -n "def lambda_handler" modules/adobe/src/adobe/handler.py
```
