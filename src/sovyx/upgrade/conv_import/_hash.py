"""Stable dedup key for imported conversations.

Conversation-level granularity matches Option C (summary-first) — one
Episode per conversation, so the natural key is
``sha256(platform || conversation_id)``. Per-message hashing is
deferred to a future deep-import mode that would produce one Episode
per turn.

Re-importing the same ChatGPT export twice must skip every
conversation; this helper is the key that makes that check trivial.
"""

from __future__ import annotations

import hashlib


def source_hash(platform: str, conversation_id: str) -> str:
    """Return the canonical dedup hash for an imported conversation.

    Stable across processes and Python versions (``hashlib.sha256`` of
    a UTF-8 encoded ``"{platform}:{conversation_id}"`` string). Don't
    change the format without a migration — existing
    ``conversation_imports`` rows would become unfindable.

    Args:
        platform: Lowercase platform identifier (``"chatgpt"``).
        conversation_id: The platform's own stable conversation ID.

    Returns:
        64-character lowercase hex digest.
    """
    key = f"{platform}:{conversation_id}".encode()
    return hashlib.sha256(key).hexdigest()
