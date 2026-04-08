# Runbook — Search Keyword Performance Analyzer

Complete setup guide, naming standards, error reference, security configuration, and operational procedures.

---

## Table of Contents

1. [AWS Account Setup](#1-aws-account-setup)
2. [IAM User & CLI Configuration](#2-iam-user--cli-configuration)
3. [GitHub Setup & Repository Configuration](#3-github-setup--repository-configuration)
4. [Branch Protection & Git Workflow](#4-branch-protection--git-workflow)
5. [Naming Standards](#5-naming-standards)
6. [Infrastructure Deployment (Terraform)](#6-infrastructure-deployment-terraform)
7. [S3 Lifecycle Rules](#7-s3-lifecycle-rules)
8. [Creating a New Lambda Function](#8-creating-a-new-lambda-function)
9. [Security & Encryption](#9-security--encryption)
10. [PII Data Handling & Role-Based Access Control](#10-pii-data-handling--role-based-access-control)
11. [GitHub Actions CI/CD](#11-github-actions-cicd)
12. [Common Errors & Fixes](#12-common-errors--fixes)
13. [Tear Down](#13-tear-down)

---

## 1. AWS Account Setup

### Create Free Tier Account

1. Go to `https://aws.amazon.com/free` → **Create a Free Account**
2. Enter email, account name (e.g. `sarath-adobe-project`), and password
3. Select **Personal** account type; fill in name, phone, address
4. Enter a credit/debit card (AWS charges $1 to verify — refunded immediately)
5. Phone verification — AWS calls or texts a code
6. Select **Basic (Free)** support plan
7. Sign in at `https://console.aws.amazon.com` as **Root user**

> **Security best practice:** Enable MFA on the root account immediately.
> Console → top-right username → Security credentials → Multi-factor authentication → Assign MFA device

---

## 2. IAM User & CLI Configuration

### Create IAM User for CLI Access

1. Console → **IAM** → **Users** → **Create User**
2. Username: `adobe-project-deployer`
3. Do NOT grant console access (CLI-only user)
4. **Attach policies directly** → `AdministratorAccess` (demo; restrict in production)
5. After creation → **Security credentials** tab → **Create access key**
6. Use case: **Command Line Interface (CLI)**
7. Download the `.csv` — store securely, never commit to git

### Install & Configure AWS CLI

```bash
# macOS
brew install awscli

# Verify
aws --version   # should show aws-cli/2.x

# Configure
aws configure
# AWS Access Key ID:     AKIA...
# AWS Secret Access Key: xxxxxxxx
# Default region name:  us-east-1
# Default output format: json

# Verify credentials
aws sts get-caller-identity
```

Expected output:
```json
{
    "UserId": "AIDA...",
    "Account": "107422471374",
    "Arn": "arn:aws:iam::107422471374:user/adobe-project-deployer"
}
```

---

## 3. GitHub Setup & Repository Configuration

### Install GitHub CLI

```bash
brew install gh
```

### Authenticate

```bash
gh auth login
# Choose: GitHub.com → HTTPS → Yes → Login with a web browser
# Paste the code shown into the browser prompt
```

### Initialize and Push Repository

```bash
cd your-project-folder

git init
git branch -M main
git add .
git commit -m "Initial commit: search keyword analyzer"

git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

### Add AWS Secrets to GitHub

```bash
# Using GitHub CLI (preferred — avoids pasting secrets in browser)
gh secret set AWS_ACCESS_KEY_ID --body "AKIA..."
gh secret set AWS_SECRET_ACCESS_KEY --body "your-secret"

# Verify
gh secret list
```

Or via browser: **Repo → Settings → Secrets and variables → Actions → New repository secret**

---

## 4. Branch Protection & Git Workflow

### Branch Protection Rules (applied to `main`)

Main branch is protected with these rules:
- Direct pushes to `main` are **blocked**
- All changes must come through a **Pull Request**
- PR requires **1 approving review**
- CI checks (**Unit Tests** + **Package Lambda**) must pass before merge
- Force pushes and branch deletion are **disabled**
- Stale approvals are dismissed when new commits are pushed

### Apply Branch Protection via CLI

```bash
gh api repos/OWNER/REPO/branches/main/protection \
  --method PUT \
  --input - <<'EOF'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["Unit Tests", "Package Lambda"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews": true
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
EOF
```

### Developer Workflow

```bash
# 1. Always start from an up-to-date main
git checkout main
git pull origin main

# 2. Create a feature branch (see naming standards below)
git checkout -b feature/SKA-123-add-bing-support

# 3. Make changes, commit often
git add src/search_keyword_analyzer.py
git commit -m "feat: add Bing search engine support"

# 4. Push feature branch
git push -u origin feature/SKA-123-add-bing-support

# 5. Open Pull Request
gh pr create --title "Add Bing support" --body "Adds bing.com to SEARCH_ENGINES map"

# 6. After review and CI passes → merge via GitHub UI (Squash and merge recommended)

# 7. Delete feature branch after merge
git branch -d feature/SKA-123-add-bing-support
git push origin --delete feature/SKA-123-add-bing-support
```

---

## 5. Naming Standards

### Git Branch Naming

| Type | Pattern | Example |
|---|---|---|
| Feature | `feature/<ticket>-<short-desc>` | `feature/SKA-42-yahoo-parser` |
| Bug fix | `fix/<ticket>-<short-desc>` | `fix/SKA-55-revenue-rounding` |
| Hotfix | `hotfix/<ticket>-<short-desc>` | `hotfix/SKA-99-lambda-oom` |
| Release | `release/<version>` | `release/1.2.0` |
| Chore | `chore/<short-desc>` | `chore/update-dependencies` |

**Rules:**
- Lowercase, hyphens only (no underscores, no spaces)
- Include ticket number where applicable
- Keep descriptions short (3–5 words max)

### Git Commit Message Format (Conventional Commits)

```
<type>(<scope>): <short description>

[optional body]
```

| Type | When to use |
|---|---|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation only |
| `test` | Adding or updating tests |
| `refactor` | Code change that is not a feature or fix |
| `chore` | Build process, dependency updates |
| `ci` | CI/CD configuration changes |

Examples:
```
feat(analyzer): add DuckDuckGo search engine support
fix(parser): handle empty product_list without crashing
docs(runbook): add S3 lifecycle rule examples
ci(actions): bump setup-terraform to v3
```

### AWS Resource Naming

Pattern: `{project}-{resource-type}-{environment}`

| Resource | Pattern | Example |
|---|---|---|
| S3 Bucket | `{project}-{env}-{account-id}` | `search-keyword-analyzer-dev-107422471374` |
| Lambda | `{project}-{env}` | `search-keyword-analyzer-dev` |
| IAM Role | `{project}-lambda-{env}` | `search-keyword-analyzer-lambda-dev` |
| KMS Key alias | `alias/{project}-{env}` | `alias/search-keyword-analyzer-dev` |
| CloudWatch Log Group | `/aws/lambda/{lambda-name}` | `/aws/lambda/search-keyword-analyzer-dev` |
| Glue Database | `{project}_{env}` (underscores) | `search_keyword_analyzer_dev` |
| Glue Table | `{layer}_{entity}` | `gold_keyword_performance` |
| Athena Workgroup | `{project}-{env}` | `search-keyword-analyzer-dev` |

### File and Directory Naming

```
src/                        # Python source (snake_case.py)
tests/                      # Test files (test_<module>.py)
terraform/                  # Terraform files (main.tf, variables.tf)
data/                       # Input data files
output/                     # Generated output (gitignored)
dist/                       # Build artifacts (gitignored)
.github/workflows/          # GitHub Actions (kebab-case.yml)
```

### Output File Naming

`YYYY-MM-DD_SearchKeywordPerformance.tab`

---

## 6. Infrastructure Deployment (Terraform)

### Prerequisites

```bash
brew install terraform
terraform --version   # >= 1.5.0
```

### First-Time Deploy

```bash
cd terraform
terraform init        # download providers
terraform plan        # review changes
terraform apply       # type 'yes' to confirm
```

### Update Existing Infrastructure

```bash
cd terraform
terraform plan        # always review before applying
terraform apply
```

### View Outputs

```bash
terraform output
```

---

## 7. S3 Lifecycle Rules

Lifecycle rules are defined in `terraform/main.tf` under `aws_s3_bucket_lifecycle_configuration`.

### Current Rules

| Prefix | Action | After |
|---|---|---|
| `landing/` | Transition to GLACIER | 30 days |
| `bronze/` | Transition to STANDARD_IA | 90 days |
| `gold/` | Transition to STANDARD_IA | 180 days |
| `gold/` | Expire (delete) | 365 days |
| `athena-results/` | Expire (delete) | 7 days |

### Adding a New Lifecycle Rule

In `terraform/main.tf`, add a new `rule` block inside `aws_s3_bucket_lifecycle_configuration`:

```hcl
rule {
  id     = "archive-silver"
  status = "Enabled"
  filter { prefix = "silver/" }
  transition {
    days          = 60
    storage_class = "STANDARD_IA"
  }
  transition {
    days          = 180
    storage_class = "GLACIER"
  }
  expiration {
    days = 730   # delete after 2 years
  }
}
```

Storage class reference:

| Class | Cost vs STANDARD | Use case |
|---|---|---|
| `STANDARD` | baseline | Frequently accessed |
| `STANDARD_IA` | ~46% cheaper | Infrequently accessed, kept long-term |
| `GLACIER` | ~68% cheaper | Archive, rarely accessed |
| `DEEP_ARCHIVE` | ~95% cheaper | Long-term compliance archive |

### Apply Changes

```bash
cd terraform && terraform apply
```

---

## 8. Creating a New Lambda Function

### Step 1 — Write the handler

Create `src/your_new_handler.py`:

```python
import json
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    logger.info(f"Event: {json.dumps(event)}")
    # your logic here
    return {"statusCode": 200, "body": json.dumps({"status": "ok"})}
```

### Step 2 — Add to Terraform

In `terraform/main.tf`:

```hcl
data "archive_file" "new_function_zip" {
  type        = "zip"
  source_dir  = "${path.module}/../src"
  output_path = "${path.module}/../dist/new_function.zip"
  excludes    = ["__pycache__"]
}

resource "aws_lambda_function" "new_function" {
  function_name    = "${var.project_name}-new-function-${var.environment}"
  role             = aws_iam_role.lambda_role.arn
  handler          = "your_new_handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 60
  memory_size      = 256
  filename         = data.archive_file.new_function_zip.output_path
  source_code_hash = data.archive_file.new_function_zip.output_base64sha256

  environment {
    variables = {
      ENVIRONMENT = var.environment
      LOG_LEVEL   = "INFO"
    }
  }
}

resource "aws_cloudwatch_log_group" "new_function_logs" {
  name              = "/aws/lambda/${aws_lambda_function.new_function.function_name}"
  retention_in_days = 14
}
```

### Step 3 — Add IAM permissions if needed

```hcl
resource "aws_iam_role_policy" "new_function_s3" {
  name = "new-function-s3-access"
  role = aws_iam_role.lambda_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:PutObject"]
      Resource = "${aws_s3_bucket.data_lake.arn}/*"
    }]
  })
}
```

### Step 4 — Deploy

```bash
cd terraform && terraform apply
```

### Step 5 — Test manually

```bash
aws lambda invoke \
  --function-name search-keyword-analyzer-new-function-dev \
  --payload '{"test": "event"}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/response.json && cat /tmp/response.json
```

---

## 9. Security & Encryption

### Encryption at Rest

| Resource | Encryption | Key |
|---|---|---|
| S3 bucket | SSE-KMS | Customer-managed KMS key |
| S3 objects | Bucket-key enabled | Reduces KMS API calls by ~99% |
| Athena results | SSE-KMS | Same KMS key |
| Glue Catalog | SSE-KMS | Customer-managed KMS key |
| Lambda env vars | AWS-managed KMS | Per-function |

### Enable Glue Data Catalog Encryption

Add to `terraform/main.tf`:

```hcl
resource "aws_glue_data_catalog_encryption_settings" "catalog" {
  data_catalog_encryption_settings {
    connection_password_encryption {
      aws_kms_key_id                       = aws_kms_key.data_key.arn
      return_connection_password_encrypted = true
    }
    encryption_at_rest {
      catalog_encryption_mode = "SSE-KMS"
      sse_aws_kms_key_id      = aws_kms_key.data_key.arn
    }
  }
}
```

### KMS Key Rotation

Automatic annual rotation is enabled:
```hcl
resource "aws_kms_key" "data_key" {
  enable_key_rotation = true
}
```

### S3 Block Public Access

All public access is blocked:
```hcl
resource "aws_s3_bucket_public_access_block" "data_lake" {
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
```

### IAM Least Privilege

The Lambda execution role has only what it needs:
- `s3:GetObject`, `s3:PutObject`, `s3:CopyObject`, `s3:ListBucket` on the data lake bucket
- `kms:Decrypt`, `kms:GenerateDataKey` on the project KMS key
- `logs:*` via AWSLambdaBasicExecutionRole (CloudWatch only)

No `s3:*` wildcard. No `iam:*`. No cross-account access.

### Secrets Management

**Never hardcode credentials.** Use:
- AWS credentials → `aws configure` locally, GitHub Secrets in CI/CD
- Lambda environment variables for non-sensitive config only
- AWS Secrets Manager for sensitive runtime secrets:

```python
import boto3, json

def get_secret(secret_name):
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_name)
    return json.loads(response["SecretString"])
```

### GitHub Secrets

| Secret | Purpose |
|---|---|
| `AWS_ACCESS_KEY_ID` | GitHub Actions AWS authentication |
| `AWS_SECRET_ACCESS_KEY` | GitHub Actions AWS authentication |

---

## 10. PII Data Handling & Role-Based Access Control

### PII Fields in This Dataset

| Field | Classification | Sensitivity |
|---|---|---|
| `ip` | Direct PII — visitor identifier | High |
| `user_agent` | Quasi-identifier (device/browser fingerprint) | Medium |
| `geo_city` / `geo_region` / `geo_country` | Location data | Low–Medium |
| `date_time` | Timestamp — identifying when combined with above | Low |

The gold output layer (`search_engine_domain`, `search_keyword`, `revenue`) contains **no PII** and is safe for all roles.

---

### Data Lakehouse PII Architecture

```
Landing/data.sql  ──►  Lambda
                           │
               ┌───────────┼───────────┐
               ▼           ▼           ▼
        bronze/raw/   bronze/masked/  gold/
         (plaintext)   (SHA-256 hash) (no PII)
         PII KMS Key   Standard KMS   Standard KMS
         Admin only    Dev + Admin    Dev + Admin
```

**Three enforcement layers per data zone:**

| Layer | Mechanism | Who Enforces |
|---|---|---|
| 1. KMS key policy | Developer has no `kms:Decrypt` on `pii_key` | AWS KMS |
| 2. IAM role policy | Developer Deny on `s3:*` for `bronze/raw/*` | AWS IAM |
| 3. S3 bucket policy | Bucket-level Deny on `bronze/raw/*` for developer role | AWS S3 |

A **bucket policy Deny** cannot be overridden by an IAM Allow — this is the definitive backstop.

---

### The Two IAM Roles

#### Admin Role (`search-keyword-analyzer-admin-dev`)
- S3: Full read/write on all layers including `bronze/raw/`
- KMS: `kms:Decrypt` on **both** `data_key` and `pii_key`
- Glue/Athena: Access to ALL tables including `bronze_hits_raw`

```bash
# Assume admin role
aws sts assume-role \
  --role-arn $(cd terraform && terraform output -raw admin_role_arn) \
  --role-session-name admin-pii-session
```

#### Developer Role (`search-keyword-analyzer-developer-dev`)
- S3: Read-only on `bronze/masked/*` and `gold/*` only
- KMS: `kms:Decrypt` on `data_key` only — **pii_key is explicitly absent**
- Glue/Athena: Access to `bronze_hits_masked` and `gold_keyword_performance` only

```bash
# Assume developer role
aws sts assume-role \
  --role-arn $(cd terraform && terraform output -raw developer_role_arn) \
  --role-session-name dev-session
```

---

### Bronze Layer: Two Glue Tables

| Glue Table | S3 Prefix | `ip` Column | `user_agent` Column | Who Can Query |
|---|---|---|---|---|
| `bronze_hits_raw` | `bronze/raw/` | Plaintext (e.g. `64.233.160.0`) | Plaintext (full UA string) | Admin only |
| `bronze_hits_masked` | `bronze/masked/` | `sha256:a3f...` (hash) | `sha256:7b2...` (hash) | Admin + Developer |

**Why two tables instead of Lake Formation column masking?**
Lake Formation column masking requires additional service enablement and does not prevent a developer from downloading the underlying S3 file. Two separate S3 prefixes with different KMS keys provides stronger guarantees.

---

### How Admins Access Plaintext PII

Admins need `kms:Decrypt` on the PII key plus `s3:GetObject` on `bronze/raw/`.

**Via Athena (recommended — audit trail via CloudTrail):**
```sql
-- Query bronze_hits_raw as admin role
-- Each query is logged in CloudTrail with the caller's identity
SELECT date_time, ip, geo_city, pagename
FROM bronze_hits_raw
WHERE event_list LIKE '%1%'
ORDER BY date_time;
```

**Via CLI (for investigation of a specific IP):**
```bash
# Download a raw file (requires admin role credentials and pii_key decrypt access)
aws s3 cp s3://$BUCKET/bronze/raw/data.sql /tmp/raw_data.sql
```

**Decryption audit:** Every `kms:Decrypt` call on `pii_key` is logged in CloudTrail under `KMS > Decrypt`. This creates a full audit trail of who accessed plaintext PII data, when, and from which IP.

---

### How Developers Work with Data

Developers query `bronze_hits_masked` — they see hashed values for `ip` and `user_agent`:

```sql
-- Count unique visitors (hash cardinality is preserved — same IP = same hash)
SELECT COUNT(DISTINCT ip) AS unique_visitors
FROM bronze_hits_masked;

-- Joining masked bronze to gold works by hash (no real IP needed for analytics)
SELECT b.pagename, g.search_keyword, g.revenue
FROM bronze_hits_masked b
JOIN gold_keyword_performance g ON b.referrer LIKE '%' || g.search_engine_domain || '%'
ORDER BY g.revenue DESC;
```

> Developers can count, group, and join on `ip` because SHA-256 is deterministic. They cannot reverse the hash to find the original IP address without a rainbow table (impractical for arbitrary IPs outside RFC-1918 ranges).

---

### Production Hardening (Beyond This Assessment)

| Enhancement | Description | Priority |
|---|---|---|
| HMAC-SHA256 with secret salt | Replace plain SHA-256 to prevent rainbow-table attacks on private IP ranges | High |
| AWS Lake Formation | Column-level security in Athena without requiring separate S3 prefixes | Medium |
| PII data retention policy | Auto-delete `bronze/raw/` after legal retention period (e.g. 90 days) | High |
| Athena audit alerts | CloudWatch alarm on `kms:Decrypt` calls on `pii_key` > threshold | Medium |
| VPC endpoint for S3/KMS | Ensure PII data never traverses the public internet | High |
| AWS Macie | Automated PII discovery in S3 to catch accidental PII leakage in other layers | Medium |

---

## 11. GitHub Actions CI/CD

### Pipeline Overview

```
Push to main / PR opened / Manual trigger
                  │
                  ▼
         ┌────────────────┐
         │   Unit Tests   │   python -m unittest
         └───────┬────────┘
                 │ pass
                 ▼
         ┌────────────────┐
         │ Package Lambda │   zip src/ → upload artifact (30-day retention)
         └───────┬────────┘
                 │
         ┌───────┴────────┐
         ▼                ▼
     [PR only]        [push/main]
  Terraform Plan     Terraform Apply
  (PR comment)       (deploy to AWS)
```

### Trigger Types

| Trigger | Jobs that run |
|---|---|
| Pull Request → main | Tests, Package, Terraform Plan (posted as PR comment) |
| Push to main | Tests, Package, Deploy (Terraform apply) |
| Manual (`workflow_dispatch`) | Tests, Package, Deploy |

### Useful CLI Commands

```bash
# List recent runs
gh run list --limit 10

# View a run
gh run view <run-id>

# View full logs
gh run view --log <run-id>

# Re-run a failed job
gh run rerun <run-id>

# Manually trigger deploy
gh workflow run ci-cd.yml

# View secrets
gh secret list
```

---

## 12. Common Errors & Fixes

### AWS Setup

| Error | Cause | Fix |
|---|---|---|
| `NoCredentials: Unable to locate credentials` | `aws configure` not run | Run `aws configure` |
| `InvalidClientTokenId` | Wrong Access Key ID | Re-check key in IAM → Security credentials |
| `AccessDenied` | Missing IAM permissions | Attach `AdministratorAccess` or specific policy |
| `ExpiredToken` | Temporary credentials expired | Re-run `aws configure` with fresh keys |

### Terraform

| Error | Cause | Fix |
|---|---|---|
| `AlreadyExistsException: alias/...` | Resources exist but no local state file | Run `terraform import aws_kms_alias.data_key alias/search-keyword-analyzer-dev` |
| `BucketAlreadyExists` | S3 bucket name taken | Change bucket name in `main.tf` |
| `EntityAlreadyExists: Role` | IAM role exists, no state | `terraform import aws_iam_role.lambda_role search-keyword-analyzer-lambda-dev` |
| `terraform: command not found` | Not installed | `brew install terraform` |
| `Provider version conflict` | Lock file mismatch | Delete `.terraform.lock.hcl`, run `terraform init -upgrade` |

### GitHub / Git

| Error | Cause | Fix |
|---|---|---|
| `Device not configured` on push | No GitHub auth | Run `gh auth login` |
| `remote: Repository not found` | Wrong remote URL | `git remote set-url origin https://github.com/OWNER/REPO.git` |
| `Updates were rejected` | Remote has newer commits | `git pull --rebase origin main` then push |
| `Protected branch` on direct push | Branch protection enabled | Create feature branch, open a PR |
| `required_status_checks context mismatch` | Job name differs from protection rule | Match `name:` in workflow to context string in branch protection |

### Lambda / S3

| Error | Cause | Fix |
|---|---|---|
| `Runtime.ImportModuleError` | Module missing from zip | Ensure all source files are in `src/` |
| `Task timed out after 300 seconds` | File too large | Increase Lambda timeout or use Glue/EMR |
| `No space left on device` | `/tmp` full (512 MB limit) | Delete temp files after processing |
| `S3 event not triggering Lambda` | Missing invoke permission | Check `aws_lambda_permission` in Terraform |
| `KMS AccessDenied` | Lambda role missing KMS perms | Add `kms:Decrypt` and `kms:GenerateDataKey` to IAM policy |

### GitHub Actions

| Error | Cause | Fix |
|---|---|---|
| `secrets.AWS_* not set` | Secrets missing | `gh secret set AWS_ACCESS_KEY_ID --body "..."` |
| Deploy job never starts | `environment: production` gate | Remove `environment:` field or create env in repo settings |
| `Required status check not complete` | CI job name mismatch | Match `name:` in workflow to exact string in branch protection |

---

## 13. Tear Down

```bash
# Remove all AWS resources
cd terraform && terraform destroy -auto-approve

# Verify bucket is gone
aws s3 ls | grep search-keyword

# Verify Lambda is gone
aws lambda list-functions --query 'Functions[?starts_with(FunctionName, `search-keyword`)].FunctionName'
```

> After destroying, the KMS key enters a **7-day pending deletion window** (configured via `deletion_window_in_days = 7`). It cannot be recovered after deletion completes.
