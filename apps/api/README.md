# guardrail-api

FastAPI + async SQLAlchemy + Alembic。依赖用 `uv` 管理。

```bash
uv sync                                  # 装依赖
uv run uvicorn guardrail_api.main:app --reload
uv run pytest -m "not integration"
uv run ruff check . && uv run ruff format --check .
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "add orders"
```

