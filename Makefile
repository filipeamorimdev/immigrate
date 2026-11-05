.PHONY: setup samples run lint format types test check lock image

VENV ?= .venv
BIN := $(VENV)/bin

setup:
	python3 -m venv $(VENV)
	$(BIN)/pip install -e ".[dev]"

samples:
	scripts/fetch-sample-libraries.sh

run:
	DATA_DIR=./data TMPDIR=./data/tmp LIBRARY_ROOT=$(or $(FOLDER),.samples/tests) IMMIGRATE_HOST=127.0.0.1 $(BIN)/immigrate

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format:
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

types:
	$(BIN)/mypy

test:
	$(BIN)/pytest --cov --cov-report=term-missing

check: lint types test

lock:
	scripts/update-lock.sh

image:
	docker build -t immigrate:local .
