# Enterprise Audit — Part B (context, mind, llm, voice)

Scope: 31 non-empty files across `src/sovyx/context`, `mind`, `llm`, `voice`.
Scoring: 10 criteria, 0 or 1 each. ENTERPRISE ≥ 8 · DEVELOPED 5–7 · NOT-ENT ≤ 4.

## Summary

| Module  | Files | Avg   | ENTERPRISE | DEVELOPED | NOT-ENT |
|---------|------:|------:|-----------:|----------:|--------:|
| context |     4 |  9.25 |          4 |         0 |       0 |
| mind    |     2 |  9.50 |          2 |         0 |       0 |
| llm     |     9 |  8.67 |          7 |         2 |       0 |
| voice   |    11 |  8.09 |          7 |         4 |       0 |
| **TOTAL** | **26** | **8.54** | **20** | **6** | **0** |

(skipping 5 empty `__init__.py` files.)

---

## context (4 non-empty files)

### File: src/sovyx/context/assembler.py — Score: 10/10 — ENTERPRISE
Pure orchestrator with DI (6 deps injected), structlog + tracer + metrics, typed dataclasses, complete docstrings. History is defensively copied (v12 fix commented). No issues.

### File: src/sovyx/context/budget.py — Score: 10/10 — ENTERPRISE
`TokenBudgetError(SovyxError)`, validated `context_window >= MIN_CONTEXT_WINDOW`, adaptive clamping with documented rules, frozen dataclass return type, logger wired. Math is defensive (overflow path reduces flex slots proportionally). Test file 214 LOC covering edge cases.

### File: src/sovyx/context/formatter.py — Score: 9/10 — ENTERPRISE
Full type hints, StrEnum-backed emoji map, `ZoneInfoNotFoundError` fallback logged. Mutates `concept.metadata["context_inclusion_count"]` inside `format_concepts_block` — side effect during formatting is mildly surprising but contained.
Failed:
- **#10 [CODE QUALITY]**: `item.metadata["context_inclusion_count"] = inc + 1` — formatter mutates input domain object; violates SRP (formatter is not a tracker).

### File: src/sovyx/context/tokenizer.py — Score: 8/10 — ENTERPRISE
Lazy encoding cache, robust `truncate` loop against non-stable decode-encode. Test file 233 LOC.
Failed:
- **#9 [RESILIENCE]**: `tiktoken.get_encoding(...)` first call may download ~1.7MB without retry/timeout; mitigated by `sovyx init` pre-cache but no runtime guard.
- **#6 [CONCURRENCY]**: docstring claims "Thread-safe" but `self._encoding = None` read/write is unguarded; race on first concurrent call (only benign, at worst loads twice).

---

## mind (2 non-empty files)

### File: src/sovyx/mind/config.py — Score: 10/10 — ENTERPRISE
Pydantic v2 with bounded `Field(ge=…, le=…)` everywhere, `model_validator` for weight sums and runtime key resolution, `MindConfigError` with proper chaining, YAML only via `yaml.safe_load`. Three-file test suite (1200+ LOC).

### File: src/sovyx/mind/personality.py — Score: 9/10 — ENTERPRISE
Deterministic prompt builder, hardcoded safety-critical strings (child-safe, anti-injection) correctly non-configurable, StrEnum-style literals on tone.
Failed:
- **#10 [CODE QUALITY]**: `_OCEAN_DESCRIPTORS` lookup via `getattr(o, trait_name)` with a string map duplicated with OCEAN field names — fragile to renames; small blemish.

---

## llm (9 non-empty files)

### File: src/sovyx/llm/circuit.py — Score: 9/10 — ENTERPRISE
Clean FSM, monotonic clock, state transitions correct, structured log on open.
Failed:
- **#6 [CONCURRENCY]**: Counter/state mutations (`_failure_count`, `_state`) unlocked; a single provider's calls from concurrent tasks can double-count. Low-risk but documented concurrency expectation is missing.

### File: src/sovyx/llm/cost.py — Score: 8/10 — ENTERPRISE
Persistence with crash recovery, ring-buffered cost log, daily reset honoring timezone, `defaultdict` breakdowns, tests 771 LOC.
Failed:
- **#10 [CODE QUALITY]**: `with counters._lock:` and `counters._maybe_reset()` — reaches into `DashboardCounters` private attrs from `CostGuard._flush_day_snapshot` (tight coupling across modules, breaks encapsulation).
- **#1 [ERROR HANDLING]**: `except Exception: logger.warning("cost_guard_persist_failed", exc_info=True)` swallows arbitrary exceptions; preferable to narrow to `aiosqlite.Error, OSError`.

