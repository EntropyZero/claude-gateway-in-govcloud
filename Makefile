# Test entrypoints. `make test` runs every suite; the CI workflow
# (.github/workflows/tests.yml) runs the same commands per job.
# Tooling expected on PATH: python3 + pytest, bats, cfn-lint, cfn-guard, pwsh.

.PHONY: test test-lambda test-bash test-cfn test-powershell docs-pdf

test: test-lambda test-bash test-cfn test-powershell
	@echo "All test suites passed."

test-lambda:
	python3 -m pytest tests/lambda tests/templates -q

test-bash:
	bats tests/bash

test-cfn:
	cfn-lint cloudformation/*.yaml
	@for t in cloudformation/*.yaml; do \
		echo "cfn-guard: $$t"; \
		cfn-guard validate -d "$$t" -r tests/cfn/rules.guard --show-summary fail || exit 1; \
	done

test-powershell:
	pwsh -NoProfile -File tests/run-pester.ps1

# Render the review-package docs (architecture, network-access-controls,
# om-runbooks) to PDF next to their sources. Deps: pip install weasyprint markdown.
# Re-run and commit the PDFs in the same change that edits a doc or diagram.
docs-pdf:
	python3 scripts/md-to-pdf.py
