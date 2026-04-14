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
# Example: if Lambda processes 50 files in a warm burst, only 1 TLS handshake is needed.
s3_client = boto3.client("s3")

# Tuple of column names that contain PII and must be hashed in the masked layer.
# Using a tuple (not a set) preserves a deterministic iteration order.
# Example: PII_FIELDS = ("ip", "user_agent")
#   "ip"         → "67.98.123.1"       hashed to "sha256:3a4b2c1d..." in masked layer
#   "user_agent" → "Mozilla/5.0 ..."   hashed to "sha256:9f1e7d8a..." in masked layer
PII_FIELDS = ("ip", "user_agent")

# KMS key ARNs injected via Lambda environment variables (set in Terraform main.tf).
# Defaults to empty string so unit tests can run without AWS credentials.
# Example: KMS_KEY_ARN     = "arn:aws:kms:us-east-1:123456789:key/abc-123"  (standard, all devs)
# Example: PII_KMS_KEY_ARN = "arn:aws:kms:us-east-1:123456789:key/xyz-789"  (admin-only decrypt)
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
        # Example: hash_pii("") → ""   hash_pii(None) → None

    # encode to bytes before hashing; hexdigest() returns a 64-char lowercase hex string
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
    # Example: hash_pii("67.98.123.1")
    #   → "sha256:" + sha256("67.98.123.1") → "sha256:3a4b..." (71 chars total)
    #
    # Example: hash_pii("44.12.96.2")
    #   → "sha256:9f1e7d..."  (different IP → completely different hash — no partial match)
    #
    # Example: hash_pii("Mozilla/5.0 (Windows; U; Windows NT 5.1 ...)")
    #   → "sha256:c8a2f1..."
    #
    # The "sha256:" prefix (7 chars) + 64-char hex digest = 71 chars total.
    # This is why all bronze columns must be type "string" not "varchar(n)" in Glue —
    # any varchar limit short enough for a real IP (e.g. varchar(20)) would truncate the hash.


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
        reader = csv.DictReader(f, delimiter="\t")
        # DictReader reads by column NAME from the header row — not by position.
        # This is critical: even if Glue schema has columns in a different order,
        # masking correctly finds "ip" and "user_agent" by name.
        #
        # Example — row 2 of data.sql as DictReader sees it:
        # {
        #   "hit_time_gmt": "1254033280",
        #   "date_time":    "2009-09-27 06:34:40",
        #   "user_agent":   "Mozilla/5.0 (Windows; U; Windows NT 5.1 ...)",  ← will be hashed
        #   "ip":           "67.98.123.1",                                    ← will be hashed
        #   "event_list":   "",
        #   ...
        # }

        fieldnames = list(reader.fieldnames or [])
        # ["hit_time_gmt", "date_time", "user_agent", "ip", "event_list", "geo_city", ...]
        # Preserves the original column order from the source file header.

        output = io.StringIO()   # in-memory buffer — no temp file on disk for the masked copy
        writer = csv.DictWriter(output, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        # Writes: "hit_time_gmt\tdate_time\tuser_agent\tip\tevent_list\tgeo_city\t...\n"

        for row in reader:
            for field in pii_fields:  # ("ip", "user_agent")
                if field in row and row[field]:  # skip if field absent or already empty
                    row[field] = hash_pii(row[field])
                    # row 2 before: row["ip"] = "67.98.123.1"
                    # row 2 after:  row["ip"] = "sha256:3a4b2c..."
                    # row 2 before: row["user_agent"] = "Mozilla/5.0 (Windows ...)"
                    # row 2 after:  row["user_agent"] = "sha256:c8a2f1..."
                    # All other fields (geo_city, pagename, etc.) are unchanged.
            writer.writerow(row)
            # Writes masked row to the in-memory buffer

    return output.getvalue().encode("utf-8")
    # output.getvalue() = full masked TSV as a string
    # .encode("utf-8")  = converts to bytes for S3 put_object Body parameter
    # This bytes object is the complete bronze/masked/data.sql file content.


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
        "Bucket": bucket,                    # "adobe-stg-data-lake"
        "Key":    key,                       # "bronze/raw/data.sql" or "bronze/masked/data.sql"
        "Body":   body,                      # raw bytes of the file content
        "ServerSideEncryption": "aws:kms",   # enforce KMS encryption — never plain S3 default
        # Without this: S3 could store the object with SSE-S3 (AWS-managed key),
        # and developers with s3:GetObject could read the raw bronze file, bypassing PII isolation.
    }
    if kms_key_arn:  # specify a customer-managed key if one was provided
        kwargs["SSEKMSKeyId"] = kms_key_arn
        # raw bronze:    SSEKMSKeyId = PII KMS key ARN   → only admin IAM role can decrypt
        # masked bronze: SSEKMSKeyId = standard KMS key  → developer IAM role can decrypt
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
    raw_key = f"bronze/raw/{base_name}"
    # base_name = "data.sql"  →  raw_key = "bronze/raw/data.sql"

    with open(local_input, "rb") as f:
        raw_bytes = f.read()
        # Reads /tmp/data.sql as raw bytes — byte-for-byte copy, no parsing or modification.
        # The file contains the original TSV with plaintext IPs and user-agent strings.
        # Example: row 16 in raw bytes still has "23.8.61.21" and "Mozilla/5.0 (Macintosh..."

    put_s3_object(bucket, raw_key, raw_bytes, _PII_KMS_KEY_ARN)
    # Uploads to: s3://adobe-stg-data-lake/bronze/raw/data.sql
    # Encrypted with PII KMS key → only admin IAM role (aws_iam_role.admin_role) can decrypt.
    # Developers with s3:GetObject still cannot read it — they lack kms:Decrypt on the PII key.

    logger.info(f"Archived raw data (PII-encrypted): s3://{bucket}/{raw_key}")
    # CloudWatch: "Archived raw data (PII-encrypted): s3://adobe-stg-data-lake/bronze/raw/data.sql"
    return raw_key  # "bronze/raw/data.sql" — included in Lambda response payload


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
    masked_key   = f"bronze/masked/{base_name}"
    # base_name = "data.sql"  →  masked_key = "bronze/masked/data.sql"

    masked_bytes = write_masked_tsv(local_input, pii_fields)
    # write_masked_tsv reads /tmp/data.sql and hashes PII fields:
    #   ip:         "67.98.123.1"       → "sha256:3a4b2c..."
    #   ip:         "23.8.61.21"        → "sha256:7f9d1e..."
    #   ip:         "44.12.96.2"        → "sha256:b2c5a8..."
    #   user_agent: "Mozilla/5.0 ..."   → "sha256:c8a2f1..."
    # All other columns (geo_city, pagename, product_list, referrer) are unchanged.
    # Returns the complete masked TSV as bytes — no temp file written.

    put_s3_object(bucket, masked_key, masked_bytes, _DATA_KMS_KEY_ARN)
    # Uploads to: s3://adobe-stg-data-lake/bronze/masked/data.sql
    # Encrypted with standard KMS key → developer IAM role can decrypt and query via Athena.
    # Athena query example: SELECT ip, pagename FROM analytics_db.adobe_bronze_masked LIMIT 10
    # → ip column shows "sha256:3a4b2c..." not "67.98.123.1"

    logger.info(f"Archived masked data (PII-hashed): s3://{bucket}/{masked_key}")
    # CloudWatch: "Archived masked data (PII-hashed): s3://adobe-stg-data-lake/bronze/masked/data.sql"
    return masked_key  # "bronze/masked/data.sql" — included in Lambda response payload
