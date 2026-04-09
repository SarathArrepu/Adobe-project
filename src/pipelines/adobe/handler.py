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
   ``gold/``.
4. **Bronze raw** — Original file re-uploaded to ``bronze/raw/`` with the
   dedicated PII KMS key (admin-decrypt only).
5. **Bronze masked** — PII fields (ip, user_agent) SHA-256 hashed; written to
   ``bronze/masked/`` with the standard KMS key (developer-accessible).

Adding a new data source
------------------------
1. Copy this directory to ``src/pipelines/<source>/``
2. Replace ``SearchKeywordAnalyzer`` with your transformation class
3. Add a ``module`` block in ``terraform/pipelines.tf``
4. Run ``terraform apply``
"""

import json                         # used to serialise the Lambda response body
import logging                      # structured log output to CloudWatch
import os                           # file-system path helpers and environment variables
import boto3                        # AWS SDK — S3 download/upload operations
from urllib.parse import unquote_plus  # decode percent-encoded S3 key names (spaces, etc.)

from shared.search_keyword_analyzer import SearchKeywordAnalyzer  # core revenue-attribution logic
from shared.dq_checker import DataQualityChecker                  # input validation gate
from shared.base_handler import archive_raw, archive_masked       # bronze-layer archival helpers

logger = logging.getLogger()         # root logger; level controlled by Lambda env var LOG_LEVEL
logger.setLevel(logging.INFO)        # default to INFO so DQ summaries are always visible

# Module-level S3 client — created once at cold start, reused across warm invocations.
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
        record = event["Records"][0]                           # Lambda delivers one record per invocation
        bucket = record["s3"]["bucket"]["name"]                # S3 bucket that received the upload
        key    = unquote_plus(record["s3"]["object"]["key"])   # decode e.g. "landing/adobe/my%20file.tsv"

        logger.info(f"Processing file: s3://{bucket}/{key}")

        # ── Download the input file to Lambda's ephemeral /tmp storage ───
        base_name   = os.path.basename(key)          # strip the S3 prefix path (e.g. "data.sql")
        local_input = f"/tmp/{base_name}"             # /tmp is the only writable path in Lambda
        s3_client.download_file(bucket, key, local_input)  # blocking download before any processing

        # ── Step 1: Data Quality checks — must pass before any S3 write ──
        # Run the full DQ suite.  If any ERROR-level issue is found (e.g. missing
        # required columns, empty file), raise immediately so nothing is persisted.
        dq_report = DataQualityChecker(local_input).run()  # execute all checks against local file
        dq_report.print_summary()                          # log summary + all issues to CloudWatch
        if not dq_report.passed():                         # at least one ERROR-level issue found
            raise ValueError(
                f"DQ checks failed for {key}: "
                f"{len(dq_report.errors)} error(s) — aborting pipeline, no data written."
            )

        # ── Step 2: Transformation: Adobe keyword attribution ─────────────
        # SearchKeywordAnalyzer streams the file row-by-row (O(unique IPs) memory).
        # run_dq=False because DQ already passed in Step 1 — avoids a second file read.
        analyzer = SearchKeywordAnalyzer(local_input)      # initialise with validated local file
        analyzer.process(run_dq=False)                     # attribute revenue; DQ already cleared

        # ── Step 3: Gold layer: aggregated keyword performance, no PII ──────
        # The gold output contains only (engine, keyword, revenue) — no IP, no user_agent.
        output_path = analyzer.write_output("/tmp/output") # write tab-delimited report to /tmp
        gold_key    = f"gold/{os.path.basename(output_path)}"  # e.g. "gold/2024-01-15_SearchKeywordPerformance.tab"
        s3_client.upload_file(output_path, bucket, gold_key)   # upload gold report to S3
        logger.info(f"Uploaded gold output: s3://{bucket}/{gold_key}")

        # ── Step 4: Bronze layers: raw (PII key) + masked (standard key) ────
        # archive_raw:    re-uploads the original file encrypted with the PII KMS key
        # archive_masked: uploads a copy with ip/user_agent replaced by SHA-256 hashes
        raw_key    = archive_raw(bucket, base_name, local_input)     # admin-only decrypt
        masked_key = archive_masked(bucket, base_name, local_input)  # developer-accessible

        # ── Build response payload ────────────────────────────────────────
        results       = analyzer.get_results()               # sorted list of (engine, kw, revenue) dicts
        total_revenue = sum(r["Revenue"] for r in results)   # aggregate across all keywords

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message":        "Processing complete",
                "input_file":     f"s3://{bucket}/{key}",         # source file that triggered the pipeline
                "gold_output":    f"s3://{bucket}/{gold_key}",    # aggregated report, no PII
                "bronze_raw":     f"s3://{bucket}/{raw_key}",     # original data, PII KMS key
                "bronze_masked":  f"s3://{bucket}/{masked_key}",  # hashed PII, standard KMS key
                "keywords_found": len(results),                    # number of distinct (engine, kw) pairs
                "total_revenue":  total_revenue,                   # sum of all attributed revenue
            }),
        }

    except Exception as e:  # catch-all — DQ failures, S3 errors, parsing errors, etc.
        logger.exception(f"Error processing file: {e}")  # log full stack trace to CloudWatch
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)}),  # surface the error message to the caller
        }
