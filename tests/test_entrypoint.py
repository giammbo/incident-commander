import os
import subprocess
from pathlib import Path

ENTRY = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh"


def _run(tmp_path, args, env_extra=None):
    """Run entrypoint.sh with stub `alembic`/`uvicorn` on PATH that print markers + DATABASE_URL."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("alembic", "uvicorn"):
        p = bindir / name
        p.write_text(f'#!/usr/bin/env sh\necho "RAN {name} args=$* db=$DATABASE_URL"\n')
        p.chmod(0o755)
    env = {"PATH": f"{bindir}:{os.environ['PATH']}"}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["sh", str(ENTRY), *args], capture_output=True, text=True, env=env)


def test_migrate_only(tmp_path):
    out = _run(tmp_path, ["migrate"]).stdout
    assert "RAN alembic args=upgrade head" in out
    assert "RAN uvicorn" not in out


def test_serve_only(tmp_path):
    out = _run(tmp_path, ["serve"]).stdout
    assert "RAN uvicorn" in out
    assert "RAN alembic" not in out


def test_default_runs_both(tmp_path):
    out = _run(tmp_path, []).stdout
    assert "RAN alembic args=upgrade head" in out
    assert "RAN uvicorn" in out


def test_assembles_database_url_from_parts(tmp_path):
    out = _run(
        tmp_path,
        ["serve"],
        {
            "DB_HOST": "pg",
            "DB_USER": "ic",
            "DB_PASSWORD": "secret",
            "DB_NAME": "icdb",
        },
    ).stdout
    assert "db=postgresql+psycopg://ic:secret@pg:5432/icdb" in out


def test_preserves_explicit_database_url(tmp_path):
    out = _run(
        tmp_path,
        ["serve"],
        {
            "DATABASE_URL": "postgresql+psycopg://x:y@host:5432/d",
            "DB_HOST": "ignored",
            "DB_USER": "ignored",
            "DB_PASSWORD": "z",
            "DB_NAME": "ignored",
        },
    ).stdout
    assert "db=postgresql+psycopg://x:y@host:5432/d" in out


def test_await_db_exits_when_at_head(tmp_path):
    # stub alembic prints a head revision -> the await loop's grep matches -> exits promptly
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "alembic").write_text('#!/usr/bin/env sh\necho "0017_meet (head)"\n')
    (bindir / "alembic").chmod(0o755)
    (bindir / "uvicorn").write_text("#!/usr/bin/env sh\necho ran uvicorn\n")
    (bindir / "uvicorn").chmod(0o755)
    r = subprocess.run(
        ["sh", str(ENTRY), "await-db"],
        capture_output=True,
        text=True,
        env={"PATH": f"{bindir}:{os.environ['PATH']}"},
        timeout=10,
    )
    assert r.returncode == 0
