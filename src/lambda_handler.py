"""
AWS Lambda handler for Search Keyword Performance Analyzer.

Triggered by S3 PutObject events on the landing/ prefix.
Reads the hit-level data file from S3, processes it, and writes:
  - gold/              — Aggregated keyword performance (no PII — safe for all)
  - bronze/raw/        — Original data encrypted with dedicated PII KMS key
                         Only the admin IAM role has kms:Decrypt on this key.
  - bronze/masked/     — Pseudonymized data with SHA-256 hashed ip/user_agent
                         Encrypted with the standard data KMS key (developer accessible).

PII handling policy:
  ip and user_agent are pseudonymized using one-way SHA-256 hashing in the masked layer.
  The raw layer relies on S3 SSE-KMS with a restricted PII key — developers cannot
  decrypt the file even if they somehow obtained the S3 object URL.
"""

import io
import os
import csv
import json
import hashlib
import logging
import boto3
from urllib.parse import unquote_plus
from search_keyword_analyzer import SearchKeywordAnalyzer

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client("s3")

# PII fields to pseudonymize in the masked bronze layer
_PII_FIELDS = ("ip", "user_agent")

# KMS key ARNs injected via Lambda environment variables:
#   PII_KMS_KEY_ARN  — Dedicated PII key; Lambda can encrypt, admin can decrypt, devs cannot
#   KMS_KEY_ARN      — General data key for landing, masked bronze, gold, athena-results
_PII_KMS_KEY_ARN = os.environ.get("PII_KMS_KEY_ARN", "")
_DATA_KMS_KEY_ARN = os.environ.get("KMS_KEY_ARN", "")


def _hash_pii(value: str) -> str:
    """
    One-way SHA-256 pseudonymization for the masked layer.

    Preserves cardinality (same input → same hash) so analytics like
    unique visitor counts remain accurate, while preventing re-identification.
    The 'sha256:' prefix marks the field as pseudonymized for downstream consumers.

    Note: For higher security, use HMAC-SHA256 with a secret salt stored in
    AWS Secrets Manager. Plain SHA-256 is vulnerable to rainbow-table attacks
    on known IP ranges (e.g., RFC-1918 private addresses).
    """
    if not value:
        return value
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_masked_tsv(local_path: str) -> bytes:
    """
    Read a tab-separated file and replace PII fields with SHA-256 hashes.
    Returns the transformed content as bytes, ready for S3 upload.
    """
    with open(local_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        fieldnames = list(reader.fieldnames or [])
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        for row in reader:
            for field in _PII_FIELDS:
                if field in row and row[field]:
                    row[field] = _hash_pii(row[field])
            writer.writerow(row)
    return output.getvalue().encode("utf-8")


def _put_s3_object(bucket: str, key: str, body: bytes, kms_key_arn: str) -> None:
    """Upload bytes to S3 with explicit KMS key for SSE."""
    kwargs = {
        "Bucket": bucket,
        "Key": key,
        "Body": body,
        "ServerSideEncryption": "aws:kms",
    }
    if kms_key_arn:
        kwargs["SSEKMSKeyId"] = kms_key_arn
    s3_client.put_object(**kwargs)


def lambda_handler(event, context):
    """
    Entry point for AWS Lambda.

    Expects an S3 PutObject event on the landing/ prefix. Produces three outputs:
      1. gold/   — Analytics result with no PII
      2. bronze/raw/    — Full data, protected by restricted PII KMS key (admin only)
      3. bronze/masked/ — Pseudonymized data accessible to developers
    """
    try:
        record = event["Records"][0]
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])

        logger.info(f"Processing file: s3://{bucket}/{key}")

        # Download input file to Lambda /tmp
        base_name = os.path.basename(key)
        local_input = f"/tmp/{base_name}"
        s3_client.download_file(bucket, key, local_input)

        # Run analytics — uses raw IP internally for session attribution
        # SearchKeywordAnalyzer never writes IP to its output (gold layer is PII-free)
        analyzer = SearchKeywordAnalyzer(local_input)
        analyzer.process()

        # ── Gold layer: no PII (engine / keyword / revenue only) ──────────────
        output_path = analyzer.write_output("/tmp/output")
        gold_key = f"gold/{os.path.basename(output_path)}"
        s3_client.upload_file(output_path, bucket, gold_key)
        logger.info(f"Uploaded gold output: s3://{bucket}/{gold_key}")

        # ── Bronze/raw: original data, PII KMS key (admin-only decrypt) ───────
        # Developer IAM role lacks kms:Decrypt on PII_KMS_KEY_ARN.
        # Even if a developer has s3:GetObject on this prefix, S3 will refuse
        # to serve the object because they cannot decrypt the envelope key.
        # An S3 bucket policy Deny on bronze/raw/* adds a second enforcement layer.
        raw_bronze_key = f"bronze/raw/{base_name}"
        with open(local_input, "rb") as f:
            raw_bytes = f.read()
        _put_s3_object(bucket, raw_bronze_key, raw_bytes, _PII_KMS_KEY_ARN)
        logger.info(f"Archived raw data (PII-encrypted): s3://{bucket}/{raw_bronze_key}")

        # ── Bronze/masked: SHA-256 hashed PII, standard KMS key ───────────────
        # ip and user_agent replaced with deterministic SHA-256 hashes.
        # Developers can query this layer via Athena (bronze_hits_masked table).
        masked_bronze_key = f"bronze/masked/{base_name}"
        masked_bytes = _write_masked_tsv(local_input)
        _put_s3_object(bucket, masked_bronze_key, masked_bytes, _DATA_KMS_KEY_ARN)
        logger.info(f"Archived masked data (PII-hashed): s3://{bucket}/{masked_bronze_key}")

        results = analyzer.get_results()
        total_revenue = sum(r["Revenue"] for r in results)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Processing complete",
                "input_file": f"s3://{bucket}/{key}",
                "gold_output": f"s3://{bucket}/{gold_key}",
                "bronze_raw": f"s3://{bucket}/{raw_bronze_key}",
                "bronze_masked": f"s3://{bucket}/{masked_bronze_key}",
                "keywords_found": len(results),
                "total_revenue": total_revenue,
            }),
        }

    except Exception as e:
        logger.exception(f"Error processing file: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)}),
        }
