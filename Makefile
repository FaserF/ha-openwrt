.PHONY: help install test lint format clean check

help:
	@echo "Available commands:"
	@echo "  make install    - Install required dependencies"
	@echo "  make test       - Run tests with pytest"
	@echo "  make lint       - Run ruff linter and mypy"
	@echo "  make format     - Run ruff formatter"
	@echo "  make check      - Run format, lint, and test (CI simulation)"
	@echo "  make clean      - Remove build and cache files"

install:
	pip install --upgrade pip
	pip install -r requirements_test.txt || true
	pip install .[dev] || true
	pre-commit install

test:
	pytest --cov=custom_components.openwrt --cov-report=term-missing

lint:
	ruff check .
	mypy custom_components/openwrt

format:
	ruff format .

check: format lint test

clean:
	rm -rf .pytest_cache
	rm -rf .mypy_cache
	rm -rf .ruff_cache
	find . -type d -name "__pycache__" -exec rm -rf {} +
	rm -f coverage.xml
	rm -f .coverage
