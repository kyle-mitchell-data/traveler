SHELL := bash
.SHELLFLAGS := -euo pipefail -c

export AWS_PROFILE ?= traveler
HOME_REGION ?= us-east-2
TF := terraform -chdir=infra/terraform
PY := $(if $(wildcard .venv/Scripts/python.exe),.venv/Scripts/python,.venv/bin/python)
OPS := $(PY) -m scripts.ops

.PHONY: help bootstrap venv hooks init plan apply fmt check test lint site release wake sleep status set unset build destroy

help:
	@echo "bootstrap  create the Terraform state bucket and install git hooks"
	@echo "venv       create .venv with the local tooling"
	@echo "init       terraform init against the state bucket"
	@echo "plan       terraform plan"
	@echo "apply      terraform apply"
	@echo "check      lint, type check, tests (python + edge), terraform fmt/validate"
	@echo "site       upload the static site"
	@echo "release    build the traveler package and publish it as desired_release"
	@echo "wake       place the traveler (REGION=eu-west-1 to choose) and route to it"
	@echo "sleep      route to resting, wait for the edge, remove every copy (NOW=1 skips the wait)"
	@echo "status     tower state and live copy count"
	@echo "set/unset  test switches: make set NAME=stay_seconds VALUE=60 (stay_seconds hop_regions max_hops kill)"
	@echo "destroy    sleep, then terraform destroy (state bucket is kept)"

bootstrap: hooks
	HOME_REGION=$(HOME_REGION) bash scripts/bootstrap-state.sh

venv:
	python -m venv .venv
	$(PY) -m pip install -q -r requirements-dev.txt

hooks:
	git config core.hooksPath .githooks

init:
	$(TF) init -input=false \
	  -backend-config="bucket=traveler-tfstate-$$(aws sts get-caller-identity --query Account --output text)"

plan: build
	$(TF) plan -input=false

apply: build
	$(TF) apply -input=false

fmt:
	$(TF) fmt -recursive
	$(PY) -m ruff format .

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .
	$(PY) -m mypy
	$(PY) -m mypy route_writer
	$(TF) fmt -recursive -check
	$(TF) validate

test:
	$(PY) -m pytest -q
	node --test edge/router.test.mjs

check: lint test

site:
	aws s3 sync site/ "s3://$$($(TF) output -raw site_bucket)/" --delete --cache-control "public, max-age=60"

release:
	$(OPS) release

wake:
	$(OPS) wake $(if $(REGION),--region $(REGION),)

sleep:
	$(OPS) sleep $(if $(NOW),--now,)

status:
	$(OPS) status

set:
	$(OPS) set $(NAME) $(VALUE)

unset:
	$(OPS) unset $(NAME)

build:
	bash scripts/build-route-writer.sh

destroy: sleep
	$(TF) destroy -input=false
