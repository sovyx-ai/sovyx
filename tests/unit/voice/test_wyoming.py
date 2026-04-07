"""Tests for Wyoming protocol server (V05-26).

Covers: wire format, service discovery, STT flow, TTS flow,
wake word detection, intent handling, server lifecycle,
audio conversion, zeroconf, and edge cases.
"""

from __future__ import annotations

import asyncio
import json
import struct
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from sovyx.voice.wyoming import (
    SOVYX_ATTRIBUTION,
    WYOMING_SERVICE_TYPE,
    SovyxWyomingServer,
    WyomingClientHandler,
    WyomingConfig,
    WyomingEvent,
    build_service_info,
    get_local_ip,
    ndarray_to_pcm_bytes,
    pcm_bytes_to_ndarray,
    write_event,
)

# ---------------------------------------------------------------------------
# Test fixtures and helpers
# ---------------------------------------------------------------------------


@dataclass
class MockTranscriptionResult:
    """Mock STT result."""

    text: str
    confidence: float = 0.95


@dataclass
class MockAudioChunk:
    """Mock TTS output chunk."""

    audio: np.ndarray
    sample_rate: int = 22050
    duration_ms: float = 0.0


@dataclass
class MockWakeResult:
    """Mock wake word detection result."""

    detected: bool
    name: str = "hey_sovyx"


class MockStreamReader:
    """Mock asyncio.StreamReader that returns queued events."""

    def __init__(self, events: list[WyomingEvent] | None = None) -> None:
        self._events = list(events) if events else []
        self._index = 0

    def add_event(self, event: WyomingEvent) -> None:
        """Queue an event."""
        self._events.append(event)

    async def readline(self) -> bytes:
        """Return the next event's header line."""
        if self._index >= len(self._events):
            return b""
        event = self._events[self._index]
        self._index += 1
        header: dict[str, object] = {"type": event.type}
        if event.data:
            header["data"] = event.data
        if event.payload:
            header["payload_length"] = len(event.payload)
        return (json.dumps(header, separators=(",", ":")) + "\n").encode("utf-8")

    async def readexactly(self, n: int) -> bytes:
        """Return binary payload from the previous event."""
        idx = self._index - 1
        if 0 <= idx < len(self._events):
            event = self._events[idx]
            if event.payload:
                return event.payload[:n]
        return b"\x00" * n


class MockStreamWriter:
    """Mock asyncio.StreamWriter that captures written data."""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False
        self._extra: dict[str, object] = {"peername": ("127.0.0.1", 12345)}

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass

    def get_extra_info(self, key: str, default: object = None) -> object:
        return self._extra.get(key, default)

    def get_events(self) -> list[WyomingEvent]:
        """Parse written bytes back into WyomingEvent objects."""
        events = []
        for data in self.written:
            text = data.decode("utf-8", errors="replace")
            # Split header from payload
            nl_idx = text.find("\n")
            if nl_idx < 0:
                continue
            header_str = text[:nl_idx]
            try:
                header = json.loads(header_str)
            except json.JSONDecodeError:
                continue
            payload_len = header.get("payload_length", 0)
            payload = data[nl_idx + 1 : nl_idx + 1 + payload_len] if payload_len else b""
            events.append(WyomingEvent(
                type=header.get("type", ""),
                data=header.get("data", {}),
                payload=payload,
            ))
        return events


def _make_handler(
    events: list[WyomingEvent] | None = None,
    config: WyomingConfig | None = None,
    stt_engine: object | None = None,
    tts_engine: object | None = None,
    wake_engine: object | None = None,
    cogloop: object | None = None,
) -> tuple[WyomingClientHandler, MockStreamReader, MockStreamWriter]:
    """Create a handler with mock reader/writer."""
    reader = MockStreamReader(events)
    writer = MockStreamWriter()
    cfg = config or WyomingConfig()
    handler = WyomingClientHandler(
        reader=reader,
        writer=writer,
        config=cfg,
        stt_engine=stt_engine,
        tts_engine=tts_engine,
        wake_engine=wake_engine,
        cogloop=cogloop,
    )
    return handler, reader, writer


# ---------------------------------------------------------------------------
# WyomingEvent wire format
# ---------------------------------------------------------------------------


