"""Hosted free-tier provider over an OpenAI-compatible chat endpoint (section 3.5):
Groq, OpenRouter or Google AI Studio's compatibility endpoint.

The key arrives as LLM_API_KEY from a Kubernetes Secret created on the command
line. It never appears in a file you commit, and never in a log line.

Structure is enforced with `response_format` (JSON mode, the one every listed
provider supports) with the schema spelled out in the system prompt, then validated
with Pydantic in `generate_checked`.
"""

from __future__ import annotations

import httpx

from ...config import Settings
from ...schemas import Answer, Passage
from .common import Completion, generate_checked


class HostedProvider:
    name = "hosted"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not settings.hosted_base_url:
            raise ValueError("LLM_PROVIDER=hosted needs HOSTED_BASE_URL in the ConfigMap")
        self.url = settings.hosted_base_url.rstrip("/")

    def complete(self, messages: list[dict[str, str]], retry: bool) -> Completion:
        body = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}
        with httpx.Client(timeout=self.settings.llm_timeout_seconds, headers=headers) as client:
            response = client.post(f"{self.url}/chat/completions", json=body)
            response.raise_for_status()
            data = response.json()
        choices = data.get("choices") or [{}]
        usage = data.get("usage") or {}
        return Completion(
            text=str(((choices[0].get("message") or {}).get("content")) or ""),
            tokens_in=int(usage.get("prompt_tokens", 0) or 0),
            tokens_out=int(usage.get("completion_tokens", 0) or 0),
        )

    def generate(self, question: str, passages: list[Passage],
                 older_version_requested: bool = False) -> Answer:
        return generate_checked(self, question, passages, older_version_requested)
