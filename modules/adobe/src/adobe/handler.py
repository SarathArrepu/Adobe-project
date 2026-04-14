"""
Adobe Analytics hit-level pipeline — Lambda handler
====================================================
Triggered by S3 PutObject events on the ``landing/adobe/`` prefix whenever a
new hit-level TSV file is uploaded.

Processing steps
----------------
1. **DQ checks** — DataQualityChecker validates the file.  If any ERROR-level
   issue is found the function raises immediately and nothing is written to S3.
2. **Attribution** — SearchKeywordAnalyzer streams the file, attributes revenue
   to external search keywords, and builds the aggregated output.
3. **Gold layer** — Aggregated keyword-performance report (no PII) uploaded to
   ``gold/dt=YYYY-MM-DD/`` using an insert-overwrite strategy: existing objects
   in today's partition are deleted before the new file is written, making
   reruns and backfills safe and idempotent.
4. **Bronze raw** — Original file re-uploaded to ``bronze/raw/`` with the
   dedicated PII KMS key (admin-decrypt only).
5. **Bronze masked** — PII fields (ip, user_agent) SHA-256 hashed; written to
   ``bronze/masked/`` with the standard KMS key (developer-accessible).

Adding a new data source
------------------------
1. Copy modules/adobe/ to modules/<source>/
2. Rename the inner src/<source>/ package folder to match your source name
3. Replace ``SearchKeywordAnalyzer`` with your transformation class
4. Update the module block in modules/<source>/terraform/pipeline.tf
5. Run ``scripts/build.sh && terraform -chdir=terraform apply``
"""

import json                         # used to serialise the Lambda response body
import logging                      # structured log output to CloudWatch
import os                           # file-system path helpers and environment variables
import boto3                        # AWS SDK — S3 download/upload operations
from datetime import datetime, timezone  # UTC timestamp for dt= partition key
from urllib.parse import unquote_plus    # decode percent-encoded S3 key names (spaces, etc.)

from adobe.analyzer import SearchKeywordAnalyzer         # adobe revenue-attribution logic
from shared.dq_checker import DataQualityChecker         # input validation gate
from shared.base_handler import archive_raw, archive_masked  # bronze-layer archival helpers

logger = logging.getLogger()         # root logger; level controlled by Lambda env var LOG_LEVEL
logger.setLevel(logging.INFO)        # default to INFO so DQ summaries are always visible

# Module-level S3 client — created once at cold start, reused across warm invocations.
# Example: same TLS connection used whether Lambda processes 1 file or 100 in a warm burst.
s3_client = boto3.client("s3")