class TestWyomingEvent:
    """Test Wyoming wire format serialization/deserialization."""

    def test_simple_event_to_bytes(self) -> None:
        """Simple event serializes to JSONL."""
        event = WyomingEvent(type="describe")
        raw = event.to_bytes()
        assert raw.endswith(b"\n")
        header = json.loads(raw.decode("utf-8").strip())
        assert header["type"] == "describe"
        assert "payload_length" not in header

    def test_event_with_data(self) -> None:
        """Event with data includes data in header."""
        event = WyomingEvent(type="transcript", data={"text": "hello", "language": "en"})
        raw = event.to_bytes()
        header = json.loads(raw.decode("utf-8").strip())
        assert header["data"]["text"] == "hello"
        assert header["data"]["language"] == "en"

    def test_event_with_payload(self) -> None:
        """Event with payload includes payload_length and appends bytes."""
        payload = b"\x00\x01" * 320  # 640 bytes
        event = WyomingEvent(
            type="audio-chunk",
            data={"rate": 16000, "width": 2, "channels": 1},
            payload=payload,
        )
        raw = event.to_bytes()
        nl_idx = raw.index(b"\n")
        header = json.loads(raw[:nl_idx].decode("utf-8"))
        assert header["payload_length"] == 640
        assert raw[nl_idx + 1 :] == payload

    def test_empty_payload_no_length(self) -> None:
        """Event with empty payload omits payload_length."""
        event = WyomingEvent(type="audio-stop")
        raw = event.to_bytes()
        header = json.loads(raw.decode("utf-8").strip())
        assert "payload_length" not in header

    @pytest.mark.asyncio
    async def test_read_simple_event(self) -> None:
        """Read a simple event from stream."""
        reader = MockStreamReader([WyomingEvent(type="describe")])
        event = await WyomingEvent.read_from(reader)
        assert event is not None
        assert event.type == "describe"
        assert event.data == {}

    @pytest.mark.asyncio
    async def test_read_event_with_payload(self) -> None:
        """Read an event with binary payload."""
        payload = b"\x00\x01" * 100
        reader = MockStreamReader([
            WyomingEvent(type="audio-chunk", data={"rate": 16000}, payload=payload),
        ])
        event = await WyomingEvent.read_from(reader)
        assert event is not None
        assert event.type == "audio-chunk"
        assert event.data["rate"] == 16000
        assert len(event.payload) == 200

    @pytest.mark.asyncio
    async def test_read_eof_returns_none(self) -> None:
        """EOF returns None."""
        reader = MockStreamReader([])
        event = await WyomingEvent.read_from(reader)
        assert event is None

    @pytest.mark.asyncio
    async def test_read_invalid_json(self) -> None:
        """Invalid JSON header returns None."""
        reader = AsyncMock()
        reader.readline = AsyncMock(return_value=b"not json\n")
        event = await WyomingEvent.read_from(reader)
        assert event is None

    @pytest.mark.asyncio
    async def test_read_connection_error(self) -> None:
        """ConnectionError during read returns None."""
        reader = AsyncMock()
        reader.readline = AsyncMock(side_effect=ConnectionError)
        event = await WyomingEvent.read_from(reader)
        assert event is None


class TestWriteEvent:
    """Test writing events to a stream."""

    @pytest.mark.asyncio
    async def test_write_event(self) -> None:
        """write_event writes and drains."""
        writer = MockStreamWriter()
        event = WyomingEvent(type="transcript", data={"text": "hello"})
        await write_event(writer, event)
        assert len(writer.written) == 1
        header = json.loads(writer.written[0].decode("utf-8").strip())
        assert header["type"] == "transcript"


# ---------------------------------------------------------------------------
# Audio conversion
# ---------------------------------------------------------------------------


