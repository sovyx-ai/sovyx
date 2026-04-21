# Model Mirrors

Sovyx ships with multi-source fallback for every ML model it consumes.
When a primary upstream host is unreachable (transient CDN outage,
rate-limit, regional DNS failure), the `ModelDownloader` falls through
to a Sovyx-owned GitHub Release that mirrors the upstream asset with
checksum verification.

This document is the registry of those mirrors plus the naming policy
that governs future additions.

## Naming convention

All model mirror releases follow the pattern:

```
<domain>-models-v{N}
```

Where `<domain>` is one of:

| Domain      | Scope                                        | Tag prefix         |
|-------------|----------------------------------------------|--------------------|
| `voice`     | STT, TTS, VAD, wake-word                     | `voice-models-v`   |
| `embedding` | Sentence / document embeddings               | `embedding-models-v` *(future)* |
| `vision`    | Reserved for future image models             | `vision-models-v`  |

The integer `{N}` bumps only when the asset set changes in a way that
breaks downstream code (new checksum, renamed file, incompatible
schema). Same-domain models that evolve independently may ship under
the same version until a breaking change forces a bump.

## Current registry

| Release tag        | Domain    | Assets                                                           | Consumer code                                |
|--------------------|-----------|------------------------------------------------------------------|----------------------------------------------|
| `models-v1`        | embedding | `e5-small-v2.onnx`, `tokenizer.json`                             | `sovyx.brain._model_downloader`              |
| `voice-models-v1`  | voice     | `silero_vad.onnx`, `kokoro-v1.0.int8.onnx`, `voices-v1.0.bin`    | `sovyx.voice.model_registry`                 |

### Historical artifact: `models-v1`

The `models-v1` tag predates the domain-prefixed convention. It was
created before Sovyx gained voice pipelines and, at the time, "models"
was synonymous with "embedding models". The tag is preserved unchanged
for **production compatibility** — every installed copy of Sovyx
v0.20.4 and earlier references this exact URL in its fallback chain
(`src/sovyx/brain/_model_downloader.py`).

Renaming the tag would break those installations on the fallback path
(primary upstream → rename break → no recovery), in exchange for
nothing but cosmetic consistency. Amazon S3, Google Cloud Storage, and
every major public artifact registry treat URLs as immutable contracts
for the same reason.

**Policy going forward**: when `e5-small-v2` is eventually replaced by
a newer embedding model, the replacement ships as `embedding-models-v2`
under the current naming convention. `models-v1` becomes a frozen
legacy mirror; nothing new lands there.

## Provenance and licensing

Every asset MUST record:

- **Upstream source** — a versioned URL or release tag at the origin
  project (not a `master` branch reference that can silently change).
- **SHA-256** — verified post-download by `ModelDownloader._verify_checksum`.
  Checksum drift between releases is a release-gate failure.
- **License** — inherited from upstream; noted in the release body.

Current provenance:

| Asset                     | Upstream                                                                                     | License    |
|---------------------------|----------------------------------------------------------------------------------------------|------------|
| `e5-small-v2.onnx`        | huggingface.co/intfloat/e5-small-v2                                                          | MIT        |
| `tokenizer.json`          | huggingface.co/intfloat/e5-small-v2                                                          | MIT        |
| `silero_vad.onnx`         | github.com/snakers4/silero-vad @ master (pinned by SHA-256)                                  | MIT        |
| `kokoro-v1.0.int8.onnx`   | github.com/thewh1teagle/kokoro-onnx, release `model-files-v1.0`                              | Apache 2.0 |
| `voices-v1.0.bin`         | github.com/thewh1teagle/kokoro-onnx, release `model-files-v1.0`                              | Apache 2.0 |

## Why separate releases per domain

Bundling every model into one omnibus release (`models-v1` holding
voice + embedding + future vision) was considered and rejected:

1. **Independent cadences.** Kokoro may release v1.1 weeks before an
   embedding model is retrained. One release per domain keeps the
   blast radius of any update contained to consumers of that domain.
2. **Checksum stability.** A single-release model invalidates caches
   for consumers that didn't change. Operators on a voice-only path
   would re-download `e5-small-v2` for nothing.
3. **License surface.** Each domain inherits different upstream
   licenses; isolating them simplifies compliance audits.
4. **Usage telemetry.** GitHub exposes per-release download counts;
   domain-separated releases expose domain-level usage patterns
   without needing extra instrumentation.

This is the same model used by HuggingFace (one repo per model),
Pytorch Hub (one release per submodel), and every big-tech internal
model registry (Uber Michelangelo, LinkedIn Pro-ML, Meta's Ax).

## Adding a new mirror

1. Upload the asset to a new GitHub Release tagged `<domain>-models-v{N}`.
2. Record the SHA-256 in the release body and in `ModelDownloader`
   call sites (or the config block that feeds them).
3. Pin the upstream source to a SHA-immutable URL (release tag, commit
   SHA, HuggingFace revision hash — never a `master`/`main` branch).
4. Update this document's registry table in the same commit.
5. If the asset supersedes an existing one, do NOT delete the old
   release. Mark the superseded asset in this document under the
   "Historical artifact" pattern and point the consumer code at the
   new URL. Old installations continue resolving via the legacy URL.
