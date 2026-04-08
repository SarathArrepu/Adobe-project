#!/bin/bash
# ============================================================
# End-to-End Demo Script — Interview Live Demo
# Run this to demonstrate the full pipeline working on AWS
# ============================================================
set -e

echo "============================================"
echo "  Search Keyword Performance Analyzer"
echo "  Live Demo"
echo "============================================"
echo ""

# ---- Step 0: Verify AWS credentials ----
echo "[Step 0] Verifying AWS credentials..."
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
if [ -z "$ACCOUNT_ID" ]; then
  echo "ERROR: AWS credentials not configured."
  echo "Run: aws configure"
  echo "  Access Key ID: (from IAM console)"
  echo "  Secret Access Key: (from IAM console)"
  echo "  Region: us-east-1"
  exit 1
fi
echo "  Authenticated as account: $ACCOUNT_ID"
echo ""

# ---- Step 1: Deploy infrastructure ----
echo "[Step 1] Deploying infrastructure with Terraform..."
cd terraform
terraform init -input=false
terraform apply -auto-approve -input=false
echo ""

# Capture outputs
BUCKET=$(terraform output -raw s3_bucket)
LAMBDA=$(terraform output -raw lambda_function)
DB=$(terraform output -raw athena_database)
WORKGROUP=$(terraform output -raw athena_workgroup)
cd ..

echo "  S3 Bucket:       $BUCKET"
echo "  Lambda Function: $LAMBDA"
echo "  Athena Database: $DB"
echo ""

# ---- Step 2: Upload data file to trigger pipeline ----
echo "[Step 2] Uploading data file to S3 landing zone..."
aws s3 cp data/data.sql s3://$BUCKET/landing/data.sql
echo "  Uploaded to s3://$BUCKET/landing/data.sql"
echo ""

# ---- Step 3: Wait for Lambda to process ----
echo "[Step 3] Waiting for Lambda to process (15 seconds)..."
sleep 15

# Check Lambda logs for success
echo "  Checking Lambda execution..."
LOG_STREAM=$(aws logs describe-log-streams \
  --log-group-name "/aws/lambda/$LAMBDA" \
  --order-by LastEventTime --descending \
  --max-items 1 \
  --query 'logStreams[0].logStreamName' --output text 2>/dev/null || echo "NONE")

if [ "$LOG_STREAM" != "NONE" ]; then
  echo "  Lambda executed successfully. Latest log stream: $LOG_STREAM"
else
  echo "  WARNING: No log stream found yet. Lambda may still be running."
  echo "  Waiting another 10 seconds..."
  sleep 10
fi
echo ""

# ---- Step 4: Verify output in Gold layer ----
echo "[Step 4] Checking Gold layer output..."
aws s3 ls s3://$BUCKET/gold/ || echo "  No output yet. Checking..."
echo ""

echo "  Downloading output file..."
mkdir -p output
aws s3 cp s3://$BUCKET/gold/ ./output/ --recursive 2>/dev/null || true
echo ""

if ls output/*_SearchKeywordPerformance.tab 1>/dev/null 2>&1; then
  echo "  === OUTPUT REPORT ==="
  cat output/*_SearchKeywordPerformance.tab
  echo ""
else
  echo "  Output file not found yet. You can check manually:"
  echo "  aws s3 ls s3://$BUCKET/gold/"
fi

# ---- Step 5: Verify Bronze archive ----
echo "[Step 5] Verifying Bronze layer archive..."
aws s3 ls s3://$BUCKET/bronze/
echo ""

# ---- Step 6: Query with Athena ----
echo "[Step 6] Querying Gold table with Athena..."
QUERY_ID=$(aws athena start-query-execution \
  --query-string "SELECT * FROM gold_keyword_performance ORDER BY revenue DESC" \
  --work-group "$WORKGROUP" \
  --query-execution-context "Database=$DB" \
  --query 'QueryExecutionId' --output text 2>/dev/null || echo "SKIP")

if [ "$QUERY_ID" != "SKIP" ]; then
  echo "  Query submitted: $QUERY_ID"
  echo "  Waiting for results..."
  sleep 5
  aws athena get-query-results \
    --query-execution-id "$QUERY_ID" \
    --query 'ResultSet.Rows[*].Data[*].VarCharValue' \
    --output table 2>/dev/null || echo "  Query still running. Check Athena console."
else
  echo "  Athena query skipped (check workgroup permissions)"
fi
echo ""

# ---- Step 7: Show S3 structure (medallion) ----
echo "[Step 7] S3 Medallion Structure:"
echo "  landing/ (ingestion point)"
aws s3 ls s3://$BUCKET/landing/ 2>/dev/null | head -3
echo "  bronze/ (raw archive)"
aws s3 ls s3://$BUCKET/bronze/ 2>/dev/null | head -3
echo "  gold/ (aggregated output)"
aws s3 ls s3://$BUCKET/gold/ 2>/dev/null | head -3
echo ""

echo "============================================"
echo "  Demo Complete!"
echo "============================================"
echo ""
echo "Next steps you can show:"
echo "  - CloudWatch logs: aws logs tail /aws/lambda/$LAMBDA"
echo "  - Athena console: query $DB.gold_keyword_performance"
echo "  - S3 console: browse s3://$BUCKET"
echo ""
echo "To tear down: cd terraform && terraform destroy -auto-approve"