class TestAudioConversion:
    """Test PCM ↔ ndarray conversion helpers."""

    def test_pcm_to_ndarray_basic(self) -> None:
        """Convert 16-bit PCM to float32 in [-1, 1]."""
        # 4 samples: -32768, -1, 0, 32767
        pcm = struct.pack("<4h", -32768, -1, 0, 32767)
        arr = pcm_bytes_to_ndarray(pcm)
        assert arr.dtype == np.float32
        assert len(arr) == 4
        assert arr[0] == pytest.approx(-1.0, abs=0.001)
        assert arr[2] == pytest.approx(0.0, abs=0.001)
        assert arr[3] == pytest.approx(32767 / 32768, abs=0.001)

    def test_pcm_to_ndarray_empty(self) -> None:
        """Empty PCM → empty array."""
        arr = pcm_bytes_to_ndarray(b"")
        assert len(arr) == 0

    def test_pcm_to_ndarray_wrong_width(self) -> None:
        """Non-16-bit width raises ValueError."""
        with pytest.raises(ValueError, match="16-bit"):
            pcm_bytes_to_ndarray(b"\x00" * 4, width=4)

    def test_ndarray_to_pcm_int16(self) -> None:
        """Int16 ndarray converts directly to bytes."""
        arr = np.array([0, 1000, -1000, 32767], dtype=np.int16)
        pcm = ndarray_to_pcm_bytes(arr)
        assert len(pcm) == 8  # 4 samples × 2 bytes
        roundtrip = np.frombuffer(pcm, dtype=np.int16)
        np.testing.assert_array_equal(roundtrip, arr)

    def test_ndarray_to_pcm_float32(self) -> None:
        """Float32 ndarray is scaled and clipped to int16."""
        arr = np.array([0.0, 0.5, -0.5, 1.0], dtype=np.float32)
        pcm = ndarray_to_pcm_bytes(arr)
        roundtrip = np.frombuffer(pcm, dtype=np.int16)
        assert roundtrip[0] == 0
        assert roundtrip[1] == pytest.approx(16383, abs=1)
        assert roundtrip[2] == pytest.approx(-16384, abs=1)
        assert roundtrip[3] == pytest.approx(32767, abs=1)

    def test_roundtrip_pcm_ndarray(self) -> None:
        """PCM → ndarray → PCM preserves data (within ±1 LSB from float32 rounding)."""
        original = np.array([0, 100, -100, 32000, -32000], dtype=np.int16)
        pcm = original.tobytes()
        as_float = pcm_bytes_to_ndarray(pcm)
        back_pcm = ndarray_to_pcm_bytes(as_float)
        result = np.frombuffer(back_pcm, dtype=np.int16)
        np.testing.assert_allclose(result, original, atol=1)


# ---------------------------------------------------------------------------
# WyomingConfig
# ---------------------------------------------------------------------------


class TestWyomingConfig:
    """Test configuration dataclass."""

    def test_defaults(self) -> None:
        """Default config has standard Wyoming values."""
        cfg = WyomingConfig()
        assert cfg.port == 10700
        assert cfg.mic_rate == 16000
        assert cfg.mic_width == 2
        assert cfg.mic_channels == 1
        assert cfg.snd_rate == 22050
        assert cfg.snd_width == 2
        assert cfg.snd_channels == 1
        assert cfg.zeroconf_enabled is True
        assert cfg.host == "0.0.0.0"  # noqa: S104

    def test_custom_config(self) -> None:
        """Custom values override defaults."""
        cfg = WyomingConfig(port=8080, name="Test", area="Kitchen")
        assert cfg.port == 8080
        assert cfg.name == "Test"
        assert cfg.area == "Kitchen"

    def test_frozen(self) -> None:
        """Config is immutable."""
        cfg = WyomingConfig()
        with pytest.raises(AttributeError):
            cfg.port = 9999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Service Info
# ---------------------------------------------------------------------------


class TestBuildServiceInfo:
    """Test Wyoming service info builder."""

    def test_contains_all_services(self) -> None:
        """Info includes asr, tts, wake, handle, satellite."""
        info = build_service_info(WyomingConfig())
        assert "asr" in info
        assert "tts" in info
        assert "wake" in info
        assert "handle" in info
        assert "satellite" in info

    def test_asr_details(self) -> None:
        """ASR service has correct name and model."""
        info = build_service_info(WyomingConfig())
        asr = info["asr"][0]
        assert asr["name"] == "sovyx-stt"
        assert asr["installed"] is True
        assert asr["supports_transcript_streaming"] is True
        assert len(asr["models"]) == 1
        assert asr["models"][0]["name"] == "moonshine-tiny"
        assert "en" in asr["models"][0]["languages"]

    def test_tts_details(self) -> None:
        """TTS service has correct voice info."""
        info = build_service_info(WyomingConfig())
        tts = info["tts"][0]
        assert tts["name"] == "sovyx-tts"
        assert tts["supports_synthesize_streaming"] is True
        assert len(tts["voices"]) == 1

    def test_wake_details(self) -> None:
        """Wake word service has correct model."""
        info = build_service_info(WyomingConfig())
        wake = info["wake"][0]
        assert wake["name"] == "sovyx-wake"
        assert wake["models"][0]["name"] == "hey_sovyx"
        assert wake["models"][0]["phrase"] == "hey sovyx"

    def test_handle_details(self) -> None:
        """Handle service references cogloop."""
        info = build_service_info(WyomingConfig())
        handle = info["handle"][0]
        assert handle["name"] == "sovyx-cogloop"
        assert handle["supports_handled_streaming"] is True

    def test_satellite_details(self) -> None:
        """Satellite info includes VAD and wake word support."""
        info = build_service_info(WyomingConfig(name="Test Sat", area="Bedroom"))
        sat = info["satellite"]
        assert sat["name"] == "Test Sat"
        assert sat["area"] == "Bedroom"
        assert sat["has_vad"] is True
        assert "hey_sovyx" in sat["active_wake_words"]
        assert sat["supports_trigger"] is True

    def test_attribution_present(self) -> None:
        """Attribution is set on all services."""
        info = build_service_info(WyomingConfig())
        assert info["asr"][0]["attribution"] == SOVYX_ATTRIBUTION
        assert info["tts"][0]["attribution"] == SOVYX_ATTRIBUTION
        assert info["satellite"]["attribution"] == SOVYX_ATTRIBUTION

    def test_version_propagated(self) -> None:
        """Config version is used in all service descriptions."""
        info = build_service_info(WyomingConfig(version="2.0.0"))
        assert info["asr"][0]["version"] == "2.0.0"
        assert info["tts"][0]["version"] == "2.0.0"
        assert info["satellite"]["version"] == "2.0.0"


