data "aws_caller_identity" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  state_bucket = "traveler-tfstate-${local.account_id}"
}

module "tower" {
  source      = "./modules/tower"
  home_region = var.home_region
  allowlist   = var.allowlist
}

module "regions" {
  source             = "./modules/regions"
  allowlist          = var.allowlist
  log_retention_days = var.log_retention_days
}

module "iam" {
  source         = "./modules/iam"
  account_id     = local.account_id
  home_region    = var.home_region
  allowlist      = var.allowlist
  github_repo    = var.github_repo
  state_bucket   = local.state_bucket
  tower_table    = module.tower.table_arn
  kvs_arn        = null # phase 2
  release_bucket = null # phase 2
}

module "budgets" {
  source             = "./modules/budgets"
  alert_email        = var.alert_email
  monthly_budget_usd = var.monthly_budget_usd
  alert_test         = var.alert_test
}
