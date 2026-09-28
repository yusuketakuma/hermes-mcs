.PHONY: test lint readme check setup-check gates

# uv is preferred when present (deps fetched ephemerally); otherwise the
# commands are assumed to be installed on PATH / in the active venv.
UV := $(shell command -v uv 2>/dev/null)
ifdef UV
PYTEST = uv run --with "pytest==9.1.1" scripts/run_tests.sh
RUFF   = uv run --with "ruff==0.16.8" ruff
else
PYTEST = scripts/run_tests.sh
RUFF   = ruff
endif

LINT_PATHS = mcs/ tests/ hermes_plugin/ integration/ ci/ scripts/ deployment/ conftest.py

test:           ## run the full test suite
	$(PYTEST)

lint:           ## ruff lint (same rule set as CI, via pyproject.toml)
	$(RUFF) check $(LINT_PATHS)

readme:         ## regenerate the auto-generated doc blocks
	python3 scripts/update_readme.py

check:          ## lint + readme drift check (PR-gate equivalent)
	$(RUFF) check $(LINT_PATHS) && python3 scripts/update_readme.py --check

gates:          ## incident-derived static gates + dev-record coverage
	python3 ci/gates.py && python3 ci/mine_gates.py --check

setup-check:    ## verify the live machine's required conditions
	python3 mcs/ops/mcs_setup.py check
