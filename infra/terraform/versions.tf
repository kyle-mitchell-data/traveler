terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Bucket is passed at init time by `make init` (it embeds the account ID).
  backend "s3" {
    key          = "traveler/terraform.tfstate"
    region       = "us-east-2"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.home_region

  default_tags {
    tags = {
      project    = "traveler"
      managed_by = "terraform"
    }
  }
}
