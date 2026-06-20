# Contributing to Incident Commander

Thanks for your interest in contributing! This guide gets you from a clone to a green pull request.

By participating you agree to our [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

You need **Python 3.12** (pinned in `.python-version`), [**uv**](https://docs.astral.sh/uv/),
and **Docker** (the test suite spins up a real Postgres via testcontainers).

```bash
git clone https://github.com/giammbo/incident-commander.git
cd incident-commander
uv sync                  # create the venv and install deps (incl. dev tools)
cp .env.example .env      # then fill SESSION_SECRET and FERNET_KEYS (see README)
```

Run the app locally with the full stack:

```bash
docker compose up --build      # app on http://localhost:8000
```

## Tests, lint, format

All three must pass before a PR is merged (CI enforces them):

```bash
uv run pytest            # Docker must be running (testcontainers Postgres)
uv run ruff check .      # lint
uv run ruff format .     # auto-format (CI runs `ruff format --check .`)
```

Write tests for any behavior you add or change. Prefer tests that exercise real behavior against
the test database over mocks; the suite uses a clean-per-test Postgres fixture.

## Project layout

```
app/
  main.py            app factory, middleware, startup bootstrap
  config.py          env settings (pydantic-settings)
  crypto.py          Fernet-encrypted column type
  db.py              engine / session / Base
  models.py          SQLAlchemy models + Role/Severity enums
  auth.py            sessions, require_user / require_role
  settings_store.py  runtime (DB) settings accessors
  routers/           HTTP routes (auth, account, incidents, users, groups, settings)
  services/          business logic (users, incidents)
  templates/         Jinja2 + HTMX (partials/ for HTMX fragments)
  static/            CSS + htmx
migrations/          Alembic (env.py + versions/)
tests/               pytest suite (conftest.py provides the Postgres fixtures)
```

## Database changes

Schema changes go through Alembic. After editing `app/models.py`, generate a migration:

```bash
# point DATABASE_URL at a throwaway/dev Postgres, then:
uv run alembic revision --autogenerate -m "describe your change"
uv run alembic upgrade head    # verify it applies; also check `alembic downgrade base`
```

Review the generated migration by hand — autogenerate can mis-handle Postgres enum types
(see `migrations/versions/0001_initial_initial.py` for the shared-enum pattern).

## Commits and pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org/) for messages (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`).
- Branch off `main`, keep PRs focused, and fill in the PR template.
- Make sure `pytest` and `ruff` are green locally; CI runs them on every PR.
- For UI changes, include a screenshot.

## Roadmap & scope

The project follows a phased roadmap (see the README): Phase 2 SMTP/invites, Phase 3 Slack,
Phase 4 Google. New integrations should land **behind the existing optional-connection model** —
an incident must always work as a plain record, with channel/Meet creation added only when a
connection is configured. If you're planning something large, open an issue to discuss first.

## Reporting bugs & security issues

Use the issue templates for bugs and feature requests. **Do not** file security vulnerabilities as
public issues — see [SECURITY.md](SECURITY.md).
