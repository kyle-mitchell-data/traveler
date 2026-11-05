variable "home_region" { type = string }
variable "allowlist" { type = list(string) }

# Single-table design: STATE, SEQ, EVENT, HOP#, REGION#, CONFIG partitions.
resource "aws_dynamodb_table" "tower" {
  name         = "traveler-tower"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  # EVENT items carry expires_at (epoch seconds) for 7-day retention.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  # Feeds the archiver in phase 6.
  stream_enabled   = true
  stream_view_type = "NEW_IMAGE"
}

# Runtime config. kill and pin_awake are flipped by make sleep/wake, so
# Terraform sets them once and then leaves them alone.
resource "aws_ssm_parameter" "allowlist" {
  name  = "/traveler/allowlist"
  type  = "StringList"
  value = join(",", var.allowlist)
}

resource "aws_ssm_parameter" "home_region" {
  name  = "/traveler/home_region"
  type  = "String"
  value = var.home_region
}

resource "aws_ssm_parameter" "auto_sleep_hours" {
  name  = "/traveler/auto_sleep_hours"
  type  = "String"
  value = "12"
}

resource "aws_ssm_parameter" "switch" {
  for_each = toset(["kill", "pin_awake"])

  name  = "/traveler/${each.key}"
  type  = "String"
  value = "false"

  lifecycle {
    ignore_changes = [value]
  }
}

variable "tower_source" { type = string }
variable "log_retention_days" { type = number }

# ---------------------------------------------------------------------------
# Release store: versioned traveler packages. The traveler copies one forward
# at each hop, so nothing is stored per region.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "releases" {
  bucket_prefix = "traveler-releases-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "releases" {
  bucket                  = aws_s3_bucket.releases.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "releases" {
  bucket = aws_s3_bucket.releases.id
  versioning_configuration {
    status = "Enabled"
  }
}

# ---------------------------------------------------------------------------
# Tower read function: read-only, behind CloudFront with a 1-second cache.
# ---------------------------------------------------------------------------

data "archive_file" "tower" {
  type        = "zip"
  source_file = var.tower_source
  output_path = "${path.root}/.build/tower.zip"
}

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "tower_read" {
  name               = "traveler-tower-read"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "tower_read" {
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.tower.arn]
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.tower_read.arn}:*"]
  }
}

resource "aws_iam_role_policy" "tower_read" {
  name   = "tower-read"
  role   = aws_iam_role.tower_read.id
  policy = data.aws_iam_policy_document.tower_read.json
}

resource "aws_cloudwatch_log_group" "tower_read" {
  name              = "/aws/lambda/traveler-tower"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "tower_read" {
  function_name    = "traveler-tower"
  role             = aws_iam_role.tower_read.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.tower.output_path
  source_code_hash = data.archive_file.tower.output_base64sha256
  memory_size      = 128
  timeout          = 5

  environment {
    variables = { TOWER_TABLE = aws_dynamodb_table.tower.name }
  }

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.tower_read.name
  }
}

resource "aws_lambda_function_url" "tower_read" {
  function_name      = aws_lambda_function.tower_read.function_name
  authorization_type = "AWS_IAM"
}

output "table_name" { value = aws_dynamodb_table.tower.name }
output "table_arn" { value = aws_dynamodb_table.tower.arn }
output "release_bucket" { value = aws_s3_bucket.releases.id }
output "read_function_name" { value = aws_lambda_function.tower_read.function_name }
output "read_url_domain" {
  value = trimsuffix(trimprefix(aws_lambda_function_url.tower_read.function_url, "https://"), "/")
}
