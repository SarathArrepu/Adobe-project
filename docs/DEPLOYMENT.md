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
# Build the Lambda zip first (stages shared + module source, outputs dist/lambda.zip)
./scripts/build.sh

cd terraform
terraform init
terraform plan        # Review what will be created
terraform apply       # Type 'yes' to confirm
```

This creates (all free tier eligible except KMS at ~$1/month):

- S3 bucket with medallion prefixes (`landing/`, `bronze/`, `gold/`)
- Lambda function (`adobe.handler.lambda_handler`, Python 3.12, 512MB, 5min timeout)
- IAM roles: Lambda execution (per pipeline), Admin (PII access), Developer (masked + gold)
- Two KMS keys: standard data key + dedicated PII key
- EventBridge rule routing `landing/adobe/` uploads to the Adobe Lambda
- CloudWatch log group + error alarm
- Athena workgroup with cost control (100 MB scan limit)
- Glue database `stg_adobe` + three Glue tables (`adobe_bronze_masked`, `adobe_bronze_raw`, `adobe_gold`)
- Glue Crawler for daily schema evolution

**Terraform files by concern:**

| File | Contents |
|---|---|
| `main.tf` | Provider + backend only |
| `variables.tf` | All variable declarations |
| `shared.tf` | S3, KMS, IAM, Glue DB, Athena (shared across all pipelines) |
| `observability.tf` | CloudWatch dashboard, Budgets, QuickSight |
| `outputs.tf` | All outputs |
| `modules/pipeline/` | Reusable module: Lambda + IAM + EventBridge + Glue + Crawler |
| `modules/adobe/terraform/pipeline.tf` | Adobe pipeline config (staged to `terraform/` by CI/CD) |

---

## Step 3: Run the Pipeline (Interview Demo)

```bash
chmod +x scripts/demo.sh && ./scripts/demo.sh
```

This runs the full pipeline end-to-end:
1. Verifies AWS credentials
2. Deploys Terraform infrastructure
3. Uploads data file to S3 landing zone (`landing/adobe/`)
4. Waits for Lambda processing
5. Downloads and displays output report
6. Queries Gold table via Athena
7. Shows S3 medallion structure

---

## Step 4: Query with Athena

Go to **AWS Console > Athena**, select workgroup `adobe-stg`, database `stg_adobe`.

### Gold layer (aggregated results — no PII)

```sql
SELECT * FROM stg_adobe.adobe_gold ORDER BY revenue DESC;
```

### Bronze masked layer (pseudonymized hit data — developer accessible)

```sql
SELECT pagename, COUNT(*) as hits
FROM stg_adobe.adobe_bronze_masked
GROUP BY pagename
ORDER BY hits DESC;

SELECT date_time, ip, geo_city, product_list, referrer
FROM stg_adobe.adobe_bronze_masked
WHERE event_list LIKE '%1%';
```

### Bronze raw layer (plaintext PII — admin role only)

```sql
-- Requires admin role and kms:Decrypt on PII key
SELECT date_time, ip, geo_city, product_list
FROM stg_adobe.adobe_bronze_raw
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
# Run all 58 unit tests (26 analyzer + 32 DQ checker)
PYTHONPATH=src python3 -m unittest discover -s tests -p "test_*.py" -v
PYTHONPATH="src:modules/adobe/src" python3 -m unittest discover -s modules/adobe/tests -p "test_*.py" -v

# Run the analyzer locally against the sample data
mkdir -p output
PYTHONPATH="src:modules/adobe/src" python3 modules/adobe/src/adobe/analyzer.py data/adobe/data.sql -o output/
```
