.PHONY: help install lint format test check clean image image-ref require-project

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Install all dependency groups
	uv sync --all-groups

lint: ## Lint and type-check
	uv run ruff format --check .
	uv run ruff check .
	uv run ty check src/

format: ## Auto-format and apply safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

test: ## Run tests
	uv run pytest

check: lint test ## Lint, type-check, then test

# Resolved from the environment, then .env — never hard-coded. cloudbuild.yaml
# already uses $$PROJECT_ID; this is the same idea for the local half.
GOOGLE_CLOUD_PROJECT ?= $(shell sed -n 's/^GOOGLE_CLOUD_PROJECT=//p' .env 2>/dev/null | head -1)
REGION ?= us-central1
IMAGE_REPO = $(REGION)-docker.pkg.dev/$(GOOGLE_CLOUD_PROJECT)/bq-context/runner

require-project:
	@test -n "$(GOOGLE_CLOUD_PROJECT)" || \
	  (echo "GOOGLE_CLOUD_PROJECT is unset. Export it or set it in .env." && exit 1)

image: require-project ## Build, verify, and push the runner image to Artifact Registry
	@test -z "$$(git status --porcelain)" || \
	  (echo "Working tree is dirty; the image would be tagged with a SHA that does not describe it." && exit 1)
	gcloud builds submit --region=$(REGION) --config cloudbuild.yaml \
	  --substitutions=_TAG=$$(git rev-parse --short HEAD) .

image-ref: require-project ## Print the image reference for the current commit
	@echo $(IMAGE_REPO):$$(git rev-parse --short HEAD)


clean: ## Remove caches and build artifacts
	rm -rf .ruff_cache .pytest_cache .ty_cache .coverage htmlcov dist build
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true
