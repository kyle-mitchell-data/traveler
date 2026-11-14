variable "account_id" { type = string }
variable "home_region" { type = string }
variable "allowlist" { type = list(string) }

# Account baseline (decision D32). Everything here is free or pennies: the first copy
# of management events is free, and the log bucket holds a year of gzipped JSON.

locals {
  trail_name = "traveler-audit"
  trail_arn  = "arn:aws:cloudtrail:${var.home_region}:${var.account_id}:trail/${local.trail_name}"
}

# ---------------------------------------------------------------------------
# CloudTrail: one multi-region trail of management events, kept for a year.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "audit" {
  bucket = "traveler-audit-${var.account_id}"
}

resource "aws_s3_bucket_public_access_block" "audit" {
  bucket                  = aws_s3_bucket.audit.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id

  rule {
    id     = "expire-after-a-year"
    status = "Enabled"
    filter {}
    expiration {
      days = 365
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

data "aws_iam_policy_document" "audit" {
  statement {
    sid       = "TrailAclCheck"
    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.audit.arn]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.trail_arn]
    }
  }

  statement {
    sid       = "TrailWrite"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.audit.arn}/AWSLogs/${var.account_id}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.trail_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
  }

  statement {
    sid       = "TlsOnly"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.audit.arn, "${aws_s3_bucket.audit.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "audit" {
  bucket = aws_s3_bucket.audit.id
  policy = data.aws_iam_policy_document.audit.json

  depends_on = [aws_s3_bucket_public_access_block.audit]
}

resource "aws_cloudtrail" "audit" {
  name                          = local.trail_name
  s3_bucket_name                = aws_s3_bucket.audit.id
  is_multi_region_trail         = true
  include_global_service_events = true
  enable_log_file_validation    = true

  depends_on = [aws_s3_bucket_policy.audit]
}

# ---------------------------------------------------------------------------
# IAM Access Analyzer: flags any role, bucket, key or function shared outside the
# account. Free for external-access findings.
# ---------------------------------------------------------------------------

resource "aws_accessanalyzer_analyzer" "account" {
  analyzer_name = "traveler-external-access"
  type          = "ACCOUNT"
}

# ---------------------------------------------------------------------------
# Account-wide guards. The site stays public through CloudFront (origin access
# control), never through a public bucket, so blocking public S3 access is safe.
# ---------------------------------------------------------------------------

resource "aws_s3_account_public_access_block" "account" {
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Nothing uses EBS; this only makes sure anything ever created is encrypted.
resource "aws_ebs_encryption_by_default" "region" {
  for_each = toset(var.allowlist)

  region  = each.key
  enabled = true
}

output "trail_arn" { value = aws_cloudtrail.audit.arn }
output "audit_bucket" { value = aws_s3_bucket.audit.id }
