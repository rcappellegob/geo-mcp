"""Shared fixtures. DB tests build a SYNTHETIC scratch database (tests/synth_urbis.py) -- never your real one.
They are skipped automatically when no PostGIS server is reachable (set PGHOST/PGPORT/PGUSER/PGPASSWORD)."""
import os
import sys
from pathlib import Path

os.environ.setdefault("GEO_MCP_LOG_DIR", "/tmp/geo-mcp-test-logs")
os.environ["PGDATABASE"] = os.environ.get("GEO_TEST_DB", "geotest")
os.environ.setdefault("PGPASSWORD", os.environ.get("PGPASSWORD", "pw"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest


@pytest.fixture(scope="session")
def synth_db():
    try:
        import synth_urbis
        synth_urbis.make_db(os.environ["PGDATABASE"])
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no PostGIS/pgRouting server for the synthetic database: {exc}")
    return os.environ["PGDATABASE"]
