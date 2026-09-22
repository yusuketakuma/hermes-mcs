.PHONY: test lint readme check setup-check gates

# uv is preferred when present (deps fetched ephemerally); otherwise the
# commands are assumed to be installed on PATH / in the active venv.
UV := $(shell command -v uv 2>/dev/null)
ifdef UV
PYTEST = uv run --with pytest python -m pytest
RUFF   = uv run --with ruff ruff
else
PYTEST = python3 -m pytest
RUFF   = ruff
endif

test:           ## run the full test suite
	$(PYTEST)

lint:           ## ruff lint (same rule set as CI, via pyproject.toml)
	$(RUFF) check mcs/ tests/

readme:         ## regenerate the auto-generated README module table
	python3 scripts/update_readme.py

check:          ## lint + readme drift check (PR-gate equivalent)
	$(RUFF) check mcs/ tests/ && python3 scripts/update_readme.py --check

gates:          ## incident-derived static gates + dev-record coverage
	python3 ci/gates.py && python3 ci/mine_gates.py --check

setup-check:    ## verify the live machine's required conditions
	python3 mcs/mcs_setup.py check
