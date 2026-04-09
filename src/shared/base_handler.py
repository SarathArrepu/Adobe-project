"""
Shared S3/KMS/PII utilities for all pipeline Lambda handlers
=============================================================
Every source-specific handler imports from this module.  Transformation and
business logic live in the individual handler; the plumbing (S3 uploads,
KMS-keyed encryption, PII pseudonymisation) lives here so it can be reused
and tested independently.

PII pseudonymisation strategy
------------------------------
Fields listed in ``PII_FIELDS`` are replaced with deterministic SHA-256
hashes in the masked bronze layer.  Determinism preserves cardinality (the
same IP maps to the same hash on every run) so unique-visitor metrics remain
accurate.

Security note: Plain SHA-256 without a salt is vulnerable to rainbow-table
attacks on known IP ranges (e.g. RFC-1918 private addresses or the small
public IPv4 space).  For higher security, switch to HMAC-SHA256 with a
secret salt stored in AWS Secrets Manager.
"""

import io        # in-memory byte buffer used to build the masked TSV without a temp file
import csv       # standard-library TSV reader/writer
import hashlib   # SHA-256 hashing for PII pseudonymisation
import logging   # structured log output
import os        # environment variable access for KMS key ARNs
import boto3     # AWS SDK — used to create the S3 client

logger = logging.getLogger()  # root logger; level set by Lambda runtime or basicConfig

# Module-level S3 client — created once and reused across invocations to
# benefit from Lambda execution-context reuse (avoids repeated TLS handshakes).
s3_client = boto3.client("s3")

# Tuple of column names that contain PII and must be hashed in the masked layer.
# Using a tuple (not a set) preserves a deterministic iteration order.
PII_FIELDS = ("ip", "user_agent")

# KMS key ARNs injected via Lambda environment variables.
# Defaults to empty string so unit tests can run without AWS credentials.
_DATA_KMS_KEY_ARN = os.environ.get("KMS_KEY_ARN", "")    # standard key — all layers
_PII_KMS_KEY_ARN  = os.environ.get("PII_KMS_KEY_ARN", "") # PII-only key — raw bronze only


def hash_pii(value: str) -> str:
    """
    One-way SHA-256 pseudonymisation for the masked bronze layer.

    The ``sha256:`` prefix marks the output as pseudonymised so downstream
    consumers know the field is not a real IP/user-agent value.

    Args:
        value: The raw PII string to pseudonymise (e.g. an IP address).

    Returns:
        ``"sha256:<hex_digest>"`` for non-empty values; the original value
        unchanged (empty string) when ``value`` is falsy.
    """
    if not value:  # preserve empty/None values as-is — no hash needed
        return value
    # encode to bytes before hashing; hexdigest() returns a lowercase hex string
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_masked_tsv(local_path: str, pii_fields: tuple = PII_FIELDS) -> bytes:
    """
    Read a tab-separated file and return a version with PII fields hashed.

    The file is read from disk but the masked copy is built in memory
    (``io.StringIO``) to avoid writing a temporary file.

    Args:
        local_path:  Path to the original unmasked TSV file on disk.
        pii_fields:  Tuple of column names to pseudonymise.
                     Defaults to ``PII_FIELDS = ("ip", "user_agent")``.

    Returns:
        UTF-8 encoded bytes of the masked TSV, ready for S3 upload.
    """
    with open(local_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")          # read input as {col: val} dicts
        fieldnames = list(reader.fieldnames or [])           # preserve original column order
        output = io.StringIO()                               # in-memory write buffer
        writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()                                 # write column names first

        for row in reader:                   # stream row-by-row — does not load full file
            for field in pii_fields:         # process each PII column
                if field in row and row[field]:  # only hash non-empty values
                    row[field] = hash_pii(row[field])  # replace plaintext with hash
            writer.writerow(row)             # write the (partially) masked row

    return output.getvalue().encode("utf-8")  # convert StringIO to bytes for S3 upload


def put_s3_object(bucket: str, key: str, body: bytes, kms_key_arn: str) -> None:
    """
    Upload bytes to an S3 object with server-side KMS encryption.

    Args:
        bucket:      S3 bucket name.
        key:         S3 object key (path within the bucket).
        body:        Raw bytes to upload.
        kms_key_arn: KMS key ARN for SSE-KMS encryption.  If empty, S3's
                     default KMS key (``aws/s3``) is used instead.
    """
    kwargs = {
        "Bucket": bucket,                    # destination bucket
        "Key":    key,                       # destination object key
        "Body":   body,                      # content to upload
        "ServerSideEncryption": "aws:kms",   # enforce KMS encryption on every upload
    }
    if kms_key_arn:  # specify a customer-managed key if one was provided
        kwargs["SSEKMSKeyId"] = kms_key_arn
    s3_client.put_object(**kwargs)  # single API call — boto3 handles multipart for large bodies


def archive_raw(bucket: str, base_name: str, local_input: str) -> str:
    """
    Copy the original (unmasked) input file to ``bronze/raw/`` using the
    dedicated PII KMS key.

    The PII KMS key policy restricts ``kms:Decrypt`` to the admin IAM role
    only — developers with ``s3:GetObject`` cannot read this object because
    they lack decrypt permission on the key.

    Args:
        bucket:      Destination S3 bucket name.
        base_name:   Filename portion of the input (used as the S3 object key).
        local_input: Local filesystem path of the file downloaded from S3 landing.

    Returns:
        The S3 key where the raw file was written (e.g. ``"bronze/raw/data.sql"``).
    """
    raw_key = f"bronze/raw/{base_name}"     # place under the raw bronze prefix
    with open(local_input, "rb") as f:
        raw_bytes = f.read()                # read entire file as bytes for upload
    put_s3_object(bucket, raw_key, raw_bytes, _PII_KMS_KEY_ARN)  # use PII-restricted key
    logger.info(f"Archived raw data (PII-encrypted): s3://{bucket}/{raw_key}")
    return raw_key  # returned so the Lambda handler can include it in the response payload


def archive_masked(
    bucket: str,
    base_name: str,
    local_input: str,
    pii_fields: tuple = PII_FIELDS,
) -> str:
    """
    Write a pseudonymised copy of the input file to ``bronze/masked/`` using
    the standard data KMS key.

    Developers with access to the standard key can read and query the masked
    bronze layer via Athena without ever seeing real IP addresses or
    user-agent strings.

    Args:
        bucket:      Destination S3 bucket name.
        base_name:   Filename portion of the input.
        local_input: Local filesystem path of the original unmasked file.
        pii_fields:  Columns to pseudonymise (default: ``PII_FIELDS``).

    Returns:
        The S3 key where the masked file was written
        (e.g. ``"bronze/masked/data.sql"``).
    """
    masked_key   = f"bronze/masked/{base_name}"                  # masked bronze prefix
    masked_bytes = write_masked_tsv(local_input, pii_fields)     # build hashed TSV in memory
    put_s3_object(bucket, masked_key, masked_bytes, _DATA_KMS_KEY_ARN)  # standard key
    logger.info(f"Archived masked data (PII-hashed): s3://{bucket}/{masked_key}")
    return masked_key  # returned so the Lambda handler can include it in the response payload
