"""Enforce, then validate, then retry once (section 3.4), for every real provider.

A provider implements one method, `complete(messages, retry) -> Completion`, that
calls its API with the strongest structure enforcement it has. `generate_checked`
wraps it: parse the reply into an `Answer`; on a parse or validation failure, call
again with the error quoted back; on a second failure raise `GenerationError`, which
the answer service turns into a safe canned response. A user never sees a 500
because the model wrote prose.

Token counts and retries are recorded on the Prometheus metrics here, so each
provider stays a thin HTTP call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from ...routes import metrics
from ...schemas import Answer, Passage
from .prompt import RETRY_SUFFIX, AnswerParseError, build_messages, parse_answer

log = logging.getLogger("rahbar.llm")


class GenerationError(RuntimeError):
    """The model could not produce a valid Answer within the retry budget."""


@dataclass
class Completion:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0


class Completer(Protocol):
    name: str

    def complete(self, messages: list[dict[str, str]], retry: bool) -> Completion:
        ...


def generate_checked(completer: Completer, question: str, passages: list[Passage],
                     older_version_requested: bool = False) -> Answer:
    messages = build_messages(question, passages, older_version_requested)
    last_error = ""
    for attempt in (1, 2):
        try:
            completion = completer.complete(messages, retry=attempt == 2)
        except httpx.TimeoutException as exc:
            raise GenerationError(f"{completer.name} timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise GenerationError(f"{completer.name} call failed: {exc}") from exc
        metrics.TOKENS_IN.inc(completion.tokens_in)
        metrics.TOKENS_OUT.inc(completion.tokens_out)
        metrics.TOKENS_PER_REQUEST.labels(direction="in").observe(completion.tokens_in)
        metrics.TOKENS_PER_REQUEST.labels(direction="out").observe(completion.tokens_out)
        try:
            return parse_answer(completion.text)
        except AnswerParseError as exc:
            last_error = str(exc)
            metrics.LLM_RETRIES.inc()
            log.warning("model reply failed validation",
                        extra={"attempt": attempt, "error": last_error,
                               "reply_head": completion.text[:200]})
            messages = messages[:1] + [{
                "role": "user",
                "content": messages[1]["content"] + RETRY_SUFFIX.format(error=last_error),
            }]
    raise GenerationError(f"invalid answer after 2 attempts: {last_error}")
