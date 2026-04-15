"""Wyoming protocol server for Home Assistant voice satellite integration.

Implements the Wyoming wire protocol (JSONL + PCM over TCP) for interoperability
with Home Assistant voice services. Sovyx exposes STT, TTS, Wake Word detection,
and intent handling (via CognitiveLoop) as Wyoming services.

Protocol reference: https://github.com/OHF-Voice/wyoming
HA integration: https://www.home-assistant.io/integrations/wyoming/

Ref: SPE-010 §11, IMPL-SUP-003
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WYOMING_TCP_PORT = 10700
_MIC_RATE = 16_000  # 16 kHz mono PCM input
_MIC_WIDTH = 2  # 16-bit signed LE
_MIC_CHANNELS = 1
_SND_RATE = 22_050  # Piper default output
_SND_WIDTH = 2
_SND_CHANNELS = 1
_INPUT_CHUNK_MS = 20
_INPUT_CHUNK_BYTES = _MIC_RATE * _MIC_WIDTH * _MIC_CHANNELS * _INPUT_CHUNK_MS // 1000  # 640
_OUTPUT_CHUNK_MS = 100
_OUTPUT_CHUNK_BYTES = _SND_RATE * _SND_WIDTH * _SND_CHANNELS * _OUTPUT_CHUNK_MS // 1000  # 4410

SOVYX_ATTRIBUTION = {"name": "Sovyx", "url": "https://sovyx.dev"}
WYOMING_SERVICE_TYPE = "_wyoming._tcp.local."


# ---------------------------------------------------------------------------
# Protocol result types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class STTResult:
    """Result from an STT transcription."""

    text: str
    confidence: float = 0.0


@dataclass(slots=True)
class TTSResult:
    """Result from a TTS synthesis."""

    audio: np.ndarray  # int16 PCM
    sample_rate: int = _SND_RATE


@dataclass(slots=True)
class WakeWordResult:
    """Result from wake word detection on a single frame."""

    detected: bool
    name: str = ""


# ---------------------------------------------------------------------------
# Protocols for engine dependencies (avoids hard coupling)
# ---------------------------------------------------------------------------


class STTEngineProtocol(Protocol):
    """Protocol for STT engines compatible with Wyoming."""

    async def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = _MIC_RATE,
    ) -> STTResult:
        """Transcribe audio to text. Returns object with .text attribute."""
        ...  # pragma: no cover


class TTSEngineProtocol(Protocol):
    """Protocol for TTS engines compatible with Wyoming."""

    async def synthesize(self, text: str) -> TTSResult:
        """Synthesize text to AudioChunk with .audio (int16 ndarray) and .sample_rate."""
        ...  # pragma: no cover


class WakeWordEngineProtocol(Protocol):
    """Protocol for wake word detectors compatible with Wyoming."""

    def process_frame(self, frame: np.ndarray) -> WakeWordResult:
        """Process audio frame. Returns object with .detected attribute."""
        ...  # pragma: no cover


class CogLoopProtocol(Protocol):
    """Protocol for cognitive loop intent handling."""

    async def generate_response(self, text: str) -> str:
        """Generate a text response for the given input."""
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WyomingConfig:
    """Configuration for the Wyoming protocol server.

    Attributes:
        host: TCP bind address. Defaults to ``127.0.0.1`` (loopback only)
            for a safe default. Set to ``0.0.0.0`` **only** in combination
            with ``auth_token`` — the server refuses to start on a
            non-loopback host without a token.
        port: TCP bind port (default 10700, Wyoming standard).
        name: Service name for Zeroconf discovery.
        area: HA area name (e.g., "Living Room").
        auth_token: Optional shared secret. When set, clients must send an
            ``{"type":"auth","data":{"token":"…"}}`` event within
            ``auth_timeout_seconds`` of connecting or be disconnected.
        auth_timeout_seconds: How long a client has to present the token
            before the server closes the connection.
        idle_timeout_seconds: Max idle time between events on an active
            connection. Protects against slow-loris / half-dead peers.
        max_event_payload_bytes: Hard cap on a single Wyoming event
            payload (binary audio + JSON header combined). Defeats
            memory-exhaustion uploads.
        max_events_per_minute: Per-connection event rate limit. Excess
            events are dropped with a warning; ``0`` disables the limiter.
        mic_rate: Input audio sample rate (Hz).
        mic_width: Input audio sample width (bytes).
        mic_channels: Input audio channel count.
        snd_rate: Output audio sample rate (Hz).
        snd_width: Output audio sample width (bytes).
        snd_channels: Output audio channel count.
        zeroconf_enabled: Whether to register with mDNS.
        output_chunk_ms: Output audio chunk duration (ms).
        version: Wyoming protocol version string.
    """

    host: str = "127.0.0.1"
    port: int = _WYOMING_TCP_PORT
    name: str = "Sovyx Voice"
    area: str | None = None
    auth_token: str | None = None
    auth_timeout_seconds: float = 5.0
    idle_timeout_seconds: float = 300.0
    max_event_payload_bytes: int = 32 * 1024 * 1024  # 32 MiB
    max_events_per_minute: int = 600
    mic_rate: int = _MIC_RATE
    mic_width: int = _MIC_WIDTH
    mic_channels: int = _MIC_CHANNELS
    snd_rate: int = _SND_RATE
    snd_width: int = _SND_WIDTH
    snd_channels: int = _SND_CHANNELS
    zeroconf_enabled: bool = True
    output_chunk_ms: int = _OUTPUT_CHUNK_MS
    version: str = "1.0.0"


# ---------------------------------------------------------------------------
# Wyoming wire protocol helpers
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WyomingEvent:
    """A Wyoming protocol event (JSONL header + optional payload).

    Attributes:
        type: Event type string (e.g., "audio-chunk", "describe").
        data: Event-specific data dictionary.
        payload: Optional binary payload (PCM audio).
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    payload: bytes = b""

    def to_bytes(self) -> bytes:
        """Serialize to Wyoming wire format: JSONL header + optional payload."""
        header: dict[str, Any] = {"type": self.type}
        if self.data:
            header["data"] = self.data
        if self.payload:
            header["payload_length"] = len(self.payload)
        line = json.dumps(header, separators=(",", ":")) + "\n"
        return line.encode("utf-8") + self.payload

    @staticmethod
    async def read_from(
        reader: asyncio.StreamReader,
        *,
        max_payload_bytes: int | None = None,
    ) -> WyomingEvent | None:
        """Read a single Wyoming event from a stream.

        Args:
            reader: Stream to read from.
            max_payload_bytes: If set, any event whose declared ``data_length``
                or ``payload_length`` exceeds this value is rejected. Used to
                defeat memory-exhaustion attacks from hostile peers.

        Returns None on EOF, read error, or size-cap violation.
        """
        try:
            line = await reader.readline()
        except (ConnectionError, asyncio.IncompleteReadError):
            return None

        if not line:
            return None

        try:
            header = json.loads(line.decode("utf-8").rstrip("\n"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("wyoming_invalid_header", raw=line[:200])
            return None

        event_type: str = header.get("type", "")
        data: dict[str, Any] = header.get("data", {})

        # Read additional JSON data if present
        data_length = header.get("data_length", 0)
        if max_payload_bytes is not None and data_length > max_payload_bytes:
            logger.warning(
                "wyoming_data_too_large",
                data_length=data_length,
                max_bytes=max_payload_bytes,
                event_type=event_type,
            )
            return None
        if data_length > 0:
            try:
                extra_bytes = await reader.readexactly(data_length)
                extra_data = json.loads(extra_bytes.decode("utf-8"))
                data.update(extra_data)
            except (asyncio.IncompleteReadError, json.JSONDecodeError):
                return None

        # Read binary payload if present
        payload = b""
        payload_length = header.get("payload_length", 0)
        if max_payload_bytes is not None and payload_length > max_payload_bytes:
            logger.warning(
                "wyoming_payload_too_large",
                payload_length=payload_length,
                max_bytes=max_payload_bytes,
                event_type=event_type,
            )
            return None
        if payload_length > 0:
            try:
                payload = await reader.readexactly(payload_length)
            except asyncio.IncompleteReadError:
                return None

        return WyomingEvent(type=event_type, data=data, payload=payload)


async def write_event(writer: asyncio.StreamWriter, event: WyomingEvent) -> None:
    """Write a Wyoming event to a stream."""
    writer.write(event.to_bytes())
    await writer.drain()


# ---------------------------------------------------------------------------
# Service Info builder
# ---------------------------------------------------------------------------


def build_service_info(config: WyomingConfig) -> dict[str, Any]:
    """Build the Wyoming ``info`` response describing Sovyx capabilities.

    Returns the ``data`` dict for an ``info`` event.
    """
    return {
        "asr": [
            {
                "name": "sovyx-stt",
                "attribution": SOVYX_ATTRIBUTION,
                "installed": True,
                "description": "Sovyx STT (Moonshine)",
                "version": config.version,
                "models": [
                    {
                        "name": "moonshine-tiny",
                        "attribution": SOVYX_ATTRIBUTION,
                        "installed": True,
                        "description": "Moonshine Tiny ONNX",
                        "version": config.version,
                        "languages": ["en"],
                    },
                ],
                "supports_transcript_streaming": True,
            },
        ],
        "tts": [
            {
                "name": "sovyx-tts",
                "attribution": SOVYX_ATTRIBUTION,
                "installed": True,
                "description": "Sovyx TTS (Piper/Kokoro)",
                "version": config.version,
                "voices": [
                    {
                        "name": "default",
                        "attribution": SOVYX_ATTRIBUTION,
                        "installed": True,
                        "description": "Default Sovyx voice",
                        "version": config.version,
                        "languages": ["en"],
                    },
                ],
                "supports_synthesize_streaming": True,
            },
        ],
        "wake": [
            {
                "name": "sovyx-wake",
                "attribution": SOVYX_ATTRIBUTION,
                "installed": True,
                "description": "OpenWakeWord",
                "version": config.version,
                "models": [
                    {
                        "name": "hey_sovyx",
                        "attribution": SOVYX_ATTRIBUTION,
                        "installed": True,
                        "description": "Sovyx wake word",
                        "version": config.version,
                        "languages": ["en"],
                        "phrase": "hey sovyx",
                    },
                ],
            },
        ],
        "handle": [
            {
                "name": "sovyx-cogloop",
                "attribution": SOVYX_ATTRIBUTION,
                "installed": True,
                "description": "Sovyx Cognitive Loop",
                "version": config.version,
                "models": [
                    {
                        "name": "cogloop",
                        "attribution": SOVYX_ATTRIBUTION,
                        "installed": True,
                        "description": "AI Companion",
                        "version": config.version,
                        "languages": ["en"],
                    },
                ],
                "supports_handled_streaming": True,
            },
        ],
        "satellite": {
            "name": config.name,
            "attribution": SOVYX_ATTRIBUTION,
            "installed": True,
            "description": "Sovyx Voice Satellite",
            "version": config.version,
            "area": config.area,
            "has_vad": True,
            "active_wake_words": ["hey_sovyx"],
            "max_active_wake_words": 2,
            "supports_trigger": True,
        },
    }


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------


def pcm_bytes_to_ndarray(pcm: bytes, width: int = _MIC_WIDTH) -> np.ndarray:
    """Convert raw PCM bytes (S16_LE) to float32 ndarray normalized to [-1, 1].

    Args:
        pcm: Raw PCM bytes.
        width: Sample width in bytes (default 2 for 16-bit).

    Returns:
        Float32 numpy array.
    """
    if width != 2:  # noqa: PLR2004 — only 16-bit supported
        msg = f"Only 16-bit PCM is supported, got width={width}"
        raise ValueError(msg)
    samples = np.frombuffer(pcm, dtype=np.int16)
    return samples.astype(np.float32) / 32768.0


def ndarray_to_pcm_bytes(audio: np.ndarray) -> bytes:
    """Convert int16 ndarray to raw PCM bytes (S16_LE).

    Args:
        audio: Int16 numpy array.

    Returns:
        Raw PCM bytes.
    """
    if audio.dtype != np.int16:
        # Assume float32 in [-1, 1]
        audio = (audio * 32767).clip(-32768, 32767).astype(np.int16)
    return audio.tobytes()


# ---------------------------------------------------------------------------
# Client handler
# ---------------------------------------------------------------------------


class WyomingClientHandler:
    """Handles a single Wyoming client connection.

    Routes incoming events to STT, TTS, wake word, or intent handlers.
    Each client connection gets its own handler instance.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        config: WyomingConfig,
        stt_engine: STTEngineProtocol | None = None,
        tts_engine: TTSEngineProtocol | None = None,
        wake_engine: WakeWordEngineProtocol | None = None,
        cogloop: CogLoopProtocol | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._config = config
        self._stt = stt_engine
        self._tts = tts_engine
        self._wake = wake_engine
        self._cogloop = cogloop
        self._closed = False

    @property
    def closed(self) -> bool:
        """Whether the handler has been closed."""
        return self._closed

    async def run(self) -> None:
        """Main event loop — read and dispatch events until disconnect.

        Enforces per-event payload caps and an idle timeout between
        consecutive events (``WyomingConfig.idle_timeout_seconds``) and a
        simple sliding-window event rate limit
        (``WyomingConfig.max_events_per_minute``).
        """
        max_payload = self._config.max_event_payload_bytes
        idle_timeout = self._config.idle_timeout_seconds
        # Sliding-window counter: (window_start_monotonic, count_in_window).
        window_start = 0.0
        window_count = 0
        rate_limit = self._config.max_events_per_minute
        try:
            while not self._closed:
                try:
                    event = await asyncio.wait_for(
                        WyomingEvent.read_from(self._reader, max_payload_bytes=max_payload),
                        timeout=idle_timeout,
                    )
                except TimeoutError:
                    logger.info("wyoming_idle_timeout")
                    break
                if event is None:
                    break

                # Per-connection rate limit (simple sliding 60 s window).
                if rate_limit > 0:
                    now = asyncio.get_event_loop().time()
                    if now - window_start >= 60.0:
                        window_start = now
                        window_count = 0
                    window_count += 1
                    if window_count > rate_limit:
                        logger.warning(
                            "wyoming_rate_limit_exceeded",
                            limit=rate_limit,
                            event_type=event.type,
                        )
                        break

                await self._dispatch(event)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            await self.close()

    async def close(self) -> None:
        """Close the connection."""
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    async def _dispatch(self, event: WyomingEvent) -> None:
        """Route an incoming event to the appropriate handler."""
        etype = event.type

        if etype == "describe":
            await self._handle_describe()
        elif etype == "transcribe":
            await self._handle_transcribe(event)
        elif etype == "synthesize":
            await self._handle_synthesize(event)
        elif etype == "detect":
            await self._handle_detect(event)
        elif etype == "transcript":
            # HA sends transcript for intent handling
            await self._handle_intent(event)
        elif etype in {"played", "run-satellite", "pause-satellite"}:
            logger.debug("wyoming_control_event", event_type=etype)
        elif etype in {"audio-start", "audio-chunk", "audio-stop"}:
            # Audio events handled within stream contexts
            pass
        else:
            logger.warning("wyoming_unhandled_event", event_type=etype)

    async def _write_event(self, event: WyomingEvent) -> None:
        """Write an event to the client."""
        if not self._closed:
            await write_event(self._writer, event)

    async def _handle_describe(self) -> None:
        """Respond to ``describe`` with service info."""
        info = build_service_info(self._config)
        await self._write_event(WyomingEvent(type="info", data=info))

    async def _handle_transcribe(self, event: WyomingEvent) -> None:
        """Handle STT: collect audio chunks → transcribe → return transcript."""
        if self._stt is None:
            logger.warning("wyoming_stt_not_available")
            await self._write_event(
                WyomingEvent(type="transcript", data={"text": ""}),
            )
            return

        language = event.data.get("language", "en")
        audio_buffer = bytearray()

        # Read audio stream until audio-stop
        while True:
            chunk_event = await WyomingEvent.read_from(self._reader)
            if chunk_event is None:
                return  # Client disconnected

            if chunk_event.type == "audio-start":
                audio_buffer.clear()
            elif chunk_event.type == "audio-chunk":
                audio_buffer.extend(chunk_event.payload)
            elif chunk_event.type == "audio-stop":
                break

        # Convert PCM to float32 ndarray
        audio_np = pcm_bytes_to_ndarray(bytes(audio_buffer), self._config.mic_width)

        # Transcribe
        result = await self._stt.transcribe(audio_np, self._config.mic_rate)

        # Extract text (handle both string and object results)
        text = result.text if hasattr(result, "text") else str(result)

        await self._write_event(
            WyomingEvent(type="transcript", data={"text": text, "language": language}),
        )

    async def _handle_synthesize(self, event: WyomingEvent) -> None:
        """Handle TTS: synthesize text → stream audio back."""
        if self._tts is None:
            logger.warning("wyoming_tts_not_available")
            await self._write_event(WyomingEvent(type="audio-stop"))
            return

        text = event.data.get("text", "")
        if not text:
            await self._write_event(WyomingEvent(type="audio-stop"))
            return

        # Synthesize
        chunk = await self._tts.synthesize(text)

        # Extract audio bytes from result
        audio_bytes = ndarray_to_pcm_bytes(chunk.audio)
        sample_rate: int = chunk.sample_rate

        # Stream audio-start → audio-chunk(s) → audio-stop
        await self._write_event(
            WyomingEvent(
                type="audio-start",
                data={
                    "rate": sample_rate,
                    "width": self._config.snd_width,
                    "channels": self._config.snd_channels,
                },
            )
        )

        # Send in chunks
        chunk_size = (
            sample_rate
            * self._config.snd_width
            * self._config.snd_channels
            * self._config.output_chunk_ms
            // 1000
        )
        for i in range(0, len(audio_bytes), chunk_size):
            chunk_data = audio_bytes[i : i + chunk_size]
            await self._write_event(
                WyomingEvent(
                    type="audio-chunk",
                    data={
                        "rate": sample_rate,
                        "width": self._config.snd_width,
                        "channels": self._config.snd_channels,
                    },
                    payload=chunk_data,
                )
            )

        await self._write_event(WyomingEvent(type="audio-stop"))

    async def _handle_detect(self, event: WyomingEvent) -> None:
        """Handle wake word detection: receive audio → detect wake word."""
        if self._wake is None:
            logger.warning("wyoming_wake_not_available")
            await self._write_event(WyomingEvent(type="not-detected"))
            return

        while True:
            chunk_event = await WyomingEvent.read_from(self._reader)
            if chunk_event is None:
                return

            if chunk_event.type == "audio-chunk":
                audio_np = pcm_bytes_to_ndarray(
                    chunk_event.payload,
                    self._config.mic_width,
                )
                # Wake word ONNX inference is CPU-bound — offload so the
                # Wyoming server keeps reading the next audio chunk in
                # parallel rather than serializing on the model.
                result = await asyncio.to_thread(self._wake.process_frame, audio_np)
                if hasattr(result, "detected") and result.detected:
                    name = getattr(result, "name", "hey_sovyx")
                    await self._write_event(
                        WyomingEvent(
                            type="detection",
                            data={"name": name, "timestamp": None},
                        )
                    )
                    return
            elif chunk_event.type == "audio-stop":
                await self._write_event(WyomingEvent(type="not-detected"))
                return

    async def _handle_intent(self, event: WyomingEvent) -> None:
        """Handle intent via CogLoop: transcript → response."""
        text = event.data.get("text", "")
        if not text or self._cogloop is None:
            await self._write_event(
                WyomingEvent(
                    type="not-handled",
                    data={"text": text},
                )
            )
            return

        try:
            response = await self._cogloop.generate_response(text)

            # Send handled with full response
            await self._write_event(
                WyomingEvent(
                    type="handled",
                    data={"text": response},
                )
            )
        except Exception:
            logger.exception("wyoming_intent_handling_failed")
            await self._write_event(
                WyomingEvent(
                    type="not-handled",
                    data={"text": text},
                )
            )


# ---------------------------------------------------------------------------
# TCP Server
# ---------------------------------------------------------------------------


class SovyxWyomingServer:
    """Wyoming TCP server with optional Zeroconf (mDNS) discovery.

    Starts a TCP server and routes incoming connections to
    ``WyomingClientHandler`` instances. Optionally registers
    with Zeroconf so Home Assistant auto-discovers Sovyx.

    Usage::

        server = SovyxWyomingServer(
            config=WyomingConfig(),
            stt_engine=my_stt,
            tts_engine=my_tts,
            wake_engine=my_wake,
            cogloop=my_cogloop,
        )
        await server.start()
        # ... server runs ...
        await server.stop()
    """

    def __init__(
        self,
        config: WyomingConfig | None = None,
        stt_engine: STTEngineProtocol | None = None,
        tts_engine: TTSEngineProtocol | None = None,
        wake_engine: WakeWordEngineProtocol | None = None,
        cogloop: CogLoopProtocol | None = None,
    ) -> None:
        self._config = config or WyomingConfig()
        self._stt = stt_engine
        self._tts = tts_engine
        self._wake = wake_engine
        self._cogloop = cogloop
        self._server: asyncio.Server | None = None
        self._zeroconf: Any = None
        self._handlers: list[WyomingClientHandler] = []
        self._running = False

    @property
    def running(self) -> bool:
        """Whether the server is currently running."""
        return self._running

    @property
    def config(self) -> WyomingConfig:
        """Server configuration."""
        return self._config

    @property
    def active_connections(self) -> int:
        """Number of currently active client connections."""
        return len([h for h in self._handlers if not h.closed])

    async def start(self) -> None:
        """Start the TCP server and optional Zeroconf registration.

        Refuses to bind on a non-loopback interface when ``auth_token`` is
        unset — prevents unauthenticated LAN exposure of the cognitive
        pipeline (which bills LLM cost to the Sovyx owner).
        """
        if self._running:
            return

        # Enforce secure default: non-loopback bind requires a token.
        loopback_hosts = {"127.0.0.1", "::1", "localhost"}
        if self._config.host not in loopback_hosts and not self._config.auth_token:
            msg = (
                f"Wyoming server refuses to bind on {self._config.host!r} "
                "without an auth_token — would expose the cognitive pipeline "
                "unauthenticated. Set WyomingConfig.auth_token or bind to "
                "127.0.0.1."
            )
            raise RuntimeError(msg)

        self._running = True

        # Register with Zeroconf before accepting connections
        if self._config.zeroconf_enabled:
            await self._register_zeroconf()

        self._server = await asyncio.start_server(
            self._handle_connection,
            self._config.host,
            self._config.port,
        )

        logger.info(
            "wyoming_server_started",
            host=self._config.host,
            port=self._config.port,
            name=self._config.name,
            auth_required=bool(self._config.auth_token),
        )

    async def stop(self) -> None:
        """Stop the server and clean up resources."""
        if not self._running:
            return

        self._running = False

        # Close all active handlers
        for handler in self._handlers:
            await handler.close()
        self._handlers.clear()

        # Close TCP server
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        # Unregister Zeroconf
        if self._zeroconf is not None:
            try:
                await self._unregister_zeroconf()
            except Exception:  # noqa: BLE001 — shutdown cleanup — zeroconf unregister best-effort
                logger.warning("wyoming_zeroconf_unregister_failed", exc_info=True)
            self._zeroconf = None

        logger.info("wyoming_server_stopped")

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a new incoming TCP connection.

        If ``auth_token`` is configured, the client must send an
        ``{"type": "auth", "data": {"token": "…"}}`` event within
        ``auth_timeout_seconds`` before any other traffic is accepted.
        """
        peer = writer.get_extra_info("peername", ("unknown", 0))
        logger.info("wyoming_client_connected", peer=str(peer))

        # Auth handshake (only when a token is configured).
        if self._config.auth_token:
            try:
                authed = await asyncio.wait_for(
                    self._authenticate(reader, writer),
                    timeout=self._config.auth_timeout_seconds,
                )
            except TimeoutError:
                logger.warning("wyoming_auth_timeout", peer=str(peer))
                authed = False
            if not authed:
                logger.warning("wyoming_auth_failed", peer=str(peer))
                with contextlib.suppress(ConnectionError, OSError):
                    writer.close()
                    await writer.wait_closed()
                return

        handler = WyomingClientHandler(
            reader=reader,
            writer=writer,
            config=self._config,
            stt_engine=self._stt,
            tts_engine=self._tts,
            wake_engine=self._wake,
            cogloop=self._cogloop,
        )
        self._handlers.append(handler)

        try:
            await handler.run()
        finally:
            if handler in self._handlers:
                self._handlers.remove(handler)
            logger.info("wyoming_client_disconnected", peer=str(peer))

    async def _authenticate(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> bool:
        """Read one event and verify it is ``auth`` with the right token.

        Uses constant-time comparison to avoid timing side-channels.
        """
        import hmac

        expected = self._config.auth_token
        if not expected:
            return True  # Should not reach here, but fail open on config drift.

        event = await WyomingEvent.read_from(
            reader,
            max_payload_bytes=self._config.max_event_payload_bytes,
        )
        if event is None or event.type != "auth":
            return False
        provided = event.data.get("token", "")
        if not isinstance(provided, str):
            return False
        if not hmac.compare_digest(provided, expected):
            return False
        # ACK — Wyoming doesn't standardize this; we send an info-like event
        # so compatible clients can detect success deterministically.
        await write_event(writer, WyomingEvent(type="auth-ok"))
        return True

    async def _register_zeroconf(self) -> None:
        """Register Sovyx as a Wyoming service via mDNS."""
        try:
            from zeroconf import ServiceInfo
            from zeroconf.asyncio import AsyncZeroconf
        except ImportError:
            logger.warning("wyoming_zeroconf_unavailable", reason="zeroconf not installed")
            return

        local_ip = get_local_ip()

        service_info = ServiceInfo(
            type_=WYOMING_SERVICE_TYPE,
            name=f"{self._config.name}.{WYOMING_SERVICE_TYPE}",
            addresses=[socket.inet_aton(local_ip)],
            port=self._config.port,
            properties={
                "name": self._config.name,
                "area": self._config.area or "",
                "version": self._config.version,
            },
        )

        self._zeroconf = AsyncZeroconf()
        await self._zeroconf.async_register_service(service_info)
        logger.info("wyoming_zeroconf_registered", name=self._config.name, ip=local_ip)

    async def _unregister_zeroconf(self) -> None:
        """Unregister from Zeroconf."""
        if self._zeroconf is not None:
            await self._zeroconf.async_unregister_all_services()
            await self._zeroconf.async_close()


def get_local_ip() -> str:
    """Get the local network IP address for Zeroconf registration.

    Falls back to ``127.0.0.1`` if unable to determine.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # noqa: S104 — UDP, no actual connection
        result: str = s.getsockname()[0]
        return result
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
