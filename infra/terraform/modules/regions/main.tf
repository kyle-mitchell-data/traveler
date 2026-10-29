variable "allowlist" { type = list(string) }
variable "log_retention_days" { type = number }

# Pre-created so the traveler never leaves an unbounded log group behind.
# Uses the provider v6 resource-level region argument (decision D11).
resource "aws_cloudwatch_log_group" "traveler" {
  for_each = toset(var.allowlist)

  region            = each.key
  name              = "/aws/lambda/traveler"
  retention_in_days = var.log_retention_days
}
