"""The Job waits for Chroma instead of burning its backoffLimit on a cold cluster."""

from __future__ import annotations

import pytest

from pipeline import run


def _settings():  # type: ignore[no-untyped-def]
    from pipeline.config import IndexerSettings
    return IndexerSettings(embed_model="m", chroma_host="chroma", chroma_port=8000,
                           collection="rahbar", corpus_dir="/c", chunk_size=400,
                           chunk_overlap=50, ollama_url="http://o")


def test_wait_for_chroma_retries_until_the_server_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def flaky(settings):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("Could not connect to a Chroma server")
        return "client"

    monkeypatch.setattr("pipeline.store.get_client", flaky)
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    assert run.wait_for_chroma(_settings(), timeout=60, pause=0) == "client"
    assert calls["n"] == 3


def test_wait_for_chroma_gives_up_after_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(settings):  # type: ignore[no-untyped-def]
        raise ValueError("connection refused")

    monkeypatch.setattr("pipeline.store.get_client", down)
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit, match="not reachable"):
        run.wait_for_chroma(_settings(), timeout=0, pause=0)
