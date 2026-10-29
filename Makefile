SHELL := bash
.SHELLFLAGS := -euo pipefail -c

export AWS_PROFILE ?= traveler-bootstrap
HOME_REGION ?= us-east-2
TF := terraform -chdir=infra/terraform

.PHONY: help bootstrap init plan apply fmt validate hooks wake sleep status destroy

help:
	@echo "bootstrap  create the Terraform state bucket and install git hooks"
	@echo "init       terraform init against the state bucket"
	@echo "plan       terraform plan"
	@echo "apply      terraform apply"
	@echo "fmt        terraform fmt + validate"
	@echo "wake / sleep / status / destroy   (phase 2)"

bootstrap: hooks
	HOME_REGION=$(HOME_REGION) bash scripts/bootstrap-state.sh

hooks:
	git config core.hooksPath .githooks

init:
	$(TF) init -input=false \
	  -backend-config="bucket=traveler-tfstate-$$(aws sts get-caller-identity --query Account --output text)"

plan:
	$(TF) plan -input=false

apply:
	$(TF) apply -input=false

fmt:
	$(TF) fmt -recursive
	$(TF) validate

wake sleep status destroy:
	@echo "$@: arrives in phase 2"; exit 1
