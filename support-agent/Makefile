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

eval:               ## end-to-end pass@1/pass^k + per-component scorecard; fails on unsafe sends
	.venv/bin/support-agent eval --trials 3 --components

eval-hybrid:        ## same scorecard with hybrid retrieval on embedded Weaviate (set VOYAGE_API_KEY + SA_EMBEDDER=voyage for real embeddings)
	SA_RETRIEVER=hybrid .venv/bin/support-agent eval --trials 1 --components

trace:              ## Jaeger UI at http://localhost:16686 ; then run with SA_OTEL_EXPORTER=otlp
	docker run --rm -d --name jaeger -e COLLECTOR_OTLP_ENABLED=true -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one:1.62.0

serve:              ## approval UI at http://127.0.0.1:8000 (basic auth: any user / SA_ADMIN_TOKEN)
	.venv/bin/support-agent serve

worker:             ## poll data/inbox (and IMAP if configured)
	.venv/bin/support-agent worker

docker:
	docker compose up --build
