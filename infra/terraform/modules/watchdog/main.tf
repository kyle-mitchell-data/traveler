variable "account_id" { type = string }
variable "home_region" { type = string }
variable "allowlist" { type = list(string) }
variable "source_dir" { type = string }
variable "tower_table_arn" { type = string }
variable "release_bucket" { type = string }
variable "traveler_role_arn" { type = string }
variable "writer_arn" { type = string }
variable "boundary_arn" { type = string }
variable "alert_email" { type = string }
variable "log_retention_days" { type = number }

locals {
  traveler_fn = "arn:aws:lambda:*:${var.account_id}:function:traveler"
}

# ---------------------------------------------------------------------------
# Watchdog function, every 5 minutes.
# ---------------------------------------------------------------------------

data "archive_file" "watchdog" {
  type        = "zip"
  source_dir  = var.source_dir
  output_path = "${path.root}/.build/watchdog.zip"
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

resource "aws_iam_role" "watchdog" {
  name                 = "traveler-watchdog"
  assume_role_policy   = data.aws_iam_policy_document.lambda_trust.json
  permissions_boundary = var.boundary_arn
}

data "aws_iam_policy_document" "watchdog" {
  # Count, delete and (on resurrection) create the traveler, only in the allowlist.
  statement {
    actions = [
      "lambda:GetFunctionConfiguration",
      "lambda:GetFunction",
      "lambda:DeleteFunction",
      "lambda:CreateFunction",
      "lambda:CreateFunctionUrlConfig",
      "lambda:GetFunctionUrlConfig",
      "lambda:AddPermission",
      "lambda:InvokeFunction",
      "lambda:PutFunctionEventInvokeConfig",
      "lambda:PutFunctionRecursionConfig",
      "lambda:TagResource",
    ]
    resources = [local.traveler_fn]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = var.allowlist
    }
  }

  statement {
    actions   = ["iam:PassRole"]
    resources = [var.traveler_role_arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["lambda.amazonaws.com"]
    }
  }

  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [var.writer_arn]
  }

  statement {
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
    ]
    resources = [var.tower_table_arn]
  }

  statement {
    actions   = ["ssm:GetParametersByPath", "ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.home_region}:${var.account_id}:parameter/traveler*"]
  }

  statement {
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${var.release_bucket}/*"]
  }

  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.watchdog.arn}:*"]
  }
}

resource "aws_iam_role_policy" "watchdog" {
  name   = "watchdog"
  role   = aws_iam_role.watchdog.id
  policy = data.aws_iam_policy_document.watchdog.json
}

resource "aws_cloudwatch_log_group" "watchdog" {
  name              = "/aws/lambda/traveler-watchdog"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "watchdog" {
  function_name    = "traveler-watchdog"
  role             = aws_iam_role.watchdog.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.watchdog.output_path
  source_code_hash = data.archive_file.watchdog.output_base64sha256
  memory_size      = 256
  timeout          = 240 # a resurrection waits 90 s for the edge before retiring old copies

  environment {
    variables = { TOWER_REGION = var.home_region }
  }

  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.watchdog.name
  }
}

data "aws_iam_policy_document" "scheduler_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "traveler-watchdog-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_trust.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.watchdog.arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "invoke-watchdog"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

resource "aws_scheduler_schedule" "watchdog" {
  name                = "traveler-watchdog"
  schedule_expression = "rate(5 minutes)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.watchdog.arn
    role_arn = aws_iam_role.scheduler.arn
    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}

# ---------------------------------------------------------------------------
# Alerts: one topic, three alarms on the watchdog's metrics (D18: few, undimensioned).
# ---------------------------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name = "traveler-alerts"
}

# The default owner statement plus AWS Budgets, whose own email never arrived (D31):
# budget alerts reach the inbox through this topic instead.
data "aws_iam_policy_document" "alerts" {
  statement {
    sid = "Owner"
    actions = [
      "SNS:GetTopicAttributes", "SNS:SetTopicAttributes", "SNS:AddPermission",
      "SNS:RemovePermission", "SNS:DeleteTopic", "SNS:Subscribe",
      "SNS:ListSubscriptionsByTopic", "SNS:Publish",
    ]
    resources = [aws_sns_topic.alerts.arn]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceOwner"
      values   = [var.account_id]
    }
  }

  statement {
    sid       = "Budgets"
    actions   = ["SNS:Publish"]
    resources = [aws_sns_topic.alerts.arn]
    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts.json
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

locals {
  alarms = {
    no-heartbeat = {
      metric    = "HeartbeatAgeSeconds"
      op        = "GreaterThanThreshold"
      threshold = 600
      missing   = "breaching" # no watchdog data at all is also bad
      text      = "The traveler has not reported for 10 minutes."
    }
    aborts = {
      metric    = "ConsecutiveAborts"
      op        = "GreaterThanOrEqualToThreshold"
      threshold = 3
      missing   = "notBreaching"
      text      = "Three or more hops in a row were aborted."
    }
    invariant = {
      metric    = "InvariantViolations"
      op        = "GreaterThanThreshold"
      threshold = 0
      missing   = "notBreaching"
      text      = "An invariant was violated: extra copies, a deleted traveler, or an orphaned copy."
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "traveler" {
  for_each = local.alarms

  alarm_name          = "traveler-${each.key}"
  alarm_description   = each.value.text
  namespace           = "Traveler"
  metric_name         = each.value.metric
  statistic           = "Maximum"
  comparison_operator = each.value.op
  threshold           = each.value.threshold
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = each.value.missing
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

output "alerts_topic_arn" { value = aws_sns_topic.alerts.arn }