### File: src/sovyx/llm/models.py — Score: 8/10 — ENTERPRISE
Pure typed dataclasses. No test dedicated beyond `test_models.py` (47 LOC — tiny but adequate for DTOs).
Failed:
- **#2 [INPUT VALIDATION]**: `finish_reason: str` is a free-form string described as `"stop"|"max_tokens"|"tool_use"|"error"` — should be `Literal[...]` or `StrEnum`.
- **#8 [DOCUMENTATION]**: `arguments: dict[str, object]` — acceptable but no module-level docstring beyond one-liner.

### File: src/sovyx/llm/router.py — Score: 9/10 — ENTERPRISE
ComplexityLevel `StrEnum`, per-provider `CircuitBreaker`, cost pre-check, cross-provider fallback, OTel spans + metrics, event emission. Heavy test coverage (564 LOC).
Failed:
- **#1 [ERROR HANDLING]**: `except Exception as e: … errors.append(f"{provider.name}: {e}")` in the inner try — broad except is load-bearing (router *must* survive any provider crash), but lacks `exc_info=True` on failure path; stack traces are lost.

### File: src/sovyx/llm/providers/_shared.py — Score: 10/10 — ENTERPRISE
Content-type validation, empty-body guard, JSON decode error chaining, Full-Jitter backoff respecting `Retry-After`. Test file 140 LOC.

### File: src/sovyx/llm/providers/anthropic.py — Score: 9/10 — ENTERPRISE
Explicit `httpx.Timeout(connect=30, read=120, write=30, pool=30)`, retry on 429/5xx with jitter, typed errors, pricing table, `ProviderUnavailableError` vs `LLMError` distinction.
Failed:
- **#10 [CODE QUALITY]**: `_PRICING` duplicated between provider and `router._get_pricing` — same literals, two places; drift risk (noted in all four providers).

### File: src/sovyx/llm/providers/openai.py — Score: 8/10 — ENTERPRISE
Same pattern as Anthropic. Good.
Failed:
- **#4 [TESTING]**: No `tests/unit/llm/providers/test_openai.py` — provider-specific behavior (streaming via `tool_calls` shape, context windows for o1/o3-mini) covered only transitively via `test_providers.py` (466 LOC covers multiple). Acceptable but a gap vs. Google which has dedicated 442-LOC file.
- **#10 [CODE QUALITY]**: pricing duplication (same as Anthropic).

### File: src/sovyx/llm/providers/google.py — Score: 9/10 — ENTERPRISE
Dedicated test file (442 LOC). Same retry/timeout discipline. API key in query string is Google's required format (not a secret leak; logged as URL would be — logger.debug doesn't log URL).
Failed:
- **#5 [SECURITY]**: `url = f"{_API_BASE}/{model}:generateContent?key={self._api_key}"` — key in URL query. Google's documented auth, but if `httpx` logs the URL on error (e.g. `ConnectError` str includes URL), key can leak to logs. No redaction.

### File: src/sovyx/llm/providers/ollama.py — Score: 8/10 — ENTERPRISE
`ping()` verification before `is_available=True`, `OLLAMA_HOST` env fallback, handles cloud-model rejection explicitly.
Failed:
- **#1 [ERROR HANDLING]**: `except Exception: self._verified = False` in `ping()` and `list_models()` — blanket catches; acceptable for liveness probe but loses diagnostic info.
- **#4 [TESTING]**: No dedicated `test_ollama.py` — covered only via `test_providers.py`.

---

## voice (11 non-empty files)

### File: src/sovyx/voice/audio.py — Score: 8/10 — ENTERPRISE
Ring buffer with O(1) ops, `AudioPlatform` StrEnum, priority queue, LUFS normalization, thread-safe `call_soon_threadsafe` bridge from PortAudio callback to asyncio queue.
Failed:
- **#1 [ERROR HANDLING]**: `except sd.PortAudioError: continue` in `negotiate_sample_rate` — silently loops on arbitrary portaudio errors; should log rate attempt.
- **#9 [RESILIENCE]**: No timeout on `self._stream.stop()` — PortAudio can block; `stop()` is fully sync inside async method.

### File: src/sovyx/voice/auto_select.py — Score: 7/10 — DEVELOPED
Hardware detection, model matrix, fallback chains, `ModelSelection` frozen dataclass.
Failed:
- **#6 [CONCURRENCY]**: `os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")` — Linux-only syscalls. On Windows (target platform per `env`), `os.sysconf` raises `AttributeError` — no guard. `subprocess.run(["nvidia-smi", ...])` is a sync blocking call that can run inside an async service.
- **#1 [ERROR HANDLING]**: `except (FileNotFoundError, subprocess.TimeoutExpired, ValueError): pass` — silently returns no-GPU on any of those; acceptable but untyped.
- **#9 [RESILIENCE]**: `subprocess.run(...)` at import-detect time blocks for up to 5s; no async variant for CLI/dashboard integration.

