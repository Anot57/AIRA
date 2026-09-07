"""Streaming client for the dedicated localhost Pocket TTS worker."""

from __future__ import annotations

import asyncio
import http.client
import json
import math
import os
import re
import struct
import threading
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from .realtime_protocol import MAX_AUDIO_CHUNK_BYTES
from .streaming import RealtimeInferenceUnavailableError, SynthesizedAudioChunk

POCKET_TTS_PROVIDER = "pocket_worker"
POCKET_TTS_PROVIDER_ENVIRONMENT = "AIRA_REALTIME_TTS_PROVIDER"
POCKET_TTS_WORKER_URL_ENVIRONMENT = "AIRA_POCKET_TTS_WORKER_URL"
POCKET_TTS_CONNECT_TIMEOUT_ENVIRONMENT = "AIRA_POCKET_TTS_CONNECT_TIMEOUT_SECONDS"
POCKET_TTS_READ_TIMEOUT_ENVIRONMENT = "AIRA_POCKET_TTS_READ_TIMEOUT_SECONDS"

DEFAULT_POCKET_TTS_WORKER_URL = "http://127.0.0.1:8766"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 2.0
DEFAULT_READ_TIMEOUT_SECONDS = 30.0
POCKET_TTS_SAMPLE_RATE_HZ = 24_000
POCKET_TTS_CHANNELS = 1
POCKET_TTS_ENCODING = "pcm_s16le"

_MAX_ERROR_BODY_BYTES = 4096
_MAX_SYNTHESIS_REQUEST_BYTES = 4096
_TURN_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,119}\Z")


def _positive_seconds(value: object, label: str) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a positive number.") from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{label} must be a positive number.")
    return seconds


def _worker_endpoint(value: object) -> tuple[str, int]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Pocket TTS worker URL must be non-empty.")
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Pocket TTS worker URL must be an HTTP origin.")
    host = parsed.hostname
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Pocket TTS worker must use a loopback host.")
    try:
        port = parsed.port or 80
    except ValueError as error:
        raise ValueError("Pocket TTS worker URL has an invalid port.") from error
    return host, port


@dataclass(frozen=True, slots=True)
class PocketTtsWorkerConfig:
    worker_url: str = DEFAULT_POCKET_TTS_WORKER_URL
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        _worker_endpoint(self.worker_url)
        _positive_seconds(self.connect_timeout_seconds, "Worker connect timeout")
        _positive_seconds(self.read_timeout_seconds, "Worker read timeout")

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> PocketTtsWorkerConfig:
        source = os.environ if environ is None else environ
        return cls(
            worker_url=source.get(
                POCKET_TTS_WORKER_URL_ENVIRONMENT, DEFAULT_POCKET_TTS_WORKER_URL
            ),
            connect_timeout_seconds=_positive_seconds(
                source.get(
                    POCKET_TTS_CONNECT_TIMEOUT_ENVIRONMENT,
                    DEFAULT_CONNECT_TIMEOUT_SECONDS,
                ),
                "Worker connect timeout",
            ),
            read_timeout_seconds=_positive_seconds(
                source.get(
                    POCKET_TTS_READ_TIMEOUT_ENVIRONMENT,
                    DEFAULT_READ_TIMEOUT_SECONDS,
                ),
                "Worker read timeout",
            ),
        )


