# Part 0: Remote state bucket
#
# Creates the S3 bucket that stores Terraform state for every other stack
# (2_sagemaker ... 8_enterprise). Run this once per AWS account, before the
# other stacks.
#
# This stack keeps its own state locally, since the bucket can't hold the
# state of the stack that creates it. The state is tiny and only describes
# this bucket; if it's lost, `terraform import` the bucket back.

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "state" {
  # Account ID makes the name globally unique
  bucket = "alex-terraform-state-${data.aws_caller_identity.current.account_id}"

  # Deleting this bucket would orphan every stack's infrastructure
  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Project   = "alex"
    Part      = "0_state_bucket"
    ManagedBy = "terraform"
  }
}

# Keep old state versions so a bad apply or corrupted state can be rolled back
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Don't keep old versions forever
resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

# State contains secrets (e.g. database passwords), so encrypt it at rest
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Reject any request that isn't over HTTPS
resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.state.arn,
        "${aws_s3_bucket.state.arn}/*",
      ]
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.state]
}
