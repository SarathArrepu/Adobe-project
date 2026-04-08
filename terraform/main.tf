terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Remote state — shared across all CI runs and local developer machines.
  # State bucket created once via: aws s3 mb s3://tfstate-search-keyword-analyzer-<account_id>
  backend "s3" {
    bucket = "tfstate-search-keyword-analyzer-107422471374"
    key    = "search-keyword-analyzer/dev/terraform.tfstate"
    region = "us-east-1"
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
