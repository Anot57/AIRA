"""Versioned JSON metadata and binary-audio rules for Aira realtime voice."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

PROTOCOL_VERSION = 1
SUPPORTED_REALTIME_COMPANION = "aanya"
AI_DISCLOSURE = "Aanya is an adult fictional AI companion, not a human."
MAX_TEXT_MESSAGE_BYTES = 16 * 1024
MAX_AUDIO_CHUNK_BYTES = 64 * 1024
MAX_TURN_AUDIO_BYTES = 2 * 1024 * 1024
MAX_TRANSCRIPT_TEXT_CHARACTERS = 10_000
MAX_RESPONSE_TEXT_CHARACTERS = 10_000
MAX_TEXT_DELTA_CHARACTERS = 4_096
PCM_ENCODING = "pcm_s16le"
PCM_SAMPLE_RATE_HZ = 16_000
PCM_CHANNELS = 1
MIN_PLAYBACK_SAMPLE_RATE_HZ = 8_000
MAX_PLAYBACK_SAMPLE_RATE_HZ = 48_000


class ProtocolError(ValueError):
    """A client-safe realtime protocol validation error."""

    def __init__(self, code: str, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fatal = fatal


class ClientEventType(StrEnum):
    SESSION_START = "session_start"
    AUDIO_CHUNK = "audio_chunk"
    END_OF_TURN = "end_of_turn"
    CANCEL_TURN = "cancel_turn"
    SESSION_END = "session_end"


class ServerEventType(StrEnum):
    SESSION_READY = "session_ready"
    STT_PARTIAL = "stt_partial"
    STT_FINAL = "stt_final"
    THINKING = "thinking"
    SAFETY_ESCALATION = "safety_escalation"
    TEXT_DELTA = "text_delta"
    TEXT_SENTENCE = "text_sentence"
    AUDIO_CHUNK = "audio_chunk"
    SPEAKING = "speaking"
    TURN_COMPLETE = "turn_complete"
    RECOVERABLE_ERROR = "recoverable_error"
    FATAL_ERROR = "fatal_error"


@dataclass(frozen=True, slots=True)
class PcmAudioFormat:
    encoding: str = PCM_ENCODING
    sample_rate_hz: int = PCM_SAMPLE_RATE_HZ
    channels: int = PCM_CHANNELS

    @classmethod
    def parse(cls, value: object) -> PcmAudioFormat:
        if not isinstance(value, dict):
            raise ProtocolError(
                "invalid_audio_format", "audio_format must be a JSON object."
            )
        expected = {"encoding", "sample_rate_hz", "channels"}
        if set(value) != expected:
            raise ProtocolError(
                "invalid_audio_format",
                "audio_format must specify encoding, sample_rate_hz, and channels.",
            )
        encoding = value.get("encoding")
        sample_rate_hz = value.get("sample_rate_hz")
        channels = value.get("channels")
        if (
            not isinstance(encoding, str)
            or type(sample_rate_hz) is not int
            or type(channels) is not int
        ):
            raise ProtocolError(
                "invalid_audio_format",
                "Audio encoding must be text and sample rate/channels must be integers.",
            )
        result = cls(
            encoding=encoding,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
        )
        if result != cls():
            raise ProtocolError(
                "unsupported_audio_format",
                "Realtime audio must be PCM signed 16-bit little-endian, mono, 16 kHz.",
            )
        return result

    def payload(self) -> dict[str, str | int]:
        return {
            "encoding": self.encoding,
            "sample_rate_hz": self.sample_rate_hz,
            "channels": self.channels,
        }


@dataclass(frozen=True, slots=True)
class ClientEvent:
    type: ClientEventType
    protocol_version: int


@dataclass(frozen=True, slots=True)
class SessionStartEvent(ClientEvent):
    companion: str
    audio_format: PcmAudioFormat


def parse_client_event(raw: str | bytes) -> ClientEvent:
    """Parse one bounded client JSON event; audio bytes use binary frames."""

    if isinstance(raw, bytes):
        if len(raw) > MAX_TEXT_MESSAGE_BYTES:
            raise ProtocolError("message_too_large", "JSON message is too large.")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ProtocolError("invalid_json", "JSON message must be UTF-8.") from error
    if not isinstance(raw, str):
        raise ProtocolError("invalid_message", "Expected a JSON text message.")
    if len(raw.encode("utf-8")) > MAX_TEXT_MESSAGE_BYTES:
        raise ProtocolError("message_too_large", "JSON message is too large.")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as error:
        raise ProtocolError("invalid_json", "Message must contain valid JSON.") from error
    if not isinstance(payload, dict):
        raise ProtocolError("invalid_message", "Message must be a JSON object.")

    version = payload.get("protocol_version")
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise ProtocolError(
            "unsupported_protocol_version",
            f"Only realtime protocol version {PROTOCOL_VERSION} is supported.",
            fatal=True,
        )
    raw_type = payload.get("type")
    try:
        event_type = ClientEventType(raw_type)
    except (TypeError, ValueError) as error:
        raise ProtocolError("unknown_event", "Unsupported realtime event type.") from error

    if event_type is ClientEventType.AUDIO_CHUNK:
        raise ProtocolError(
            "binary_audio_required",
            "Send PCM audio_chunk data as a binary WebSocket frame.",
        )
    if event_type is ClientEventType.SESSION_START:
        allowed = {"type", "protocol_version", "companion", "audio_format"}
        if set(payload) != allowed:
            raise ProtocolError(
                "invalid_session_start", "session_start contains unsupported fields."
            )
        companion = payload.get("companion")
        if companion != SUPPORTED_REALTIME_COMPANION:
            raise ProtocolError(
                "unsupported_companion",
                "Realtime voice currently supports only companion=aanya.",
                fatal=True,
            )
        return SessionStartEvent(
            type=event_type,
            protocol_version=version,
            companion=companion,
            audio_format=PcmAudioFormat.parse(payload.get("audio_format")),
        )

    allowed = {"type", "protocol_version"}
    if set(payload) != allowed:
        raise ProtocolError(
            "invalid_event", f"{event_type.value} contains unsupported fields."
        )
    return ClientEvent(type=event_type, protocol_version=version)


def server_event(event_type: ServerEventType, **fields: object) -> dict[str, object]:
    return {
        "type": event_type.value,
        "protocol_version": PROTOCOL_VERSION,
        **fields,
    }


def error_event(error: ProtocolError) -> dict[str, object]:
    return server_event(
        ServerEventType.FATAL_ERROR
        if error.fatal
        else ServerEventType.RECOVERABLE_ERROR,
        code=error.code,
        message=error.message,
    )
