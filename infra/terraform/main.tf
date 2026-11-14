data "aws_caller_identity" "current" {}

locals {
  account_id   = data.aws_caller_identity.current.account_id
  state_bucket = "traveler-tfstate-${local.account_id}"
  repo_root    = "${path.root}/../.."
}

module "tower" {
  source             = "./modules/tower"
  home_region        = var.home_region
  allowlist          = var.allowlist
  tower_source       = "${local.repo_root}/tower/handler.py"
  log_retention_days = var.log_retention_days
}

module "regions" {
  source             = "./modules/regions"
  allowlist          = var.allowlist
  log_retention_days = var.log_retention_days
}

module "edge" {
  source             = "./modules/edge"
  tower_url_domain   = module.tower.read_url_domain
  router_source      = "${local.repo_root}/edge/traveler-router.js"
  writer_dir         = "${path.root}/.build/route-writer"
  log_retention_days = var.log_retention_days
  tower_table_name   = module.tower.table_name
  tower_table_arn    = module.tower.table_arn
}

module "iam" {
  source         = "./modules/iam"
  account_id     = local.account_id
  home_region    = var.home_region
  allowlist      = var.allowlist
  github_repo    = var.github_repo
  github_subject = var.github_oidc_subject_prefix
  state_bucket   = local.state_bucket
  tower_table    = module.tower.table_arn
  kvs_arn        = module.edge.kvs_arn
  release_bucket = module.tower.release_bucket
  writer_arn     = module.edge.writer_arn
}

module "watchdog" {
  source             = "./modules/watchdog"
  account_id         = local.account_id
  home_region        = var.home_region
  allowlist          = var.allowlist
  source_dir         = "${path.root}/.build/watchdog"
  tower_table_arn    = module.tower.table_arn
  release_bucket     = module.tower.release_bucket
  traveler_role_arn  = module.iam.traveler_role_arn
  writer_arn         = module.edge.writer_arn
  boundary_arn       = module.iam.boundary_arn
  alert_email        = var.alert_email
  log_retention_days = var.log_retention_days
}

module "budgets" {
  source             = "./modules/budgets"
  alert_email        = var.alert_email
  alerts_topic_arn   = module.watchdog.alerts_topic_arn
  monthly_budget_usd = var.monthly_budget_usd
  alert_test         = var.alert_test
}

module "security" {
  source      = "./modules/security"
  account_id  = local.account_id
  home_region = var.home_region
  allowlist   = var.allowlist
}

# CloudFront may call the tower read function, and nothing else may (D17).
resource "aws_lambda_permission" "tower_from_cloudfront" {
  for_each = {
    url    = "lambda:InvokeFunctionUrl"
    invoke = "lambda:InvokeFunction"
  }

  statement_id           = "cloudfront-${each.key}"
  function_name          = module.tower.read_function_name
  action                 = each.value
  principal              = "cloudfront.amazonaws.com"
  source_arn             = module.edge.distribution_arn
  function_url_auth_type = each.key == "url" ? "AWS_IAM" : null
}

# Settings that `make wake/sleep/status` and the traveler read at runtime.
resource "aws_ssm_parameter" "setting" {
  for_each = {
    kvs_arn          = module.edge.kvs_arn
    distribution_arn = module.edge.distribution_arn
    role_arn         = module.iam.traveler_role_arn
    release_bucket   = module.tower.release_bucket
    tower_table      = module.tower.table_name
    site_bucket      = module.edge.site_bucket
    site_url         = "https://${module.edge.domain}"
    route_writer     = module.edge.writer_name
  }

  name  = "/traveler/${each.key}"
  type  = "String"
  value = each.value
}
