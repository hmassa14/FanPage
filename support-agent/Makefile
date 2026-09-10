.PHONY: install demo test lint typecheck eval serve worker docker

install:            ## create venv and install with dev extras
	uv venv .venv && uv pip install -e ".[dev]"

demo:               ## run the sample emails through the pipeline (offline by default)
	.venv/bin/support-agent demo

test:               ## unit + integration tests (offline)
	.venv/bin/pytest

lint:
	.venv/bin/ruff check src tests evals && .venv/bin/ruff format --check src tests evals

typecheck:
	.venv/bin/mypy src

eval:               ## replay the labeled dataset; fails on unsafe sends or < 90% decision accuracy
	.venv/bin/support-agent eval

serve:              ## approval UI at http://127.0.0.1:8000 (basic auth: any user / SA_ADMIN_TOKEN)
	.venv/bin/support-agent serve

worker:             ## poll data/inbox (and IMAP if configured)
	.venv/bin/support-agent worker

docker:
	docker compose up --build
