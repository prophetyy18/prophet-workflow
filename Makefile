.PHONY: help install check test lint format typecheck

help:
	@printf '%s\n' 'install  Install the template and development dependencies' \
		'check    Run all local quality gates' \
		'test     Run the regression suite' \
		'lint     Run Ruff lint and format checks' \
		'format   Apply Ruff formatting' \
		'typecheck Run strict mypy'

install:
	python3 -m pip install -e '.[dev]'

check: test lint typecheck

test:
	python3 -m pytest

lint:
	python3 -m ruff check .
	python3 -m ruff format --check .

format:
	python3 -m ruff check --fix .
	python3 -m ruff format .

typecheck:
	python3 -m mypy
