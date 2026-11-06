variable "home_region" {
  description = "Fixed region for the tower, watchdog, release store and state."
  type        = string
  default     = "us-east-2"
}

variable "allowlist" {
  description = "Regions the traveler may visit. Only regions already enabled on the account."
  type        = list(string)
  default = [
    "ap-northeast-1", "ap-northeast-2", "ap-northeast-3", "ap-south-1",
    "ap-southeast-1", "ap-southeast-2", "ca-central-1", "eu-central-1",
    "eu-north-1", "eu-west-1", "eu-west-2", "eu-west-3", "sa-east-1",
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
  ]
}

variable "github_repo" {
  description = "owner/name of the repo allowed to assume the CI roles."
  type        = string
  default     = "kyle-mitchell-data/traveler"
}

variable "github_oidc_subject_prefix" {
  description = "The repo's immutable OIDC subject prefix (owner@id/name@id), from GET /repos/{repo}/actions/oidc/customization/sub."
  type        = string
  default     = "repo:kyle-mitchell-data@349532211/traveler@1437877847"
}

variable "alert_email" {
  description = "Where budget alerts go. Set in terraform.tfvars, which is not committed."
  type        = string
}

variable "monthly_budget_usd" {
  type    = number
  default = 25
}

variable "alert_test" {
  description = "Adds a throwaway $0.01 budget so the email path can be proven. Turn off once an alert arrives."
  type        = bool
  default     = false
}

variable "log_retention_days" {
  type    = number
  default = 3
}
