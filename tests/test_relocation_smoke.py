"""
tests/test_relocation_smoke.py
===============================
Step 1 sanity checks: geo-mcp moved from a standalone repo into
chat-platform/mcp/geo-mcp/ with no code changes. These tests exist to catch
anything that *looks* unrelated to the move but actually depends on the old
repo layout or the old build context.

Nothing here needs a live Postgres or a running MCP server -- that's the
point: these are import-time / static checks, safe to run in CI on every
commit, before any integration test that needs real infrastructure.

Run with:  pytest tests/test_relocation_smoke.py -v
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

# app/ is imported as a top-level package ("import app.xxx"), matching
# ENV PYTHONPATH=/app set in the Containerfile. Outside the container, the
# equivalent is "the geo-mcp folder itself must be on sys.path" -- exactly
# what running pytest from mcp/geo-mcp/ (with a pyproject.toml/pytest.ini
# there, or PYTHONPATH set) gives you. We do it explicitly here so this
# file works no matter where pytest is invoked from.
GEO_MCP_ROOT = Path(__file__).resolve().parent.parent
if str(GEO_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(GEO_MCP_ROOT))


# --------------------------------------------------------------------------- #
# 1. Package imports cleanly from the new location
# --------------------------------------------------------------------------- #

IMPORT_TARGETS = [
    "app.config",
    "app.db",
    "app.logging_setup",
    "app.geo.detector",
    "app.geo.resolver",
    "app.geo.builder",
    "app.geo.mapviz",
    "app.tools.geo_tools",
    "app.tools.revision",
    "app.main",
]


@pytest.mark.parametrize("module_name", IMPORT_TARGETS)
def test_module_imports_without_error(module_name):
    """
    Every module the server depends on must import cleanly with no DB
    connection, no network call, and no missing-env-var crash -- db_dsn()
    is only ever called lazily inside query()/execute(), never at import
    time, so this must succeed even with zero environment configured.
    """
    importlib.import_module(module_name)


def test_main_registers_expected_tools():
    """
    Confirms FastMCP tool registration still works post-move (i.e. the
    decorator-based registration in app/main.py didn't silently break).
    """
    main = importlib.import_module("app.main")
    tool_manager = main.mcp._tool_manager  # FastMCP internal registry
    registered = set(tool_manager._tools.keys()) if hasattr(tool_manager, "_tools") else set()
    expected = {"geo_query", "resolve_with_choice", "resolve_place", "resolve_address_tool", "geometries_to_geojson", "ping"}
    missing = expected - registered
    assert not missing, f"Tools missing after relocation: {missing}"


# --------------------------------------------------------------------------- #
# 2. Config's default env-file path is a no-op when absent (must not crash
#    just because /srv/geo-mcp/.env doesn't exist on a dev machine or in CI)
# --------------------------------------------------------------------------- #

def test_config_loads_with_no_env_file_present(monkeypatch, tmp_path):
    monkeypatch.setenv("GEO_MCP_ENV_FILE", str(tmp_path / "does_not_exist.env"))
    # Force a fresh import so DEFAULT_ENV_FILE re-reads the env var above.
    sys.modules.pop("app.config", None)
    config = importlib.import_module("app.config")
    assert config.PGHOST  # falls back to "localhost", proves no crash occurred


# --------------------------------------------------------------------------- #
# 3. Containerfile: COPY sources must still exist relative to THIS folder.
#    This is the real risk of a monorepo move -- if the image is ever built
#    with the repo root as build context instead of mcp/geo-mcp/, every
#    COPY line below silently resolves to the wrong (or a missing) path.
# --------------------------------------------------------------------------- #

CONTAINERFILE_COPY_SOURCES = ["requirements.txt", "app/"]


@pytest.mark.parametrize("relative_source", CONTAINERFILE_COPY_SOURCES)
def test_containerfile_copy_sources_exist(relative_source):
    if not (GEO_MCP_ROOT / "Containerfile").exists():
        pytest.skip("Containerfile not present here (tests run inside the container image, or the update zip omits it)")
    assert (GEO_MCP_ROOT / relative_source).exists(), (
        f"Containerfile expects to COPY '{relative_source}' from the build "
        f"context. It must be built with mcp/geo-mcp/ as the context, e.g.\n"
        f"  podman build -t geo-mcp:latest -f mcp/geo-mcp/Containerfile mcp/geo-mcp\n"
        f"NOT from the chat-platform repo root."
    )


def test_containerfile_has_no_repo_root_relative_paths():
    """
    Guards against someone 'fixing' the Containerfile to reference
    ../frontend or ../backend style paths once it lives in a monorepo --
    that would only work with a different build context than intended.
    """
    if not (GEO_MCP_ROOT / "Containerfile").exists():
        pytest.skip("Containerfile not present here (tests run inside the container image, or the update zip omits it)")
    text = (GEO_MCP_ROOT / "Containerfile").read_text()
    assert "../" not in text


# --------------------------------------------------------------------------- #
# 4. No secrets committed. This is not caused by the move, but consolidating
#    into a monorepo is exactly the moment this kind of thing gets noticed
#    or silently carried forward -- so it's asserted explicitly.
# --------------------------------------------------------------------------- #

def test_env_file_is_gitignored():
    """
    A real .env living in this working directory is expected -- it's also
    the host bind-mount source the running container reads from, so its
    physical presence here is normal, not a bug. What must be true is that
    git never tracks it, so the credentials inside it never end up in
    version control.
    """
    env_file = GEO_MCP_ROOT / ".env"
    if not env_file.exists():
        pytest.skip(".env not present in this checkout -- nothing to check")

    is_repo = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=GEO_MCP_ROOT, capture_output=True, text=True,
    )
    if is_repo.returncode != 0:
        pytest.skip("not inside a git repo yet -- re-run this check once chat-platform is under git")

    result = subprocess.run(["git", "check-ignore", "-q", str(env_file)], cwd=GEO_MCP_ROOT)
    assert result.returncode == 0, (
        ".env exists but is NOT covered by .gitignore. Add '.env' to "
        ".gitignore (repo root or mcp/geo-mcp/.gitignore) before running "
        "`git add` here, or the real credentials in it will get committed."
    )


# --------------------------------------------------------------------------- #
# 5. requirements.txt still resolves. Slow (hits PyPI) -- opt-in only.
#    Run explicitly with: pytest tests/test_relocation_smoke.py -m slow
# --------------------------------------------------------------------------- #

@pytest.mark.slow
def test_requirements_are_pip_installable(tmp_path):
    result = subprocess.run(
        [
            sys.executable, "-m", "pip", "install",
            "--dry-run", "--no-cache-dir",
            "-r", str(GEO_MCP_ROOT / "requirements.txt"),
        ],
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr
