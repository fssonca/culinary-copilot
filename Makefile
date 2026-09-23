.PHONY: lint test check dev up down
lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src/ scripts/
test:
	uv run pytest
check: lint test
dev:
	uv run uvicorn culinary_copilot.api.app:create_app --factory --reload
up:
	docker compose up --build -d --wait
down:
	docker compose down
