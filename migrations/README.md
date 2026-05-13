# Alembic migrations

The baseline migration (`0001_initial_schema.py`) just replays
`SQLModel.metadata.create_all` so an existing install can be brought to the
canonical version without losing data. Future revisions should be hand-written
diffs.

## Commands

```bash
# Apply migrations
alembic upgrade head

# Generate a new revision after editing models/entities.py
alembic revision --autogenerate -m "describe change"

# Roll back one step
alembic downgrade -1
```

The runtime database URL is taken from `app.config.get_settings()` so the
`sqlalchemy.url` in `alembic.ini` is just a fallback for tooling.
