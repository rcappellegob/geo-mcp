"""
app/llm.py
==========
Client for the nginx-routed, Ollama-compatible LLM/embedding backends.
Payload shape confirmed working against the real infrastructure.
"""

from __future__ import annotations

import json
from typing import Optional

import requests

from app.config import ACTIVE_EMBEDDING_BACKEND, ACTIVE_LLM_BACKEND, EMBEDDING_BACKENDS, LLM_BACKENDS

DEFAULT_TIMEOUT = 30

# Passed through to Ollama's "options" field. temperature=0 for deterministic,
# reproducible structured-extraction output.
LLM_OPTIONS = {"temperature": 0}


def llm_json(prompt: str, backend: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Send a prompt to an LLM backend and parse its JSON response."""
    cfg = LLM_BACKENDS[backend or ACTIVE_LLM_BACKEND]
    payload = {
        "model": cfg["model"],
        "prompt": prompt,
        "stream": False,
        "options": LLM_OPTIONS,
        "format": "json",
    }
    resp = requests.post(f"{cfg['base_url']}/api/generate", json=payload, timeout=timeout)
    resp.raise_for_status()
    raw = resp.json().get("response", "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"LLM backend {backend or ACTIVE_LLM_BACKEND} did not return valid JSON: {raw!r}"
        ) from exc


def embed(text: str, backend: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT) -> list[float]:
    """Return an embedding vector for a piece of text."""
    cfg = EMBEDDING_BACKENDS[backend or ACTIVE_EMBEDDING_BACKEND]
    resp = requests.post(
        f"{cfg['base_url']}/api/embeddings",
        json={"model": cfg["model"], "prompt": text},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]
