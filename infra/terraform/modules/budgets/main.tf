variable "alert_email" { type = string }
variable "monthly_budget_usd" { type = number }
variable "alert_test" { type = bool }

locals {
  # Actual-spend alerts at $10, $20 and the full budget (decision D1).
  actual_thresholds = [10, 20, var.monthly_budget_usd]
}

resource "aws_budgets_budget" "monthly" {
  name         = "traveler-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = local.actual_thresholds
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "ABSOLUTE_VALUE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.alert_email]
    }
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}

# Throwaway budget that trips on the first cent of real spend, credits excluded,
# to prove the email path for the phase 1 gate.
resource "aws_budgets_budget" "alert_test" {
  count = var.alert_test ? 1 : 0

  name         = "traveler-alert-test"
  budget_type  = "COST"
  limit_amount = "0.01"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_types {
    include_credit = false
    include_refund = false
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 1
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }
}