### File: src/sovyx/voice/jarvis.py — Score: 9/10 — ENTERPRISE
`StrEnum FillerCategory`, repetition-avoidance, pre-cache of audio chunks, `validate_jarvis_config` raises `ValueError` on invalid bounds. Test 743 LOC.
Failed:
- **#1 [ERROR HANDLING]**: `except Exception: logger.warning("Failed to cache filler", phrase=phrase)` — swallows all exceptions without `exc_info` during `pre_cache`; three instances in this file.

### File: src/sovyx/voice/pipeline.py — Score: 7/10 — DEVELOPED
840 LOC, state machine, barge-in detector, event emission, `VoicePipelineConfig` frozen dataclass, `validate_config`.
Failed:
- **#6 [CONCURRENCY]**: `self._queue: asyncio.Queue[AudioChunk] = asyncio.Queue()` constructed in `AudioOutputQueue.__init__` (and `self._first_token_event = asyncio.Event()` in `VoicePipeline.__init__`) — creating asyncio primitives outside of a running loop is a long-standing footgun (ties queue to loop at that moment; fails in multi-loop/test setups with DeprecationWarning on 3.10+).
- **#1 [ERROR HANDLING]**: Multiple `except Exception as exc` without `exc_info=True` in `_end_recording`, `speak`, `stream_text`, `flush_stream`, `_emit`. Diagnostic loss on failure chain.
- **#10 [CODE QUALITY]**: `VoicePipeline` class is 450+ LOC with 15 methods and owns state machine + audio + jarvis + bargein + events + streaming — SRP violation; `pipeline.py` file is 840 LOC (largest in scope). Note: pipeline handlers + orchestrator + output queue + barge-in + re-export of JarvisIllusion all in one file.

### File: src/sovyx/voice/stt.py — Score: 8/10 — ENTERPRISE
ABC + concrete, `STTState` IntEnum lifecycle guard, `asyncio.wait_for(..., timeout=…)` with `TimeoutError` handling, streaming via queue + listener.
Failed:
- **#1 [ERROR HANDLING]**: `TimeoutError` caught → returns `text=""` silently; no retry, no error propagation — downstream cannot distinguish "silent audio" from "STT stalled".
- **#8 [DOCUMENTATION]**: `self._transcriber: object | None = None` then `# type: ignore[attr-defined]` on `.create_stream(...)` and `.add_listener(...)` — lost typing on the external dependency; a `Protocol` wrapper would document the contract.

### File: src/sovyx/voice/stt_cloud.py — Score: 9/10 — ENTERPRISE
API key validated on init, `_MAX_AUDIO_DURATION_S=120` hard cap, WAV encoding validated, `CloudSTTError(VoiceError)`, `httpx.Timeout` wired from config.
Failed:
- **#9 [RESILIENCE]**: No retry on transient 5xx/429 from Whisper; `_call_whisper_api` single shot. Contrasts with LLM providers that retry 3×.

### File: src/sovyx/voice/tts_kokoro.py — Score: 8/10 — ENTERPRISE
Config validation, model fallback q8→full, StrEnum-style language allowlist. 784 LOC test.
Failed:
- **#6 [CONCURRENCY]**: `self._kokoro.create(text, …)` is synchronous CPU/ONNX work called directly inside `async def synthesize` — blocks the event loop for the full TTS duration (can be seconds). Should use `asyncio.to_thread(...)`.
- **#1 [ERROR HANDLING]**: `except Exception: logger.warning("Failed to list Kokoro voices")` in `list_voices` — no `exc_info`.

### File: src/sovyx/voice/tts_piper.py — Score: 8/10 — ENTERPRISE
Config validator, phoneme-id truncation guard (`_MAX_PHONEME_IDS=50_000`), ABC base, streaming synthesis.
Failed:
- **#6 [CONCURRENCY]**: Same as Kokoro — `self._session.run(None, args)` synchronous ONNX inference inside `async def synthesize`, blocks the loop.
- **#10 [CODE QUALITY]**: `synthesize_streaming` abstract method uses `if False: yield` hack to satisfy `AsyncIterator` typing — workable but unusual; a `@overload`-based signature would be cleaner.

### File: src/sovyx/voice/vad.py — Score: 9/10 — ENTERPRISE
Hysteresis FSM with `VADState` IntEnum (mild — see below), `VADConfig` frozen, validated (`offset < onset`), persistent LSTM state.
Failed:
- **#9 [RESILIENCE]**: `onnxruntime.InferenceSession(model_path, …)` raises on missing file but no graceful fallback to a cpu-lite VAD; voice pipeline hard-fails on bad model.

