# Contributing

## Setup

```bash
pip install -e ".[dev,anthropic]"
pytest
```

## Layout

- `src/ctxpipe/core/` — agent-agnostic engine: models, store, compaction, tokens
- `src/ctxpipe/adapters/` — one module per agent
- `src/ctxpipe/llm/` — LLM providers
- `src/ctxpipe/cli.py` — argparse front end
- `src/ctxpipe/wrapper.py`, `src/ctxpipe/evals.py`
- `tests/` — pytest; integration fixtures live in `tests/data/`
- `evals/`, `examples/` — runnable methodology + demo

## Adding an adapter

1. Subclass `ctxpipe.adapters.base.Adapter`.
2. Implement `read(project) -> Session` and `write(session, project) -> Path`.
   `write` must not overwrite the agent's own session file.
3. Decorate with `@register` and import the module in `ctxpipe/adapters/__init__.py`.
4. Record tool pairing: put matching ids in `Message.meta["pair_ids"]`.
5. Add a fixture under `tests/data/` and an integration test next to the
   existing ones in `tests/test_adapters_integration.py`.

## Conventions

- No comments unless the intent is non-obvious.
- Core must never import agent specifics — adapters translate.
- Compaction is deterministic: policies/tags decide *what*, the LLM decides *how*.
- Keep `pytest` green before opening a PR.
