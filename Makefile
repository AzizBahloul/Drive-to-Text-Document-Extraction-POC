.PHONY: install dev test lint format docker

PYTHON ?= python3
VENV ?= .venv

install:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -e ".[dev]"

dev:
	$(VENV)/bin/uvicorn app.main:app --reload --port 8000

test:
	$(VENV)/bin/pytest

lint:
	$(VENV)/bin/ruff check app tests
	$(VENV)/bin/ruff format --check app tests

format:
	$(VENV)/bin/ruff format app tests
	$(VENV)/bin/ruff check --fix app tests

docker:
	docker compose up --build
