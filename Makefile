.PHONY: lint test check dev demo up down
lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src/ scripts/
test:
	uv run pytest
check: lint test
dev:
	uv run uvicorn culinary_copilot.api.app:create_app --factory --reload
# Live demo: paid model calls. Budgets apply to sessions created after
# start; override any value, e.g. make demo DEMO_TOOL_CALLS=1000000.
DEMO_STEPS ?= 40
DEMO_TOOL_CALLS ?= 40
DEMO_INPUT_TOKENS ?= 300000
DEMO_OUTPUT_TOKENS ?= 60000
DEMO_WALL_CLOCK_S ?= 240
demo:
	HF_HUB_OFFLINE=1 LLM_RECOMMENDATION_ENABLED=true EPICURE_ENABLED=true EMBEDDINGS_ENABLED=true \
	SESSION_MAX_STEPS=$(DEMO_STEPS) SESSION_MAX_TOOL_CALLS=$(DEMO_TOOL_CALLS) \
	AGENT_INPUT_TOKEN_CEILING=$(DEMO_INPUT_TOKENS) AGENT_OUTPUT_TOKEN_CEILING=$(DEMO_OUTPUT_TOKENS) \
	AGENT_WALL_CLOCK_S=$(DEMO_WALL_CLOCK_S) AGENT_RECORD_TRAJECTORY=true WEB_SEARCH_ENABLED=true \
	uv run uvicorn culinary_copilot.api.app:create_app --factory
up:
	docker compose up --build -d --wait
down:
	docker compose down