def lambda_handler(event: dict, context: object) -> dict:
    """
    AWS Lambda entry point for the Adobe Analytics hit-level pipeline.

    Expects a standard S3 PutObject event delivered via EventBridge.  Reads
    the triggered object from ``landing/adobe/``, validates it, transforms
    it, and writes the results to three output layers.

    DQ checks run first — no data is written to S3 if the file fails
    ERROR-level checks.

    Args:
        event:   AWS event dict containing ``Records[0].s3.bucket.name`` and
                 ``Records[0].s3.object.key``.
        context: Lambda context object (unused but required by the interface).

    Returns:
        A dict with ``statusCode`` (200 on success, 500 on error) and a
        ``body`` JSON string.  On success the body includes S3 paths for all
        three output layers and a revenue summary.
    """
    try:
        # ── Parse the triggering S3 event ────────────────────────────────
        # Example event shape from EventBridge when data.sql lands in S3:
        # {
        #   "Records": [{
        #     "s3": {
        #       "bucket": {"name": "adobe-stg-data-lake"},
        #       "object": {"key": "landing/adobe/data.sql"}
        #     }
        #   }]
        # }
        record = event["Records"][0]                           # first (and only) record per invocation
        bucket = record["s3"]["bucket"]["name"]                # e.g. "adobe-stg-data-lake"
        key    = unquote_plus(record["s3"]["object"]["key"])   # e.g. "landing/adobe/data.sql"
        # unquote_plus example: "landing/adobe/my%20file.sql" → "landing/adobe/my file.sql"
        # (S3 encodes spaces as + in event notifications, not %20 — unquote_plus handles both)

        logger.info(f"Processing file: s3://{bucket}/{key}")
        # CloudWatch log: "Processing file: s3://adobe-stg-data-lake/landing/adobe/data.sql"

        # ── Download the input file to Lambda's ephemeral /tmp storage ───
        base_name   = os.path.basename(key)          # "landing/adobe/data.sql" → "data.sql"
        local_input = f"/tmp/{base_name}"             # "/tmp/data.sql"  (/tmp is writable in Lambda)
        s3_client.download_file(bucket, key, local_input)
        # After this: /tmp/data.sql exists with all 21 rows from the source file

        # ── Step 1: Data Quality checks — must pass before any S3 write ──
        # Run the full DQ suite against /tmp/data.sql.
        # Example: checks that columns hit_time_gmt, ip, event_list, product_list, referrer exist.
        # Example: checks that all hit_time_gmt values are valid Unix timestamps (e.g. 1254033280).
        # Example: checks that ip values are valid IPv4 (e.g. "67.98.123.1" passes, "" fails).
        dq_report = DataQualityChecker(local_input).run()
        dq_report.print_summary()
        # CloudWatch: "DQ Report [PASSED] — /tmp/data.sql | 21 rows | 0 errors, 0 warnings, 0 info"
        if not dq_report.passed():
            # Example failure: file with no "ip" column → MISSING_REQUIRED_COLUMNS ERROR → abort
            raise ValueError(
                f"DQ checks failed for {key}: "
                f"{len(dq_report.errors)} error(s) — aborting pipeline, no data written."
            )

        # ── Step 2: Transformation: Adobe keyword attribution ─────────────
        # SearchKeywordAnalyzer streams /tmp/data.sql row by row.
        # After process(), internal state looks like:
        #   _visitor_search_attribution = {
        #     "67.98.123.1": ("google.com", "Ipod"),    # row 2: google referral
        #     "23.8.61.21":  ("bing.com",   "Zune"),    # row 3: bing referral
        #     "44.12.96.2":  ("google.com", "ipod"),    # row 5: google referral
        #   }
        #   _revenue_data = {
        #     ("bing.com",   "Zune"): 250.0,   # row 16: 23.8.61.21 purchased Zune $250
        #     ("google.com", "ipod"): 190.0,   # row 19: 44.12.96.2 purchased iPod Nano $190
        #     ("google.com", "Ipod"): 290.0,   # row 22: 67.98.123.1 purchased iPod Touch $290
        #   }
        analyzer = SearchKeywordAnalyzer(local_input)
        analyzer.process(run_dq=False)   # run_dq=False avoids reading the file a second time

        # ── Step 3: Gold layer: aggregated keyword performance, no PII ──────
        # write_output() creates: /tmp/output/2026-04-13_SearchKeywordPerformance.tab
        # File contents (tab-delimited, sorted by revenue desc):
        #   Search Engine Domain\tSearch Keyword\tRevenue
        #   google.com\tIpod\t290.00
        #   bing.com\tZune\t250.00
        #   google.com\tipod\t190.00
        output_path = analyzer.write_output("/tmp/output")

        # Build Hive-style S3 partition path using today's UTC date.
        # Example: dt_date = "2026-04-13"
        dt_date     = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        gold_prefix = f"gold/dt={dt_date}/"                      # "gold/dt=2026-04-13/"
        gold_key    = f"{gold_prefix}{os.path.basename(output_path)}"
        # gold_key = "gold/dt=2026-04-13/2026-04-13_SearchKeywordPerformance.tab"

        # --- Insert-overwrite: delete existing partition objects before writing ----
        # If Lambda ran yesterday, gold/dt=2026-04-12/ already has a file.
        # We only delete TODAY's partition (gold/dt=2026-04-13/) so yesterday's data stays.
        # Example: if today's partition has 1 stale file from an earlier rerun → delete it.
        paginator = s3_client.get_paginator("list_objects_v2")
        objects_to_delete = []
        for page in paginator.paginate(Bucket=bucket, Prefix=gold_prefix):
            # page["Contents"] = [{"Key": "gold/dt=2026-04-13/2026-04-13_SearchKeywordPerformance.tab", ...}]
            for obj in page.get("Contents", []):   # "Contents" absent when prefix is empty (first run)
                objects_to_delete.append({"Key": obj["Key"]})

        if objects_to_delete:
            # delete_objects: single API call, up to 1000 keys per request.
            # Quiet=True suppresses per-key success confirmations in the response.
            s3_client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": objects_to_delete, "Quiet": True},
            )
            logger.info(f"Insert-overwrite: deleted {len(objects_to_delete)} existing object(s) from {gold_prefix}")
            # CloudWatch: "Insert-overwrite: deleted 1 existing object(s) from gold/dt=2026-04-13/"

        s3_client.upload_file(output_path, bucket, gold_key)
        # Uploads /tmp/output/2026-04-13_SearchKeywordPerformance.tab
        # → s3://adobe-stg-data-lake/gold/dt=2026-04-13/2026-04-13_SearchKeywordPerformance.tab
        logger.info(f"Uploaded gold output: s3://{bucket}/{gold_key}")

        # ── Step 4: Bronze layers: raw (PII key) + masked (standard key) ────
        # archive_raw: byte-for-byte copy of /tmp/data.sql
        #   → s3://adobe-stg-data-lake/bronze/raw/data.sql
        #   encrypted with PII KMS key — only admin IAM role can decrypt
        raw_key    = archive_raw(bucket, base_name, local_input)

        # archive_masked: same file but ip and user_agent are SHA-256 hashed
        #   ip "44.12.96.2"    → "sha256:3a4b2c..." (71 chars)
        #   user_agent "Mozilla/..." → "sha256:9f1e7d..."
        #   → s3://adobe-stg-data-lake/bronze/masked/data.sql
        #   encrypted with standard KMS key — developers can query via Athena
        masked_key = archive_masked(bucket, base_name, local_input)

        # ── Build response payload ────────────────────────────────────────
        results       = analyzer.get_results()          # sorted list: google/Ipod $290, bing/Zune $250, ...
        total_revenue = sum(r["Revenue"] for r in results)  # 290.0 + 250.0 + 190.0 = 730.0

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message":        "Processing complete",
                "input_file":     f"s3://{bucket}/{key}",
                # "s3://adobe-stg-data-lake/landing/adobe/data.sql"
                "gold_output":    f"s3://{bucket}/{gold_key}",
                # "s3://adobe-stg-data-lake/gold/dt=2026-04-13/2026-04-13_SearchKeywordPerformance.tab"
                "bronze_raw":     f"s3://{bucket}/{raw_key}",
                # "s3://adobe-stg-data-lake/bronze/raw/data.sql"
                "bronze_masked":  f"s3://{bucket}/{masked_key}",
                # "s3://adobe-stg-data-lake/bronze/masked/data.sql"
                "keywords_found": len(results),     # 3  (google/Ipod, bing/Zune, google/ipod)
                "total_revenue":  total_revenue,    # 730.0
            }),
        }

    except Exception as e:
        logger.exception(f"Error processing file: {e}")
        # Example: DQ failure → "Error processing file: DQ checks failed for landing/adobe/data.sql: 1 error(s)"
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)}),
        }
