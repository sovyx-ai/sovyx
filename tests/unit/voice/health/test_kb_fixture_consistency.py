"""CI-side HIL-fixture consistency — every shipped KB profile must
have its companion attestation fixtures.

This test is the automated gate that backs ``sovyx kb fixtures all``
for contributors. When a PR adds a new profile YAML under
``_mixer_kb/profiles/`` without the accompanying fixture triple, this
test fails — loudly — at CI time, keeping the shipping pool honest.

Fixture triple (V2 Master Plan §F.3):

* ``<profile_id>_before.txt`` — pre-heal ``amixer -c N contents`` dump
* ``<profile_id>_after.txt``  — post-heal ``amixer -c N contents`` dump
* ``<profile_id>_capture.wav``— 3-second validation capture for
  reviewer replay

Scope:

* Runs on every platform (fixture files are plain text / WAV — no
  Linux-only subprocess). A macOS / Windows reviewer running the full
  gate catches missing fixtures the same way Linux CI does.

* When the shipped directory is empty (Phase F1 status as of this
  writing), the test is a trivial no-op — the guard is there for the
  moment profiles start landing.
"""

from __future__ import annotations

from pathlib import Path

from sovyx.cli.commands.kb import _missing_fixtures
from sovyx.voice.health._mixer_kb import _SHIPPED_PROFILES_DIR
from sovyx.voice.health._mixer_kb.loader import load_profiles_from_directory

_REPO_ROOT = Path(__file__).resolve().parents[4]
"""Four levels up from this test file lands us at ``E:\\sovyx`` /
``/repo``. The fixtures root is reached from there; the traversal is
intentionally hard-coded because pytest's ``rootdir`` discovery can
drift under xdist."""


_FIXTURES_ROOT = _REPO_ROOT / "tests" / "fixtures" / "voice" / "mixer"


def test_every_shipped_profile_has_hil_fixtures() -> None:
    """Each shipped profile_id must have all three HIL fixtures.

    Fails with a per-profile list of missing fixture paths, so a
    contributor can copy-paste the offending lines directly from
    the CI log.
    """
    shipped = load_profiles_from_directory(_SHIPPED_PROFILES_DIR)
    if not shipped:
        # Empty shipping pool — Phase F1 pre-H. The guard flips on
        # with the first contribution.
        return

    # In the unusual case the fixtures root itself is missing,
    # surface a clear message rather than a generic OSError in the
    # loop below. (Shouldn't happen: the directory is a tracked repo
    # artefact.)
    assert _FIXTURES_ROOT.is_dir(), (
        f"Fixtures root missing: {_FIXTURES_ROOT} — tracked artefact "
        "at tests/fixtures/voice/mixer must exist"
    )

    failures: list[str] = []
    for profile in shipped:
        missing = _missing_fixtures(profile.profile_id, _FIXTURES_ROOT)
        for name in missing:
            failures.append(f"{profile.profile_id}: missing {name}")

    if failures:
        joined = "\n".join(failures)
        assert False, (  # noqa: PT015, B011 — structured message aids triage
            f"{len(failures)} HIL fixture file(s) missing:\n{joined}\nUnder: {_FIXTURES_ROOT}"
        )


def test_fixtures_root_only_contains_expected_files() -> None:
    """Reverse-consistency: every file under ``fixtures/voice/mixer``
    should map back to a shipped profile.

    An orphaned fixture (``foo_before.txt`` with no ``foo.yaml`` in
    the shipping pool) isn't a correctness failure on its own, but
    it's a strong smell that a profile was reverted without its
    fixtures being cleaned up. The test logs the discrepancy so a
    reviewer spotting it in a PR diff can decide whether to restore
    the profile or drop the fixtures.

    Skipped when the fixtures directory is absent (repo clone that
    trimmed test artefacts — pip wheels, sdists).
    """
    if not _FIXTURES_ROOT.is_dir():
        return
    shipped_ids = {p.profile_id for p in load_profiles_from_directory(_SHIPPED_PROFILES_DIR)}
    orphans: list[str] = []
    for child in sorted(_FIXTURES_ROOT.iterdir()):
        if not child.is_file():
            continue
        stem = child.name
        matched = False
        for pid in shipped_ids:
            if stem.startswith(f"{pid}_"):
                matched = True
                break
        if not matched:
            orphans.append(child.name)
    # Orphan-free is the happy case; otherwise we fail so a reviewer
    # catches the stale data rather than letting it rot.
    assert not orphans, (
        f"Orphan fixture files (no matching shipped profile): {orphans!r}\n"
        "Either restore the profile YAML or delete the fixtures."
    )