# ---------------------------------------------------------------------------
# Client handler — describe
# ---------------------------------------------------------------------------


class TestHandlerDescribe:
    """Test describe event handling."""

    @pytest.mark.asyncio
    async def test_describe_returns_info(self) -> None:
        """describe → info response with all services."""
        handler, reader, writer = _make_handler([
            WyomingEvent(type="describe"),
        ])
        await handler.run()

        events = writer.get_events()
        assert len(events) >= 1
        info_event = events[0]
        assert info_event.type == "info"
        assert "asr" in info_event.data
        assert "tts" in info_event.data
        assert "satellite" in info_event.data


# ---------------------------------------------------------------------------
# Client handler — STT
# ---------------------------------------------------------------------------


class TestHandlerSTT:
    """Test STT (transcribe) event handling."""

    @pytest.mark.asyncio
    async def test_transcribe_flow(self) -> None:
        """transcribe → audio-start → chunks → audio-stop → transcript."""
        stt = AsyncMock()
        stt.transcribe = AsyncMock(return_value=MockTranscriptionResult(text="hello world"))

        pcm = bytes(640)  # 320 samples silence
        handler, reader, writer = _make_handler(
            events=[
                WyomingEvent(type="transcribe", data={"language": "en"}),
                WyomingEvent(type="audio-start", data={"rate": 16000}),
                WyomingEvent(type="audio-chunk", data={"rate": 16000}, payload=pcm),
                WyomingEvent(type="audio-chunk", data={"rate": 16000}, payload=pcm),
                WyomingEvent(type="audio-stop"),
            ],
            stt_engine=stt,
        )
        await handler.run()

        events = writer.get_events()
        transcript_events = [e for e in events if e.type == "transcript"]
        assert len(transcript_events) == 1
        assert transcript_events[0].data["text"] == "hello world"
        assert transcript_events[0].data["language"] == "en"

    @pytest.mark.asyncio
    async def test_transcribe_calls_stt_with_audio(self) -> None:
        """STT engine receives concatenated audio buffer."""
        stt = AsyncMock()
        stt.transcribe = AsyncMock(return_value=MockTranscriptionResult(text="test"))

        pcm = np.zeros(320, dtype=np.int16).tobytes()
        handler, _, writer = _make_handler(
            events=[
                WyomingEvent(type="transcribe", data={}),
                WyomingEvent(type="audio-start"),
                WyomingEvent(type="audio-chunk", payload=pcm),
                WyomingEvent(type="audio-chunk", payload=pcm),
                WyomingEvent(type="audio-stop"),
            ],
            stt_engine=stt,
        )
        await handler.run()

        stt.transcribe.assert_called_once()
        audio_arg = stt.transcribe.call_args[0][0]
        # 2 chunks of 320 samples = 640 float32 samples
        assert len(audio_arg) == 640

    @pytest.mark.asyncio
    async def test_transcribe_no_stt_engine(self) -> None:
        """Without STT engine, return empty transcript."""
        handler, _, writer = _make_handler(
            events=[
                WyomingEvent(type="transcribe", data={"language": "en"}),
            ],
            stt_engine=None,
        )
        await handler.run()

        events = writer.get_events()
        transcript = [e for e in events if e.type == "transcript"]
        assert len(transcript) == 1
        assert transcript[0].data["text"] == ""

    @pytest.mark.asyncio
    async def test_transcribe_audio_start_clears_buffer(self) -> None:
        """audio-start clears previously accumulated audio."""
        stt = AsyncMock()
        stt.transcribe = AsyncMock(return_value=MockTranscriptionResult(text="fresh"))

        pcm_old = np.ones(320, dtype=np.int16).tobytes()
        pcm_new = np.zeros(160, dtype=np.int16).tobytes()
        handler, _, writer = _make_handler(
            events=[
                WyomingEvent(type="transcribe"),
                WyomingEvent(type="audio-chunk", payload=pcm_old),
                WyomingEvent(type="audio-start"),  # Should clear old data
                WyomingEvent(type="audio-chunk", payload=pcm_new),
                WyomingEvent(type="audio-stop"),
            ],
            stt_engine=stt,
        )
        await handler.run()

        audio_arg = stt.transcribe.call_args[0][0]
        assert len(audio_arg) == 160  # Only new chunk


