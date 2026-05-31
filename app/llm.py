"""Claude client wrapper — used only by intake (chat + JD/URL extraction).

Centralises the Anthropic client so prompt caching and model selection live in
one place. Guards on ANTHROPIC_API_KEY being absent (it's added later) so the
rest of the app boots and non-LLM paths keep working.
"""

from __future__ import annotations

from . import config


class LLMUnavailable(RuntimeError):
    """Raised when an LLM call is attempted without ANTHROPIC_API_KEY set."""


def available() -> bool:
    return bool(config.ANTHROPIC_API_KEY)


def client():
    """Lazily build the Anthropic client. Import is lazy so the dependency is
    only required when intake actually runs."""
    if not available():
        raise LLMUnavailable(
            "ANTHROPIC_API_KEY is not set. The conversational intake needs it; "
            "the rest of the app works without it."
        )
    from anthropic import Anthropic  # lazy import

    return Anthropic(api_key=config.ANTHROPIC_API_KEY)
