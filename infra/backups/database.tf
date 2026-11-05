# Locked buckets for database dumps. Separate from the documents bucket because
# they differ in who can read them (nobody on the app side) and how long they
# live. Restores: RECOVERY.md.

locals {
  db_buckets = {
    hourly = {
      name           = var.db_bucket_name
      retention_days = var.db_hourly_retention_days
    }
    monthly = {
      name           = var.db_monthly_bucket_name
      retention_days = var.db_monthly_retention_days
    }
  }
}

resource "aws_s3_bucket" "db" {
  for_each = local.db_buckets

  bucket = each.value.name

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

resource "aws_s3_bucket_versioning" "db" {
  for_each = local.db_buckets

  bucket = aws_s3_bucket.db[each.key].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "db" {
  for_each = local.db_buckets

  bucket = aws_s3_bucket.db[each.key].id

  rule {
    default_retention {
      mode = var.db_retention_mode
      days = each.value.retention_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.db]
}

resource "aws_s3_bucket_public_access_block" "db" {
  for_each = local.db_buckets

  bucket = aws_s3_bucket.db[each.key].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "db" {
  for_each = local.db_buckets

  bucket = aws_s3_bucket.db[each.key].id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "db" {
  for_each = local.db_buckets

  bucket = aws_s3_bucket.db[each.key].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# The bucket is versioned, so `expiration` only adds a delete marker; it's the
# noncurrent rule that removes the dump.
resource "aws_s3_bucket_lifecycle_configuration" "db" {
  for_each = local.db_buckets

  bucket = aws_s3_bucket.db[each.key].id

  dynamic "rule" {
    for_each = each.key == "hourly" ? [1] : []

    content {
      id     = "expire-dumps"
      status = "Enabled"

      filter {}

      # Past the lock, so a dump is never marked deleted while still locked.
      expiration {
        days = var.db_hourly_retention_days + 2
      }
    }
  }

  rule {
    id     = "remove-noncurrent"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }

  rule {
    id     = "remove-delete-markers"
    status = "Enabled"

    filter {}

    expiration {
      expired_object_delete_marker = true
    }
  }

  depends_on = [aws_s3_bucket_versioning.db]
}

# Add only: dumps hold personal and auth data, and a compromised server must
# not be able to read past ones.
data "aws_iam_policy_document" "db" {
  for_each = local.db_buckets

  statement {
    sid    = "AppAccountAddOnly"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.app_account_id}:root"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.db[each.key].arn}/*"]
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
      aws_s3_bucket.db[each.key].arn,
      "${aws_s3_bucket.db[each.key].arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "db" {
  for_each = local.db_buckets

  bucket = aws_s3_bucket.db[each.key].id
  policy = data.aws_iam_policy_document.db[each.key].json

  depends_on = [aws_s3_bucket_public_access_block.db]
}

# The check for stopped backups runs here, outside the server it watches: no
# PUT into the hourly bucket for two hours raises the alarm, whatever the cause.
resource "aws_s3_bucket_metric" "hourly" {
  bucket = aws_s3_bucket.db["hourly"].id
  name   = "EntireBucket"
}

resource "aws_sns_topic" "alerts" {
  count = var.alert_email == null ? 0 : 1

  name = "lexchain-${var.environment}-backup-alerts"
}

resource "aws_sns_topic_subscription" "alerts" {
  count = var.alert_email == null ? 0 : 1

  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_metric_alarm" "missing_dump" {
  count = var.alert_email == null ? 0 : 1

  alarm_name        = "lexchain-${var.environment}-missing-db-dump"
  alarm_description = "No database dump reached the hourly bucket for two hours."

  namespace   = "AWS/S3"
  metric_name = "PutRequests"
  dimensions = {
    BucketName = aws_s3_bucket.db["hourly"].bucket
    FilterId   = aws_s3_bucket_metric.hourly.name
  }

  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 2
  comparison_operator = "LessThanThreshold"
  threshold           = 1

  # No PUTs means no data points at all, which is the case this alarm is for.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts[0].arn]
  ok_actions    = [aws_sns_topic.alerts[0].arn]
}