# ---------------------------------------------------------------------------
# Client handler — TTS
# ---------------------------------------------------------------------------


class TestHandlerTTS:
    """Test TTS (synthesize) event handling."""

    @pytest.mark.asyncio
    async def test_synthesize_flow(self) -> None:
        """synthesize → audio-start → audio-chunk(s) → audio-stop."""
        audio_data = np.zeros(22050, dtype=np.int16)  # 1s of silence
        tts = AsyncMock()
        tts.synthesize = AsyncMock(
            return_value=MockAudioChunk(audio=audio_data, sample_rate=22050),
        )

        handler, _, writer = _make_handler(
            events=[
                WyomingEvent(type="synthesize", data={"text": "hello"}),
            ],
            tts_engine=tts,
        )
        await handler.run()

        events = writer.get_events()
        types = [e.type for e in events]
        assert types[0] == "audio-start"
        assert types[-1] == "audio-stop"
        assert all(t == "audio-chunk" for t in types[1:-1])

    @pytest.mark.asyncio
    async def test_synthesize_audio_start_has_format(self) -> None:
        """audio-start contains rate, width, channels."""
        audio_data = np.zeros(100, dtype=np.int16)
        tts = AsyncMock()
        tts.synthesize = AsyncMock(
            return_value=MockAudioChunk(audio=audio_data, sample_rate=22050),
        )

        handler, _, writer = _make_handler(
            events=[WyomingEvent(type="synthesize", data={"text": "hi"})],
            tts_engine=tts,
        )
        await handler.run()

        events = writer.get_events()
        start = events[0]
        assert start.data["rate"] == 22050
        assert start.data["width"] == 2
        assert start.data["channels"] == 1

    @pytest.mark.asyncio
    async def test_synthesize_empty_text(self) -> None:
        """Empty text → audio-stop only (no synthesis)."""
        tts = AsyncMock()
        handler, _, writer = _make_handler(
            events=[WyomingEvent(type="synthesize", data={"text": ""})],
            tts_engine=tts,
        )
        await handler.run()

        events = writer.get_events()
        assert any(e.type == "audio-stop" for e in events)
        tts.synthesize.assert_not_called()

    @pytest.mark.asyncio
    async def test_synthesize_no_tts_engine(self) -> None:
        """Without TTS engine, send audio-stop."""
        handler, _, writer = _make_handler(
            events=[WyomingEvent(type="synthesize", data={"text": "hello"})],
            tts_engine=None,
        )
        await handler.run()

        events = writer.get_events()
        assert any(e.type == "audio-stop" for e in events)

    @pytest.mark.asyncio
    async def test_synthesize_chunks_are_limited_size(self) -> None:
        """Audio is split into chunks of configured size."""
        # Large audio: 2 seconds
        audio_data = np.zeros(44100, dtype=np.int16)
        tts = AsyncMock()
        tts.synthesize = AsyncMock(
            return_value=MockAudioChunk(audio=audio_data, sample_rate=22050),
        )

        cfg = WyomingConfig(output_chunk_ms=100)
        handler, _, writer = _make_handler(
            events=[WyomingEvent(type="synthesize", data={"text": "long text"})],
            tts_engine=tts,
            config=cfg,
        )
        await handler.run()

        events = writer.get_events()
        chunks = [e for e in events if e.type == "audio-chunk"]
        # 2s of audio at 22050Hz, 100ms chunks → ~20 chunks
        assert len(chunks) > 1
        # Each chunk should be ≤ output_chunk_bytes
        expected_chunk_bytes = 22050 * 2 * 1 * 100 // 1000  # 4410
        for chunk in chunks:
            assert len(chunk.payload) <= expected_chunk_bytes


# ---------------------------------------------------------------------------
# Client handler — Wake word
# ---------------------------------------------------------------------------


