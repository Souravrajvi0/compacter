# Contributing

## Setup

```bash
pip install -e ".[dev,anthropic]"
pytest
```

## Layout

- `src/compacter/core/` — agent-agnostic engine: models, store, compaction, tokens
- `src/compacter/adapters/` — one module per agent
- `src/compacter/llm/` — LLM providers
- `src/compacter/cli.py` — argparse front end
- `src/compacter/wrapper.py`, `src/compacter/evals.py`
- `tests/` — pytest; integration fixtures live in `tests/data/`
- `evals/`, `examples/` — runnable methodology + demo

## Adding an adapter

1. Subclass `compacter.adapters.base.Adapter`.
2. Implement `read(project) -> Session` and `write(session, project) -> Path`.
   `write` must not overwrite the agent's own session file.
3. Decorate with `@register` and import the module in `compacter/adapters/__init__.py`.
4. Record tool pairing: put matching ids in `Message.meta["pair_ids"]`.
5. Add a fixture under `tests/data/` and an integration test next to the
   existing ones in `tests/test_adapters_integration.py`.

## Agent version compatibility

`tests/data/compat/<agent>/<version>.*` holds one fixture per agent release we have verified.
`pytest tests/test_compat.py` reads each one, checks tool pairs, and does a write -> read round trip.
`compacter doctor` compares the agents installed on your machine with those versions. When an agent
ships a new release: run `compacter doctor`, and if it says "newer", capture a fresh fixture and add it.

## Conventions

- No comments unless the intent is non-obvious.
- Core must never import agent specifics — adapters translate.
- Compaction is deterministic: policies/tags decide *what*, the LLM decides *how*.
- Keep `pytest` green before opening a PR.
