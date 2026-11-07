"""Ollama provider: ollama-host or ollama-incluster (section 3.5).

Structure is enforced with `format=<JSON schema>` on /api/chat (Ollama constrains
decoding to the schema), then validated again with Pydantic in `generate_checked`.
Temperature comes from the ConfigMap. The call has a timeout so a stalled model
cannot pin a worker.
"""

from __future__ import annotations

import httpx

from ...config import Settings
from ...schemas import Answer, Passage
from .common import Completion, generate_checked
from .prompt import answer_schema


class OllamaProvider:
    name = "ollama"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.url = settings.ollama_url.rstrip("/")

    def complete(self, messages: list[dict[str, str]], retry: bool) -> Completion:
        body = {
            "model": self.settings.llm_model,
            "messages": messages,
            "format": answer_schema(),
            "stream": False,
            "options": {"temperature": self.settings.temperature, "num_ctx": 8192},
        }
        with httpx.Client(timeout=self.settings.llm_timeout_seconds) as client:
            response = client.post(f"{self.url}/api/chat", json=body)
            response.raise_for_status()
            data = response.json()
        return Completion(
            text=str((data.get("message") or {}).get("content", "")),
            tokens_in=int(data.get("prompt_eval_count", 0) or 0),
            tokens_out=int(data.get("eval_count", 0) or 0),
        )

    def generate(self, question: str, passages: list[Passage],
                 older_version_requested: bool = False) -> Answer:
        return generate_checked(self, question, passages, older_version_requested)