class TestHandlerWakeWord:
    """Test wake word detection event handling."""

    @pytest.mark.asyncio
    async def test_detect_found(self) -> None:
        """detect → audio chunks → detection event when wake word detected."""
        wake = MagicMock()
        wake.process_frame = MagicMock(
            side_effect=[
                MockWakeResult(detected=False),
                MockWakeResult(detected=True, name="hey_sovyx"),
            ],
        )

        pcm = np.zeros(320, dtype=np.int16).tobytes()
        handler, _, writer = _make_handler(
            events=[
                WyomingEvent(type="detect"),
                WyomingEvent(type="audio-chunk", payload=pcm),
                WyomingEvent(type="audio-chunk", payload=pcm),
            ],
            wake_engine=wake,
        )
        await handler.run()

        events = writer.get_events()
        detections = [e for e in events if e.type == "detection"]
        assert len(detections) == 1
        assert detections[0].data["name"] == "hey_sovyx"

    @pytest.mark.asyncio
    async def test_detect_not_found(self) -> None:
        """detect → audio → audio-stop → not-detected."""
        wake = MagicMock()
        wake.process_frame = MagicMock(return_value=MockWakeResult(detected=False))

        pcm = np.zeros(320, dtype=np.int16).tobytes()
        handler, _, writer = _make_handler(
            events=[
                WyomingEvent(type="detect"),
                WyomingEvent(type="audio-chunk", payload=pcm),
                WyomingEvent(type="audio-stop"),
            ],
            wake_engine=wake,
        )
        await handler.run()

        events = writer.get_events()
        not_detected = [e for e in events if e.type == "not-detected"]
        assert len(not_detected) == 1

    @pytest.mark.asyncio
    async def test_detect_no_wake_engine(self) -> None:
        """Without wake engine, return not-detected."""
        handler, _, writer = _make_handler(
            events=[WyomingEvent(type="detect")],
            wake_engine=None,
        )
        await handler.run()

        events = writer.get_events()
        assert any(e.type == "not-detected" for e in events)


# ---------------------------------------------------------------------------
# Client handler — Intent handling
# ---------------------------------------------------------------------------


class TestHandlerIntent:
    """Test intent handling via CogLoop."""

    @pytest.mark.asyncio
    async def test_intent_handled(self) -> None:
        """transcript → handled response."""
        cogloop = AsyncMock()
        cogloop.generate_response = AsyncMock(return_value="Lights are on")

        handler, _, writer = _make_handler(
            events=[
                WyomingEvent(type="transcript", data={"text": "turn on the lights"}),
            ],
            cogloop=cogloop,
        )
        await handler.run()

        events = writer.get_events()
        handled = [e for e in events if e.type == "handled"]
        assert len(handled) == 1
        assert handled[0].data["text"] == "Lights are on"
        cogloop.generate_response.assert_called_once_with("turn on the lights")

    @pytest.mark.asyncio
    async def test_intent_no_cogloop(self) -> None:
        """Without cogloop, return not-handled."""
        handler, _, writer = _make_handler(
            events=[WyomingEvent(type="transcript", data={"text": "hello"})],
            cogloop=None,
        )
        await handler.run()

        events = writer.get_events()
        assert any(e.type == "not-handled" for e in events)

    @pytest.mark.asyncio
    async def test_intent_empty_text(self) -> None:
        """Empty transcript text → not-handled."""
        cogloop = AsyncMock()
        handler, _, writer = _make_handler(
            events=[WyomingEvent(type="transcript", data={"text": ""})],
            cogloop=cogloop,
        )
        await handler.run()

        events = writer.get_events()
        assert any(e.type == "not-handled" for e in events)
        cogloop.generate_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_intent_exception(self) -> None:
        """CogLoop exception → not-handled."""
        cogloop = AsyncMock()
        cogloop.generate_response = AsyncMock(side_effect=RuntimeError("LLM timeout"))

        handler, _, writer = _make_handler(
            events=[WyomingEvent(type="transcript", data={"text": "hello"})],
            cogloop=cogloop,
        )
        await handler.run()

        events = writer.get_events()
        assert any(e.type == "not-handled" for e in events)


# ---------------------------------------------------------------------------
# Client handler — control events & edge cases
# ---------------------------------------------------------------------------


