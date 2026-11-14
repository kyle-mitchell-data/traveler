variable "account_id" { type = string }
variable "home_region" { type = string }
variable "allowlist" { type = list(string) }
variable "github_repo" { type = string }
variable "github_subject" { type = string }
variable "state_bucket" { type = string }
variable "tower_table" { type = string }
variable "kvs_arn" {
  type    = string
  default = null
}
variable "release_bucket" {
  type    = string
  default = null
}
variable "writer_arn" {
  type    = string
  default = null
}

locals {
  traveler_fn = "arn:aws:lambda:*:${var.account_id}:function:traveler"
}

# ---------------------------------------------------------------------------
# Permissions boundary: the most the traveler can ever do, whatever its policy
# says. No IAM writes, so it can never edit its own policy or create roles.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "boundary" {
  statement {
    sid = "Ceiling"
    actions = [
      "lambda:*",
      "dynamodb:*",
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
      "ssm:DeleteParameter",
      "kms:Decrypt",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "cloudfront-keyvaluestore:*",
      "s3:GetObject",
      "iam:PassRole",
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "NoIamWrites"
    effect = "Deny"
    actions = [
      "iam:Add*", "iam:Attach*", "iam:Create*", "iam:Delete*", "iam:Detach*",
      "iam:Put*", "iam:Remove*", "iam:Set*", "iam:Tag*", "iam:Untag*",
      "iam:Update*", "iam:Upload*",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "boundary" {
  name   = "traveler-boundary"
  policy = data.aws_iam_policy_document.boundary.json
}

# ---------------------------------------------------------------------------
# Traveler execution role: one global role, used by every copy in every region.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "traveler" {
  name                 = "traveler"
  assume_role_policy   = data.aws_iam_policy_document.lambda_trust.json
  permissions_boundary = aws_iam_policy.boundary.arn
}

data "aws_iam_policy_document" "traveler" {
  # Create, check, invoke and retire copies of itself - only in the allowlist.
  statement {
    sid = "SelfReplicate"
    actions = [
      "lambda:CreateFunction",
      "lambda:GetFunction",
      "lambda:GetFunctionConfiguration",
      "lambda:DeleteFunction",
      "lambda:CreateFunctionUrlConfig",
      "lambda:GetFunctionUrlConfig",
      "lambda:AddPermission",
      "lambda:InvokeFunction",
      "lambda:PutFunctionEventInvokeConfig",
      "lambda:PutFunctionConcurrency",
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
    sid       = "PassOwnRoleToLambda"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.traveler.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["lambda.amazonaws.com"]
    }
  }

  statement {
    sid = "Tower"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
      "dynamodb:BatchWriteItem",
      "dynamodb:TransactWriteItems",
    ]
    resources = [var.tower_table]
  }

  statement {
    sid = "Config"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
    ]
    resources = ["arn:aws:ssm:${var.home_region}:${var.account_id}:parameter/traveler/*"]
  }

  # Consume the one-shot failure-injection switch (phase 3 gate tests).
  statement {
    sid       = "ConsumeInjection"
    actions   = ["ssm:DeleteParameter"]
    resources = ["arn:aws:ssm:${var.home_region}:${var.account_id}:parameter/traveler/inject"]
  }

  statement {
    sid       = "SecureStringDecrypt"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.home_region}.amazonaws.com"]
    }
  }

  statement {
    sid       = "OwnLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:*:${var.account_id}:log-group:/aws/lambda/traveler:*"]
  }

  dynamic "statement" {
    for_each = var.kvs_arn == null ? [] : [var.kvs_arn]
    content {
      sid = "RouteFlip"
      actions = [
        "cloudfront-keyvaluestore:DescribeKeyValueStore",
        "cloudfront-keyvaluestore:GetKey",
        "cloudfront-keyvaluestore:PutKey",
        "cloudfront-keyvaluestore:UpdateKeys",
      ]
      resources = [statement.value]
    }
  }

  # Route flips go through the home-region route writer (decision D16).
  dynamic "statement" {
    for_each = var.writer_arn == null ? [] : [var.writer_arn]
    content {
      sid       = "FlipRoute"
      actions   = ["lambda:InvokeFunction"]
      resources = [statement.value]
    }
  }

  dynamic "statement" {
    for_each = var.release_bucket == null ? [] : [var.release_bucket]
    content {
      sid       = "ReadReleases"
      actions   = ["s3:GetObject"]
      resources = ["arn:aws:s3:::${statement.value}/*"]
    }
  }
}

resource "aws_iam_role_policy" "traveler" {
  name   = "traveler"
  role   = aws_iam_role.traveler.id
  policy = data.aws_iam_policy_document.traveler.json
}

# ---------------------------------------------------------------------------
# GitHub OIDC: CI gets short-lived credentials, never access keys.
# ---------------------------------------------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

# The repo issues immutable subjects (owner and repo pinned by numeric ID, so a renamed
# or re-created repo cannot inherit trust); the name form is kept for the legacy format.
data "aws_iam_policy_document" "ci_trust" {
  for_each = {
    plan = [for p in ["repo:${var.github_repo}", var.github_subject] : "${p}:pull_request"]
    # Only jobs in the "production" environment, which is limited to main: any other
    # workflow on main (a bot, a future pull_request_target) gets no deploy role.
    deploy = [for p in ["repo:${var.github_repo}", var.github_subject] : "${p}:environment:production"]
  }

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = each.value
    }
  }
}

resource "aws_iam_role" "ci" {
  for_each = data.aws_iam_policy_document.ci_trust

  name                 = "traveler-ci-${each.key}"
  assume_role_policy   = each.value.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "ci_plan_readonly" {
  role       = aws_iam_role.ci["plan"].name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# Plan needs to write the state lock file; nothing else in the bucket.
data "aws_iam_policy_document" "ci_plan_lock" {
  statement {
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${var.state_bucket}/traveler/terraform.tfstate.tflock"]
  }
}

resource "aws_iam_role_policy" "ci_plan_lock" {
  name   = "state-lock"
  role   = aws_iam_role.ci["plan"].id
  policy = data.aws_iam_policy_document.ci_plan_lock.json
}

resource "aws_iam_role_policy_attachment" "ci_deploy_admin" {
  role       = aws_iam_role.ci["deploy"].name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# Decision D12: CI can never create long-lived credentials or change account-level settings.
data "aws_iam_policy_document" "ci_deploy_guard" {
  statement {
    effect = "Deny"
    actions = [
      "iam:CreateUser",
      "iam:CreateAccessKey",
      "iam:CreateLoginProfile",
      "account:*",
      "organizations:*",
    ]
    resources = ["*"]
  }

  # Decision D32: CI cannot switch off the account baseline. Removing any of it
  # takes a person signed in through SSO.
  statement {
    sid    = "KeepBaseline"
    effect = "Deny"
    actions = [
      "cloudtrail:StopLogging",
      "cloudtrail:DeleteTrail",
      "access-analyzer:DeleteAnalyzer",
      "s3:DeleteAccountPublicAccessBlock",
      "ec2:DisableEbsEncryptionByDefault",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "ci_deploy_guard" {
  name   = "guard"
  role   = aws_iam_role.ci["deploy"].id
  policy = data.aws_iam_policy_document.ci_deploy_guard.json
}

output "traveler_role_arn" { value = aws_iam_role.traveler.arn }
output "boundary_arn" { value = aws_iam_policy.boundary.arn }
output "ci_plan_role_arn" { value = aws_iam_role.ci["plan"].arn }
output "ci_deploy_role_arn" { value = aws_iam_role.ci["deploy"].arn }
