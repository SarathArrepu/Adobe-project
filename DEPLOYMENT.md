# AWS Deployment Guide

## Step 0: AWS Account Setup (One-Time)

Use your existing AWS free tier account. Do NOT create a new one.

### Create an IAM User for CLI Access

1. Go to **AWS Console > IAM > Users > Create User**
2. User name: `search-keyword-deployer`
3. Attach policy: `AdministratorAccess` (for demo; narrow for production)
4. Go to **Security Credentials > Create Access Key > CLI**
5. Save the Access Key ID and Secret Access Key

### Configure AWS CLI

```bash
aws configure
# Access Key ID:     AKIA...........
# Secret Access Key: xxxxxxxxxxxxxxxxxxxxxxxx
# Region:            us-east-1
# Output format:     json
```

### Verify Authentication

```bash
aws sts get-caller-identity
```

**Never commit credentials to GitHub.**

---

## Step 1: Install Prerequisites

```bash
# Terraform
brew install terraform          # macOS
# OR
sudo apt-get install terraform  # Linux

# Verify
terraform --version   # >= 1.5.0
aws --version          # >= 2.x
python3 --version      # >= 3.8
```

---

## Step 2: Deploy Infrastructure

```bash
cd terraform
terraform init
terraform plan        # Review what will be created
terraform apply       # Type 'yes' to confirm
```

This creates (all free tier eligible except KMS at ~$1/month):

- S3 bucket with medallion prefixes (landing/bronze/silver/gold)
- Lambda function (Python 3.12, 512MB, 5min timeout)
- IAM role with least-privilege S3 + KMS + CloudWatch permissions
- KMS key for encryption at rest
- S3 event notification to trigger Lambda on file upload
- CloudWatch log group + error alarm
- Athena workgroup with cost control (100MB scan limit)
- Glue catalog database + Bronze and Gold table definitions

---

## Step 3: Run the Pipeline (Interview Demo)

```bash
chmod +x demo.sh
./demo.sh
```

This runs the full pipeline end-to-end:
1. Verifies AWS credentials
2. Deploys Terraform infrastructure
3. Uploads data file to S3 landing zone
4. Waits for Lambda processing
5. Downloads and displays output report
6. Queries Gold table via Athena
7. Shows S3 medallion structure

---

## Step 4: Query with Athena

Go to **AWS Console > Athena**, select workgroup `search-keyword-analyzer-dev`, database `search_keyword_analyzer_dev`.

### Gold layer (aggregated results)

```sql
SELECT * FROM gold_keyword_performance ORDER BY revenue DESC;
```

### Bronze layer (raw hit data)

```sql
SELECT pagename, COUNT(*) as hits
FROM bronze_hits
GROUP BY pagename
ORDER BY hits DESC;

SELECT date_time, ip, geo_city, product_list, referrer
FROM bronze_hits
WHERE event_list LIKE '%1%';
```

---

## Tear Down (After Interview)

```bash
cd terraform && terraform destroy -auto-approve
```

---

## Local Testing (No AWS Required)

```bash
python src/search_keyword_analyzer.py data/data.sql -o output/
python -m unittest tests.test_analyzer -v
```
