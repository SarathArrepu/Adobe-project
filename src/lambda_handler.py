"""
AWS Lambda handler for Search Keyword Performance Analyzer.

Triggered by S3 PutObject events on the landing/ prefix.
Reads the hit-level data file from S3, processes it, and writes
the output report to the gold/ prefix in the same bucket.
"""

import os
import json
import logging
import boto3
from urllib.parse import unquote_plus
from search_keyword_analyzer import SearchKeywordAnalyzer

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client("s3")


def lambda_handler(event, context):
    """
    Entry point for AWS Lambda.

    Expects an S3 PutObject event. Downloads the file to /tmp,
    runs the analyzer, and uploads the output back to S3.
    """
    try:
        record = event["Records"][0]
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])

        logger.info(f"Processing file: s3://{bucket}/{key}")

        # Download input file to Lambda /tmp
        local_input = f"/tmp/{os.path.basename(key)}"
        s3_client.download_file(bucket, key, local_input)

        # Process
        analyzer = SearchKeywordAnalyzer(local_input)
        analyzer.process()

        # Write output locally then upload to gold/
        output_path = analyzer.write_output("/tmp/output")
        output_filename = os.path.basename(output_path)
        output_key = f"gold/{output_filename}"
        s3_client.upload_file(output_path, bucket, output_key)
        logger.info(f"Uploaded output to s3://{bucket}/{output_key}")

        # Archive raw file to bronze/
        bronze_key = f"bronze/{os.path.basename(key)}"
        s3_client.copy_object(
            Bucket=bucket,
            Key=bronze_key,
            CopySource={"Bucket": bucket, "Key": key},
        )

        results = analyzer.get_results()
        total_revenue = sum(r["Revenue"] for r in results)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Processing complete",
                "input_file": f"s3://{bucket}/{key}",
                "output_file": f"s3://{bucket}/{output_key}",
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
