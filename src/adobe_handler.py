"""
Lambda handler for the Adobe Analytics hit-level pipeline.

Triggered by S3 PutObject events on the landing/adobe/ prefix.
Reads the hit-level TSV, runs keyword attribution, and writes:
  - gold/              — Aggregated keyword performance (no PII)
  - bronze/raw/        — Original data encrypted with dedicated PII KMS key (admin only)
  - bronze/masked/     — SHA-256 hashed ip/user_agent, standard KMS key (developer accessible)

To add a new data source:
  1. Copy this file to src/<source>_handler.py
  2. Replace SearchKeywordAnalyzer with your transformation class
  3. Add a module block in terraform/main.tf (see the salesforce example comment)
  4. terraform apply
"""

import json
import logging
import os
import boto3
from urllib.parse import unquote_plus

from search_keyword_analyzer import SearchKeywordAnalyzer
from base_handler import archive_raw, archive_masked

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client("s3")


def lambda_handler(event, context):
    """
    Entry point for AWS Lambda.

    Expects an S3 PutObject event (delivered via EventBridge) on the landing/ prefix.
    Produces three outputs:
      1. gold/           — Analytics result, no PII
      2. bronze/raw/     — Full data, restricted PII KMS key (admin only)
      3. bronze/masked/  — Pseudonymized ip/user_agent, standard KMS key (developers)
    """
    try:
        record = event["Records"][0]
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])

        logger.info(f"Processing file: s3://{bucket}/{key}")

        base_name = os.path.basename(key)
        local_input = f"/tmp/{base_name}"
        s3_client.download_file(bucket, key, local_input)

        # ── Transformation: Adobe keyword attribution ─────────────────────
        # SearchKeywordAnalyzer uses raw IP internally for session stitching.
        # It never writes IP to the gold output — gold layer is PII-free.
        analyzer = SearchKeywordAnalyzer(local_input)
        analyzer.process()

        # ── Gold layer: aggregated keyword performance, no PII ────────────
        output_path = analyzer.write_output("/tmp/output")
        gold_key = f"gold/{os.path.basename(output_path)}"
        s3_client.upload_file(output_path, bucket, gold_key)
        logger.info(f"Uploaded gold output: s3://{bucket}/{gold_key}")

        # ── Bronze layers: raw (PII key) + masked (standard key) ─────────
        raw_key    = archive_raw(bucket, base_name, local_input)
        masked_key = archive_masked(bucket, base_name, local_input)

        results = analyzer.get_results()
        total_revenue = sum(r["Revenue"] for r in results)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Processing complete",
                "input_file":    f"s3://{bucket}/{key}",
                "gold_output":   f"s3://{bucket}/{gold_key}",
                "bronze_raw":    f"s3://{bucket}/{raw_key}",
                "bronze_masked": f"s3://{bucket}/{masked_key}",
                "keywords_found": len(results),
                "total_revenue":  total_revenue,
            }),
        }

    except Exception as e:
        logger.exception(f"Error processing file: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)}),
        }
