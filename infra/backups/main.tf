# The Object Lock bucket for archived originals, one per environment (one
# workspace each). Lives in its own state so `tofu destroy` on the app can
# never reach it. Production should also move it to its own AWS account (set
# app_account_id), so a compromised app account cannot override GOVERNANCE
# locks or rewrite the bucket policy.

terraform {
  required_version = ">= 1.8"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project     = "lexchain"
      Component   = "backups"
      Environment = var.environment
      ManagedBy   = "opentofu"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  app_account_id = coalesce(var.app_account_id, data.aws_caller_identity.current.account_id)
}

resource "aws_s3_bucket" "backups" {
  bucket = var.bucket_name

  # Object Lock can only be switched on when the bucket is created.
  object_lock_enabled = true

  lifecycle {
    prevent_destroy = true

    precondition {
      condition     = terraform.workspace == var.environment
      error_message = "Workspace is '${terraform.workspace}' but environment is '${var.environment}'. Run `tofu workspace select ${var.environment}` or pass the matching -var-file."
    }
  }
}

resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id

  # Required by Object Lock; it cannot be suspended afterwards.
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    default_retention {
      mode = var.retention_mode
      days = var.retention_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.backups]
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket = aws_s3_bucket.backups.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "backups" {
  bucket = aws_s3_bucket.backups.id

  # Objects the app account writes are owned by this account, not the writer.
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# The app account is trusted as a whole here; infra/app narrows that to the
# instance role. Trusting the account rather than the role ARN means neither
# config has to be applied before the other, and moving the bucket to its own
# account later is a tfvars change.
data "aws_iam_policy_document" "backups" {
  statement {
    sid    = "AppAccountReadWrite"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.app_account_id}:root"]
    }

    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:GetObjectRetention",
      "s3:ListBucket",
    ]

    resources = [
      aws_s3_bucket.backups.arn,
      "${aws_s3_bucket.backups.arn}/*",
    ]
  }

  # `fetch` reads the newest version, so a junk version over an existing key
  # would break restore even with the original still locked underneath.
  statement {
    sid     = "DenyOverwrite"
    effect  = "Deny"
    actions = ["s3:PutObject"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    resources = ["${aws_s3_bucket.backups.arn}/*"]

    condition {
      test     = "Null"
      variable = "s3:if-none-match"
      values   = ["true"]
    }
  }

  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    resources = [
      aws_s3_bucket.backups.arn,
      "${aws_s3_bucket.backups.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "backups" {
  bucket = aws_s3_bucket.backups.id
  policy = data.aws_iam_policy_document.backups.json

  depends_on = [aws_s3_bucket_public_access_block.backups]
}
