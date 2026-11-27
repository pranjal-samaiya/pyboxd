.PHONY: install dev test test-unit lint format typecheck run docker-build docker-up load-test clean

PY ?= python3

install:            ## install the package
	$(PY) -m pip install .

dev:                ## install with dev tools (editable)
	$(PY) -m pip install -e ".[dev]"

test:               ## full test suite (e2e uses nsjail if installed, else unsandboxed mode)
	$(PY) -m pytest

test-unit:          ## tests that need no compilers
	$(PY) -m pytest -m "not e2e"

lint:               ## ruff lint + format check
	ruff check src tests scripts
	ruff format --check src tests scripts

format:
	ruff format src tests scripts
	ruff check --fix src tests scripts

typecheck:
	mypy src

run:                ## run locally WITHOUT isolation (dev only; needs PYBOXD_AUTH_TOKEN)
	PYBOXD_ALLOW_UNSANDBOXED=1 pyboxd --config languages.yaml --port 8080

docker-build:
	docker build -t pyboxd:latest --build-arg COMMIT=$$(git rev-parse --short HEAD) .

docker-up:          ## build and start with docker compose (reads .env)
	docker compose up --build -d

load-test:          ## needs a running server and PYBOXD_AUTH_TOKEN
	$(PY) scripts/load_test.py --url http://localhost:8080

clean:
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
