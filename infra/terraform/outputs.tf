output "tower_table" {
  value = module.tower.table_name
}

output "traveler_role_arn" {
  value = module.iam.traveler_role_arn
}

output "ci_plan_role_arn" {
  value = module.iam.ci_plan_role_arn
}

output "ci_deploy_role_arn" {
  value = module.iam.ci_deploy_role_arn
}

output "site_url" {
  value = "https://${module.edge.domain}"
}

output "site_bucket" {
  value = module.edge.site_bucket
}