### File: src/sovyx/voice/wake_word.py — Score: 8/10 — ENTERPRISE
2-stage verification, config validation, state machine, audio buffer for verifier. 642-LOC test.
Failed:
- **#6 [CONCURRENCY]**: `self._session.run(None, ort_inputs)` synchronous ONNX on every frame — called from async pipeline path. 1280 samples @ 16 kHz = 80 ms frames; inference time < frame duration on Pi5 so it works, but blocks the loop for 5 ms on each call.
- **#10 [CODE QUALITY]**: `WakeWordState` uses `IntEnum` not `StrEnum` — per CLAUDE.md anti-pattern #9 "All enums with string values MUST inherit from StrEnum". These are pure-integer states so it's fine, but inconsistent with `ComplexityLevel` in llm.

### File: src/sovyx/voice/wyoming.py — Score: 6/10 — DEVELOPED
Protocol-driven (good), TCP server + Zeroconf, `WyomingClientHandler` per-connection.
Failed:
- **#5 [SECURITY]**: `host: str = "0.0.0.0"` default with `noqa: S104` — binds all interfaces, **no authentication on the TCP listener**. Any host on the LAN can speak Wyoming to Sovyx and invoke `CogLoop.generate_response` (free LLM-costed queries against the owner's budget) or STT/TTS. This is explicit/intentional per Wyoming spec, but for an enterprise audit it is a critical gap; no allowlist, no token, no TLS.
- **#5 [SECURITY]**: `_handle_intent` forwards arbitrary attacker-supplied `text` to `cogloop.generate_response(text)` → LLM cost. No rate limit, no size cap.
- **#2 [INPUT VALIDATION]**: `event.data.get("text", "")` — no size/length cap on synthesize or transcript payloads; `audio_buffer` grows unbounded until `audio-stop` (DoS: attacker streams chunks forever).
- **#9 [RESILIENCE]**: No timeout on `reader.readline()` / `readexactly(payload_length)` — a slow-loris client can pin a task forever.

---

## Top issues across B

1. **Wyoming TCP listener is unauthenticated (critical).** `0.0.0.0:10700` exposes STT+TTS+CogLoop to the LAN with no token or allowlist, no rate limit, no size caps, no read timeouts. Hostile LAN peer can exhaust LLM budget or pin connections (slow-loris). `src/sovyx/voice/wyoming.py:142,597,473`.

2. **Sync ONNX / external native calls inside `async def` (event-loop starvation).** `tts_piper._synthesize_ids`, `tts_kokoro.synthesize`, `wake_word._run_inference`, `vad.process_frame`, `stt._transcribe_oneshot` all run heavy CPU work on the loop thread. Pipeline co-residents (HTTP, dashboard) stall during TTS. `asyncio.to_thread` is the fix.

3. **`asyncio.Queue()` / `asyncio.Event()` created in `__init__`.** `voice/pipeline.py:213, 447`, `voice/audio.py:224` — anti-pattern since 3.10; breaks under nested loops/tests and binds to whichever loop happens to be running at construction (often `None`).

4. **Google provider: API key in URL query string.** `llm/providers/google.py:132` — Google's documented contract, but any `httpx` error that stringifies the URL leaks the key to logs. No redaction filter on the logger side.

5. **`except Exception:` without `exc_info=True`** in 15+ places across `voice/pipeline.py`, `voice/jarvis.py`, `llm/cost.py`, `llm/router.py`, `voice/tts_kokoro.py`. Diagnostic loss on the hot path that matters most (production degradation).

6. **Pricing table duplicated 5×.** `llm/router.py`, and each of the four provider files carry their own `_PRICING` dict. Single source of truth missing → rate drift between estimation (router) and recording (provider).

7. **God file: `voice/pipeline.py` 840 LOC.** Output queue + barge-in + state machine + orchestrator + Jarvis re-export. Split into `pipeline/state.py`, `pipeline/output.py`, `pipeline/bargein.py`, `pipeline/orchestrator.py`.

8. **`CostGuard` reaches into `DashboardCounters._lock` / `_maybe_reset`.** Private-attr access across module boundaries breaks encapsulation and creates an implicit coupling that only holds as long as both files happen to define those attrs.

9. **`auto_select.py` uses Linux-only `os.sysconf`.** Breaks on Windows (the documented primary dev platform). No `platform.system()` guard.

10. **STT timeout swallowed into empty string.** `voice/stt.py:436` catches `TimeoutError` and returns `text=""` indistinguishable from silence — the pipeline discards "empty transcription" silently, masking stuck STT.
