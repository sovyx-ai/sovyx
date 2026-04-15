"""LLM prompt for DREAM phase pattern extraction (SPE-003 phase 7).

Kept separate from ``brain/dream.py`` to match the layout of
``cognitive/reflect/_prompts.py`` — underscore-prefixed module
signals "internal, accessed via parent package".

The prompt asks the LLM to find *recurring themes* across a bundle
of recent conversation summaries, not to extract every fact. Output
is JSON — malformed or over-ambitious responses fall back to ``[]``
in the caller, so the model is pushed to err on the side of fewer,
higher-confidence patterns.
"""

from __future__ import annotations

_PATTERN_EXTRACTION_PROMPT = (
    "You are the DREAM phase of a persistent AI companion's memory system.\n"
    "You see a window of recent conversations. Your job is to surface\n"
    "recurring THEMES — implicit patterns that span multiple conversations\n"
    "but were never stated explicitly. These become long-term insights.\n"
    "\n"
    "Return a JSON array of objects with these fields:\n"
    '- "name": short theme label (2-6 words, noun-phrase)\n'
    '- "content": one-sentence description of the pattern\n'
    '- "importance": float 0.0-1.0 (how load-bearing across episodes?)\n'
    "\n"
    "Rules:\n"
    "- A theme must appear in at least TWO distinct episodes.\n"
    "- Prefer stable traits, preferences, and recurring concerns\n"
    "  over one-off facts (those are handled by the Reflect phase).\n"
    "- Skip greetings, filler, single-conversation topics.\n"
    "- Return AT MOST {max_patterns} themes, highest-importance first.\n"
    "- If no recurring themes are visible, return [].\n"
    "- Return ONLY the JSON array, no prose, no code fences.\n"
    "\n"
    "Recent episodes ({count} total, chronological):\n"
    "{episodes}"
)


def build_pattern_prompt(episode_digest: str, count: int, max_patterns: int) -> str:
    """Substitute placeholders into the pattern-extraction prompt.

    Args:
        episode_digest: Pre-rendered episode summary block (see
            ``brain/dream.py::_render_episodes``).
        count: Number of episodes in the window.
        max_patterns: Upper bound the LLM is asked to respect.

    Returns:
        Fully-formed prompt string ready for ``LLMRouter.generate``.
    """
    return _PATTERN_EXTRACTION_PROMPT.format(
        max_patterns=max_patterns,
        count=count,
        episodes=episode_digest,
    )
