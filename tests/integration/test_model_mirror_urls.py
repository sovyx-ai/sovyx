"""Sanity checks for the model download URLs hard-coded in _model_downloader.

Marked ``network`` so it is opt-in. CI can run these with
``pytest -m network`` on a scheduled job; the default run skips them so a
flaky internet connection on a dev box doesn't fail the suite.

What breaks if we don't have this:
    v0.16.9 shipped with a mirror URL pointing at a GitHub Release
    (``models-v1``) that had never been created — so the mirror always
    returned 404 in production and the fallback was theater. A simple HEAD
    check catches that class of regression before users hit it.
"""

from __future__ import annotations

import httpx
import pytest

from sovyx.brain._model_downloader import MODEL_URLS, TOKENIZER_URLS
from sovyx.voice.model_registry import (
    _KOKORO_MODEL_URLS,
    _KOKORO_VOICES_URLS,
    _SILERO_URLS,
)

_ACCEPTABLE = {200, 301, 302, 303, 307, 308}

_ALL_URLS = [
    *MODEL_URLS,
    *TOKENIZER_URLS,
    *_SILERO_URLS,
    *_KOKORO_MODEL_URLS,
    *_KOKORO_VOICES_URLS,
]


@pytest.mark.network()
@pytest.mark.parametrize("url", _ALL_URLS)
def test_model_mirror_url_responds(url: str) -> None:
    """Every hard-coded download URL must return a non-4xx/5xx on HEAD.

    Covers brain (e5-small-v2 + tokenizer) and voice (silero-vad, kokoro
    model + voices) URL tables. The voice primary URL for silero started
    504'ing in v0.17.0 via the github.com/.../raw/... redirect layer —
    this test would have caught it before users hit it.
    """
    with httpx.Client(follow_redirects=False, timeout=15.0) as client:
        resp = client.head(url)
    assert resp.status_code in _ACCEPTABLE, (
        f"{url} returned {resp.status_code} — mirror is broken. "
        f"Either the release/asset was deleted or the URL drifted."
    )
