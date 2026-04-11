#!/usr/bin/env bash
# Build the Lambda deployment package for local development.
#
# Run this before `terraform -chdir=terraform apply` whenever you change
# Python source files.  The CI/CD pipeline runs the equivalent steps
# automatically in the "Package Lambda" job.
#
# Usage:
#   ./scripts/build.sh
#
# Output:
#   dist/lambda.zip  — ready to deploy via Terraform or aws lambda update-function-code

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "Building Lambda deployment package..."

# Clean and recreate staging and dist directories
rm -rf staging
mkdir -p dist staging/shared

# Copy shared utilities (dq_checker, base_handler)
cp -r src/shared/. staging/shared/

# Copy each pipeline module's source package.
# Each modules/<name>/src/ directory contains a Python package named <name>/.
# That package lands at the zip root so Lambda resolves e.g. adobe.handler.lambda_handler.
for module_dir in modules/*/; do
    module_name=$(basename "$module_dir")
    cp -r "${module_dir}src/." staging/
    echo "  Packaged module: $module_name"
done

# Zip the staging directory, excluding Python caches
cd staging && zip -r ../dist/lambda.zip . \
    --exclude "*__pycache__*" \
    --exclude "*.pyc" \
    --exclude "*.pyo"
cd "$REPO_ROOT"

# Clean up staging
rm -rf staging

echo ""
echo "Lambda package: $(du -sh dist/lambda.zip | cut -f1)  →  dist/lambda.zip"
echo ""
echo "Next steps:"
echo "  terraform -chdir=terraform apply"
