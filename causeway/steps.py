"""Per-decision slices of a chat transcript (SPEC 4.1): what the model saw before each decision."""

from __future__ import annotations

from typing import Any


def prefixes(messages: list[dict[str, Any]]) -> list[tuple[list[dict[str, Any]], dict[str, Any]]]:
    """One (messages_before, assistant_message) pair per assistant message, in order.

    messages_before holds every message up to but excluding that assistant message, as a new list, so it is the
    input the model had when it made that decision. Earlier assistant messages are included.
    """
    pairs: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []
    for index, message in enumerate(messages):
        if message.get("role") == "assistant":
            pairs.append((list(messages[:index]), message))
    return pairs