class TestHandlerEdgeCases:
    """Test control events and edge cases."""

    @pytest.mark.asyncio
    async def test_played_event(self) -> None:
        """played event is accepted without response."""
        handler, _, writer = _make_handler(
            events=[WyomingEvent(type="played")],
        )
        await handler.run()
        # No response expected
        events = writer.get_events()
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_run_satellite_event(self) -> None:
        """run-satellite is accepted silently."""
        handler, _, writer = _make_handler(
            events=[WyomingEvent(type="run-satellite")],
        )
        await handler.run()
        events = writer.get_events()
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_pause_satellite_event(self) -> None:
        """pause-satellite is accepted silently."""
        handler, _, writer = _make_handler(
            events=[WyomingEvent(type="pause-satellite")],
        )
        await handler.run()
        events = writer.get_events()
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_unknown_event(self) -> None:
        """Unknown event types are logged and ignored."""
        handler, _, writer = _make_handler(
            events=[WyomingEvent(type="some-future-event")],
        )
        await handler.run()
        events = writer.get_events()
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_multiple_events_sequential(self) -> None:
        """Handler processes multiple events in sequence."""
        stt = AsyncMock()
        stt.transcribe = AsyncMock(return_value=MockTranscriptionResult(text="hi"))

        pcm = bytes(640)
        handler, _, writer = _make_handler(
            events=[
                WyomingEvent(type="describe"),
                WyomingEvent(type="transcribe"),
                WyomingEvent(type="audio-start"),
                WyomingEvent(type="audio-chunk", payload=pcm),
                WyomingEvent(type="audio-stop"),
            ],
            stt_engine=stt,
        )
        await handler.run()

        events = writer.get_events()
        types = [e.type for e in events]
        assert "info" in types
        assert "transcript" in types

    @pytest.mark.asyncio
    async def test_handler_close(self) -> None:
        """Handler can be closed manually."""
        handler, _, writer = _make_handler()
        assert not handler.closed
        await handler.close()
        assert handler.closed
        assert writer.closed

    @pytest.mark.asyncio
    async def test_double_close(self) -> None:
        """Double close is safe (no-op)."""
        handler, _, _ = _make_handler()
        await handler.close()
        await handler.close()  # Should not raise
        assert handler.closed

    @pytest.mark.asyncio
    async def test_describe_mid_session(self) -> None:
        """describe received after other events still works."""
        stt = AsyncMock()
        stt.transcribe = AsyncMock(return_value=MockTranscriptionResult(text="x"))

        handler, _, writer = _make_handler(
            events=[
                WyomingEvent(type="played"),
                WyomingEvent(type="describe"),
            ],
            stt_engine=stt,
        )
        await handler.run()

        events = writer.get_events()
        assert any(e.type == "info" for e in events)


# ---------------------------------------------------------------------------
# SovyxWyomingServer lifecycle
# ---------------------------------------------------------------------------


class TestSovyxWyomingServer:
    """Test the TCP server lifecycle."""

    def test_defaults(self) -> None:
        """Server initializes with defaults."""
        server = SovyxWyomingServer()
        assert not server.running
        assert server.config.port == 10700
        assert server.active_connections == 0

    def test_custom_config(self) -> None:
        """Server accepts custom config."""
        cfg = WyomingConfig(port=9999, name="Custom")
        server = SovyxWyomingServer(config=cfg)
        assert server.config.port == 9999
        assert server.config.name == "Custom"

    @pytest.mark.asyncio
    async def test_start_stop(self) -> None:
        """Server starts and stops cleanly."""
        cfg = WyomingConfig(port=0, zeroconf_enabled=False)  # port=0 for random
        server = SovyxWyomingServer(config=cfg)

        await server.start()
        assert server.running

        await server.stop()
        assert not server.running

    @pytest.mark.asyncio
    async def test_double_start(self) -> None:
        """Double start is idempotent."""
        cfg = WyomingConfig(port=0, zeroconf_enabled=False)
        server = SovyxWyomingServer(config=cfg)

        await server.start()
        await server.start()  # No-op
        assert server.running

        await server.stop()

    @pytest.mark.asyncio
    async def test_double_stop(self) -> None:
        """Double stop is safe."""
        cfg = WyomingConfig(port=0, zeroconf_enabled=False)
        server = SovyxWyomingServer(config=cfg)

        await server.start()
        await server.stop()
        await server.stop()  # No-op
        assert not server.running

    @pytest.mark.asyncio
    async def test_client_connection(self) -> None:
        """Client connects and receives info on describe."""
        cfg = WyomingConfig(port=0, zeroconf_enabled=False)
        server = SovyxWyomingServer(config=cfg)

        await server.start()
        assert server._server is not None

        # Get the actual port
        port = server._server.sockets[0].getsockname()[1]

        # Connect a client
        reader, writer = await asyncio.open_connection("127.0.0.1", port)

        # Send describe
        describe = WyomingEvent(type="describe")
        writer.write(describe.to_bytes())
        await writer.drain()

        # Read response
        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        assert line
        header = json.loads(line.decode("utf-8").strip())
        assert header["type"] == "info"
        assert "asr" in header.get("data", {})

        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.1)  # Let server process disconnect

        await server.stop()

    @pytest.mark.asyncio
    async def test_multiple_clients(self) -> None:
        """Server handles multiple concurrent clients."""
        cfg = WyomingConfig(port=0, zeroconf_enabled=False)
        server = SovyxWyomingServer(config=cfg)

        await server.start()
        port = server._server.sockets[0].getsockname()[1]

        # Connect 3 clients
        clients = []
        for _ in range(3):
            r, w = await asyncio.open_connection("127.0.0.1", port)
            clients.append((r, w))

        await asyncio.sleep(0.1)
        assert server.active_connections == 3

        # Close all clients
        for _, w in clients:
            w.close()
            await w.wait_closed()

        await asyncio.sleep(0.2)  # Let server clean up
        assert server.active_connections == 0

        await server.stop()


