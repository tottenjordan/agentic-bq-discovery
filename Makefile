.PHONY: help install lint format test check clean image image-ref

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

image: ## Build, verify, and push the runner image to Artifact Registry
	@test -z "$$(git status --porcelain)" || \
	  (echo "Working tree is dirty; the image would be tagged with a SHA that does not describe it." && exit 1)
	gcloud builds submit --region=us-central1 --config cloudbuild.yaml \
	  --substitutions=_TAG=$$(git rev-parse --short HEAD) .

image-ref: ## Print the image reference for the current commit
	@echo us-central1-docker.pkg.dev/hybrid-vertex/bq-context/runner:$$(git rev-parse --short HEAD)


clean: ## Remove caches and build artifacts
	rm -rf .ruff_cache .pytest_cache .ty_cache .coverage htmlcov dist build
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true
