"""
Shared utilities for all pipeline Lambda handlers.

Every source handler imports from here. Transformation logic lives in the source-specific handler; plumbing lives here.
"""

import io
import csv
import hashlib
import logging
import os
import boto3

logger = logging.getLogger()

s3_client = boto3.client("s3")

# PII fields pseudonymized in the masked bronze layer
PII_FIELDS = ("ip", "user_agent")

_DATA_KMS_KEY_ARN = os.environ.get("KMS_KEY_ARN", "")
_PII_KMS_KEY_ARN = os.environ.get("PII_KMS_KEY_ARN", "")


def hash_pii(value: str) -> str:
    """
    One-way SHA-256 pseudonymization for the masked bronze layer.

    Preserves cardinality (same input → same hash) so analytics like unique
    visitor counts remain accurate while preventing re-identification.
    The 'sha256:' prefix marks the field as pseudonymized for downstream consumers.

    Note: For higher security use HMAC-SHA256 with a secret salt stored in
    AWS Secrets Manager. Plain SHA-256 is vulnerable to rainbow-table attacks
    on known IP ranges (e.g. RFC-1918 private addresses).
    """
    if not value:
        return value
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_masked_tsv(local_path: str, pii_fields: tuple = PII_FIELDS) -> bytes:
    """
    Read a tab-separated file, replace PII fields with SHA-256 hashes.
    Returns the transformed content as bytes ready for S3 upload.
    """
    with open(local_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        fieldnames = list(reader.fieldnames or [])
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        for row in reader:
            for field in pii_fields:
                if field in row and row[field]:
                    row[field] = hash_pii(row[field])
            writer.writerow(row)
    return output.getvalue().encode("utf-8")


def put_s3_object(bucket: str, key: str, body: bytes, kms_key_arn: str) -> None:
    """Upload bytes to S3 with SSE-KMS encryption."""
    kwargs = {
        "Bucket": bucket,
        "Key": key,
        "Body": body,
        "ServerSideEncryption": "aws:kms",
    }
    if kms_key_arn:
        kwargs["SSEKMSKeyId"] = kms_key_arn
    s3_client.put_object(**kwargs)


def archive_raw(bucket: str, base_name: str, local_input: str) -> str:
    """
    Copy the original input file to bronze/raw/ using the PII KMS key.
    Developers cannot decrypt this object even with s3:GetObject — the PII key
    restricts kms:Decrypt to the admin role only.

    Returns the S3 key written.
    """
    raw_key = f"bronze/raw/{base_name}"
    with open(local_input, "rb") as f:
        raw_bytes = f.read()
    put_s3_object(bucket, raw_key, raw_bytes, _PII_KMS_KEY_ARN)
    logger.info(f"Archived raw data (PII-encrypted): s3://{bucket}/{raw_key}")
    return raw_key


def archive_masked(bucket: str, base_name: str, local_input: str, pii_fields: tuple = PII_FIELDS) -> str:
    """
    Write a pseudonymized copy of the input file to bronze/masked/.
    PII fields are replaced with deterministic SHA-256 hashes.

    Returns the S3 key written.
    """
    masked_key = f"bronze/masked/{base_name}"
    masked_bytes = write_masked_tsv(local_input, pii_fields)
    put_s3_object(bucket, masked_key, masked_bytes, _DATA_KMS_KEY_ARN)
    logger.info(f"Archived masked data (PII-hashed): s3://{bucket}/{masked_key}")
    return masked_key