# ---------------------------------------------------------------------------
# Zeroconf
# ---------------------------------------------------------------------------


class TestZeroconf:
    """Test Zeroconf (mDNS) registration."""

    def test_service_type_constant(self) -> None:
        """Wyoming service type is correct."""
        assert WYOMING_SERVICE_TYPE == "_wyoming._tcp.local."

    def test_get_local_ip(self) -> None:
        """get_local_ip returns a valid IP."""
        ip = get_local_ip()
        parts = ip.split(".")
        assert len(parts) == 4
        # Should be valid IP octets
        for part in parts:
            assert 0 <= int(part) <= 255

    def test_get_local_ip_fallback(self) -> None:
        """get_local_ip falls back to 127.0.0.1 on error."""
        with patch("sovyx.voice.wyoming.socket.socket") as mock_socket:
            instance = MagicMock()
            instance.connect.side_effect = OSError("no network")
            mock_socket.return_value = instance
            ip = get_local_ip()
            assert ip == "127.0.0.1"

    @pytest.mark.asyncio
    async def test_zeroconf_not_installed(self) -> None:
        """Server handles missing zeroconf gracefully."""
        cfg = WyomingConfig(port=0, zeroconf_enabled=True)
        server = SovyxWyomingServer(config=cfg)

        with patch.dict("sys.modules", {"zeroconf": None, "zeroconf.asyncio": None}):
            # Should not crash, just log warning
            await server.start()

        await server.stop()


# ---------------------------------------------------------------------------
# Constants validation
# ---------------------------------------------------------------------------


class TestConstants:
    """Test module-level constants."""

    def test_attribution(self) -> None:
        """Attribution has correct values."""
        assert SOVYX_ATTRIBUTION["name"] == "Sovyx"
        assert "sovyx.dev" in SOVYX_ATTRIBUTION["url"]

    def test_input_chunk_bytes(self) -> None:
        """Input chunk size is calculated correctly."""
        from sovyx.voice.wyoming import _INPUT_CHUNK_BYTES

        expected = 16000 * 2 * 1 * 20 // 1000  # 640
        assert expected == _INPUT_CHUNK_BYTES

    def test_output_chunk_bytes(self) -> None:
        """Output chunk size is calculated correctly."""
        from sovyx.voice.wyoming import _OUTPUT_CHUNK_BYTES

        expected = 22050 * 2 * 1 * 100 // 1000  # 4410
        assert expected == _OUTPUT_CHUNK_BYTES


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestProperties:
    """Property-based tests for audio conversion."""

    @pytest.mark.parametrize("n_samples", [0, 1, 160, 320, 640, 16000])
    def test_pcm_roundtrip_sizes(self, n_samples: int) -> None:
        """PCM conversion handles various buffer sizes (±1 LSB from float32)."""
        rng = np.random.default_rng(42)
        original = rng.integers(-32768, 32767, size=n_samples, dtype=np.int16)
        pcm = original.tobytes()
        as_float = pcm_bytes_to_ndarray(pcm)
        assert len(as_float) == n_samples
        back_pcm = ndarray_to_pcm_bytes(as_float)
        result = np.frombuffer(back_pcm, dtype=np.int16)
        np.testing.assert_allclose(result, original, atol=1)

    @pytest.mark.parametrize("rate", [8000, 16000, 22050, 44100, 48000])
    def test_output_chunk_calculation(self, rate: int) -> None:
        """Output chunk size scales with sample rate."""
        cfg = WyomingConfig(snd_rate=rate, output_chunk_ms=100)
        chunk_bytes = rate * cfg.snd_width * cfg.snd_channels * cfg.output_chunk_ms // 1000
        assert chunk_bytes > 0
        assert chunk_bytes == rate * 2 * 100 // 1000
