.PHONY: setup run check types

setup:
	uv sync
	cd frontend && npm ci
	uv run debate-bro types
	cd frontend && npm run build

run:
	uv run debate-bro serve

types:
	uv run debate-bro types

check:
	uv run ruff check debate_bro tests
	uv run pytest -q
	cd frontend && npm run build
	cd frontend && npm run test:e2e
