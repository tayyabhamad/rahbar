"""Hosted free-tier provider over an OpenAI-compatible chat endpoint (section 3.5):
Google AI Studio (`.../v1beta/openai`), Groq or OpenRouter.

The key arrives as LLM_API_KEY from a Kubernetes Secret created on the command
line. It never appears in a file you commit, and never in a log line.

Structure is enforced with `response_format` (JSON mode, the one every listed
provider supports) with the schema spelled out in the system prompt, then validated
with Pydantic in `generate_checked`. Free tiers rate-limit, so a 429 or a 5xx is
retried with backoff (honouring Retry-After) before the call counts as failed.
"""

from __future__ import annotations

import logging
import time

import httpx

from ...config import Settings
from ...embedding import retry_wait
from ...schemas import Answer, Passage
from .common import Completion, generate_checked

log = logging.getLogger("rahbar.llm.hosted")

RETRIES = 4


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
        for attempt in range(1, RETRIES + 1):
            with httpx.Client(timeout=self.settings.llm_timeout_seconds,
                              headers=headers) as client:
                response = client.post(f"{self.url}/chat/completions", json=body)
            if (response.status_code == 429 or response.status_code >= 500) \
                    and attempt < RETRIES:
                wait = retry_wait(response, attempt)
                log.warning("hosted provider rate-limited or failed; retrying",
                            extra={"status": response.status_code, "wait": wait,
                                   "attempt": attempt})
                time.sleep(wait)
                continue
            response.raise_for_status()
            data = response.json()
            break
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