class PocketTtsWorkerSynthesizer:
    """Forward real worker chunks without buffering a completed utterance."""

    def __init__(self, config: PocketTtsWorkerConfig | None = None) -> None:
        self.config = config or PocketTtsWorkerConfig.from_environment()
        self._host, self._port = _worker_endpoint(self.config.worker_url)
        self._state_lock = threading.RLock()
        self._request_lock = asyncio.Lock()
        self._turn_id: str | None = None
        self._generation = 0
        self._active_connection: http.client.HTTPConnection | None = None
        self._closed = False

    def check_ready(self) -> None:
        payload = self._json_request("GET", "/ready")
        if payload.get("status") != "ready":
            reason = payload.get("message")
            detail = reason if isinstance(reason, str) and reason else "not ready"
            raise RealtimeInferenceUnavailableError(
                f"Pocket TTS worker is not ready: {detail}"
            )
        if (
            payload.get("sample_rate_hz") != POCKET_TTS_SAMPLE_RATE_HZ
            or payload.get("channels") != POCKET_TTS_CHANNELS
            or payload.get("encoding") != POCKET_TTS_ENCODING
        ):
            raise RealtimeInferenceUnavailableError(
                "Pocket TTS worker reported an unsupported audio format."
            )

    async def warmup(self) -> None:
        await asyncio.to_thread(self.check_ready)

    async def start_turn(self, turn_id: str) -> None:
        if not isinstance(turn_id, str) or not _TURN_ID.fullmatch(turn_id):
            raise ValueError("Pocket TTS turn ID is invalid.")
        async with self._request_lock:
            with self._state_lock:
                if self._closed:
                    raise RuntimeError("Pocket TTS synthesizer is closed.")
                self._generation += 1
                self._turn_id = turn_id

    async def synthesize_stream(
        self, text: str, cancel_event: threading.Event
    ) -> AsyncIterator[SynthesizedAudioChunk]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Pocket TTS synthesis text must be non-empty.")
        request_text = text.strip()
        async with self._request_lock:
            with self._state_lock:
                if self._closed:
                    raise RuntimeError("Pocket TTS synthesizer is closed.")
                turn_id = self._turn_id
                generation = self._generation
            if turn_id is None:
                raise RuntimeError("Pocket TTS turn has not been started.")
            if cancel_event.is_set():
                raise asyncio.CancelledError
            request_id = uuid.uuid4().hex
            body = json.dumps(
                {"turn_id": turn_id, "request_id": request_id, "text": request_text},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(body) > _MAX_SYNTHESIS_REQUEST_BYTES:
                raise ValueError("Pocket TTS synthesis request is too large.")

            connection: http.client.HTTPConnection | None = None
            response: http.client.HTTPResponse | None = None
            previous_chunk_at = time.perf_counter()
            try:
                connection, response = await asyncio.to_thread(
                    self._open_stream, body, generation
                )
                self._validate_stream_headers(response)
                while True:
                    pcm = await asyncio.to_thread(self._read_frame, response)
                    chunk_ready_at = time.perf_counter()
                    synthesis_ms = round(
                        max(0.0, chunk_ready_at - previous_chunk_at) * 1000.0, 3
                    )
                    previous_chunk_at = chunk_ready_at
                    if not pcm:
                        break
                    if cancel_event.is_set() or not self._is_current(generation, turn_id):
                        raise asyncio.CancelledError
                    yield SynthesizedAudioChunk(
                        pcm=pcm,
                        sample_rate_hz=POCKET_TTS_SAMPLE_RATE_HZ,
                        channels=POCKET_TTS_CHANNELS,
                        synthesis_ms=synthesis_ms,
                    )
            except asyncio.CancelledError:
                raise
            except (OSError, EOFError, http.client.HTTPException) as error:
                if cancel_event.is_set() or not self._is_current(generation, turn_id):
                    raise asyncio.CancelledError from error
                raise RealtimeInferenceUnavailableError(
                    "Pocket TTS worker stream failed."
                ) from error
            finally:
                if response is not None:
                    response.close()
                if connection is not None:
                    connection.close()
                with self._state_lock:
                    if self._active_connection is connection:
                        self._active_connection = None

    async def cancel(self) -> None:
        with self._state_lock:
            turn_id = self._turn_id
            self._generation += 1
            self._turn_id = None
            connection = self._active_connection
        if connection is not None:
            connection.close()
        if turn_id is not None:
            try:
                await asyncio.to_thread(
                    self._json_request,
                    "POST",
                    "/v1/cancel",
                    {"turn_id": turn_id},
                    self.config.connect_timeout_seconds,
                )
            except RealtimeInferenceUnavailableError:
                # Local invalidation and socket closure already prevent stale audio.
                pass

    async def close(self) -> None:
        await self.cancel()
        with self._state_lock:
            self._closed = True

    def _is_current(self, generation: int, turn_id: str) -> bool:
        with self._state_lock:
            return (
                not self._closed
                and self._generation == generation
                and self._turn_id == turn_id
            )

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self._host, self._port, timeout=timeout)

    def _open_stream(
        self, body: bytes, generation: int
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        connection = self._connection(self.config.read_timeout_seconds)
        with self._state_lock:
            if self._closed or generation != self._generation:
                raise asyncio.CancelledError
            self._active_connection = connection
        try:
            connection.request(
                "POST",
                "/v1/synthesize",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            if response.status != 200:
                detail = response.read(_MAX_ERROR_BODY_BYTES + 1)
                raise RealtimeInferenceUnavailableError(
                    self._response_error(response.status, detail)
                )
            return connection, response
        except BaseException:
            connection.close()
            with self._state_lock:
                if self._active_connection is connection:
                    self._active_connection = None
            raise

    @staticmethod
    def _validate_stream_headers(response: http.client.HTTPResponse) -> None:
        if (
            response.getheader("X-Aira-Pcm-Encoding") != POCKET_TTS_ENCODING
            or response.getheader("X-Aira-Sample-Rate")
            != str(POCKET_TTS_SAMPLE_RATE_HZ)
            or response.getheader("X-Aira-Channels") != str(POCKET_TTS_CHANNELS)
        ):
            raise RealtimeInferenceUnavailableError(
                "Pocket TTS worker returned invalid stream metadata."
            )

    @staticmethod
    def _read_frame(response: http.client.HTTPResponse) -> bytes:
        header = PocketTtsWorkerSynthesizer._read_exact(response, 4)
        length = struct.unpack("!I", header)[0]
        if length == 0:
            return b""
        if length > MAX_AUDIO_CHUNK_BYTES or length % 2:
            raise RealtimeInferenceUnavailableError(
                "Pocket TTS worker returned an invalid PCM frame."
            )
        return PocketTtsWorkerSynthesizer._read_exact(response, length)

    @staticmethod
    def _read_exact(response: http.client.HTTPResponse, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = response.read(size - len(chunks))
            if not chunk:
                raise EOFError("Pocket TTS worker stream ended unexpectedly.")
            chunks.extend(chunk)
        return bytes(chunks)

    def _json_request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        timeout: float | None = None,
    ) -> dict[str, object]:
        body = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        headers = {"Connection": "close"}
        if body is not None:
            headers.update(
                {"Content-Type": "application/json", "Content-Length": str(len(body))}
            )
        connection = self._connection(
            self.config.connect_timeout_seconds if timeout is None else timeout
        )
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(_MAX_ERROR_BODY_BYTES + 1)
            if response.status != 200:
                raise RealtimeInferenceUnavailableError(
                    self._response_error(response.status, raw)
                )
            if len(raw) > _MAX_ERROR_BODY_BYTES:
                raise RealtimeInferenceUnavailableError(
                    "Pocket TTS worker returned an oversized status response."
                )
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("Status response must be an object.")
            return parsed
        except RealtimeInferenceUnavailableError:
            raise
        except (OSError, ValueError, json.JSONDecodeError, http.client.HTTPException) as error:
            raise RealtimeInferenceUnavailableError(
                "Pocket TTS worker is unavailable."
            ) from error
        finally:
            connection.close()

    @staticmethod
    def _response_error(status: int, raw: bytes) -> str:
        message = "Pocket TTS worker request failed."
        if len(raw) <= _MAX_ERROR_BODY_BYTES:
            try:
                payload = json.loads(raw)
                candidate = payload.get("message") if isinstance(payload, dict) else None
                if isinstance(candidate, str) and candidate.strip():
                    message = " ".join(candidate.split())[:200]
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                pass
        return f"Pocket TTS worker returned HTTP {status}: {message}"


def pocket_tts_is_configured(environ: Mapping[str, str] | None = None) -> bool:
    source = os.environ if environ is None else environ
    return (
        source.get(POCKET_TTS_PROVIDER_ENVIRONMENT, "").strip().casefold()
        == POCKET_TTS_PROVIDER
    )
