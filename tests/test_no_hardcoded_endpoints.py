"""The LLM host lives in the .env (LLM_NGINX_BASE), never in the code."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"
IP_URL = re.compile(r"https?://\d{1,3}(?:\.\d{1,3}){3}")


def test_no_ip_address_urls_in_the_source():
    offenders = [f"{p.relative_to(APP)}:{i}" for p in APP.rglob("*.py") for i, line in enumerate(p.read_text().splitlines(), 1) if IP_URL.search(line)]
    assert not offenders, f"hard-coded http://<ip> URLs: {offenders}"


def _run(env_value):
    code = ("import os, sys\n"
            "sys.path.insert(0, %r)\n"
            "from app.config import LLM_BACKENDS\n"
            "print(LLM_BACKENDS['qwen']['base_url'])\n" % str(APP.parent))
    env = {"PATH": "/usr/bin:/bin", "GEO_MCP_ENV_FILE": "/nonexistent/.env", "GEO_MCP_LOG_DIR": "/tmp/geo-mcp-test-logs"}
    if env_value is not None:
        env["LLM_NGINX_BASE"] = env_value
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env).stdout.strip()


def test_base_url_comes_from_the_environment():
    assert _run("http://llm.example.test:8080/") == "http://llm.example.test:8080/qwen"   # trailing slash tolerated


def test_missing_base_url_stops_with_a_clear_message():
    assert _run(None) == "/qwen"                 # nothing guessed ...
    from app import llm
    with pytest.raises(RuntimeError, match="LLM_NGINX_BASE is not set"):
        llm._base_url({"base_url": "/qwen"})     # ... and the first LLM call explains what to do
