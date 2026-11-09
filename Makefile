.PHONY: setup run check types

setup:
	uv sync
	cd frontend && npm ci
	uv run change-my-mind types
	cd frontend && npm run build

run:
	uv run change-my-mind serve

types:
	uv run change-my-mind types

check:
	uv run ruff check change_my_mind tests
	uv run pytest -q
	cd frontend && npm run build
	cd frontend && npm run test:e2e
