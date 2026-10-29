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

output "table_name" { value = aws_dynamodb_table.tower.name }
output "table_arn" { value = aws_dynamodb_table.tower.arn }
