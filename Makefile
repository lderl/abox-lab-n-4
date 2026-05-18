help:
	@echo "Available targets:"
	@echo "  run    - Bootstrap the full environment (install tools, provision cluster)"
	@echo "  secret - Create openai-token secret from OPENAI_API_KEY env var"
	@echo "  down   - Destroy the cluster and all resources"
	@echo "  push   - Bump patch version, tag, and push to trigger CI"
	@echo "  tools  - Install necessary tools only"
	@echo "  tofu   - Initialize OpenTofu"
	@echo "  apply  - Apply OpenTofu configuration"

run:
	@bash scripts/setup.sh

secret:
	@test -n "$(OPENAI_API_KEY)" || (echo "Error: OPENAI_API_KEY is not set. Run: export OPENAI_API_KEY=sk-..."; exit 1)
	@for ns in agentgateway-system kagent; do \
		kubectl create secret generic openai-token \
			--from-literal=Authorization="$(OPENAI_API_KEY)" \
			--namespace $$ns \
			--dry-run=client -o yaml | kubectl apply -f -; \
	done
	@echo "openai-token secret applied in agentgateway-system and kagent"

tools:
	@curl -fsSL https://get.opentofu.org/install-opentofu.sh | sh -s -- --install-method standalone
	@curl -sS https://webi.sh/k9s | bash

tofu:
	@cd bootstrap && tofu init

apply:
	@cd bootstrap && tofu apply -auto-approve

down:
	@cd bootstrap && tofu destroy -auto-approve

push:
	@git fetch origin --tags --force
	$(eval TAG=$(shell git tag --list 'v*' | sort -V | tail -1 | sed 's/^v//' | grep . || echo "0.0.0"))
	$(eval MAJOR=$(shell echo $(TAG) | cut -d. -f1))
	$(eval MINOR=$(shell echo $(TAG) | cut -d. -f2))
	$(eval PATCH=$(shell echo $(TAG) | cut -d. -f3))
	$(eval NEW_TAG=v$(MAJOR).$(MINOR).$(shell echo $$(($(PATCH)+1))))
	@git tag $(NEW_TAG)
	@git push origin main $(NEW_TAG)
	@echo "Tagged and pushed $(NEW_TAG)"
