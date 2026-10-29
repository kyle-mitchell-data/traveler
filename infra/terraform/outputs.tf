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
