"""Loopback-only streaming Pocket TTS worker for the dedicated Windows runtime."""

from __future__ import annotations

import argparse
import importlib.metadata
import ipaddress
import json
import logging
import math
import os
import re
import struct
import threading
from collections.abc import Iterator, Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SAMPLE_RATE_HZ = 24_000
CHANNELS = 1
ENCODING = "pcm_s16le"
MAX_REQUEST_BYTES = 4096
MAX_TEXT_CHARACTERS = 1000
MAX_PCM_FRAME_BYTES = 64 * 1024

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
DEFAULT_RUNTIME_ROOT = Path(r"E:\aira-local-runtime\pocket-tts")
DEFAULT_VOICE_STATE = DEFAULT_RUNTIME_ROOT / "aanya_voice.safetensors"
DEFAULT_CACHE_ROOT = DEFAULT_RUNTIME_ROOT / "cache"

VOICE_STATE_ENVIRONMENT = "AIRA_POCKET_TTS_VOICE_STATE"
CACHE_ROOT_ENVIRONMENT = "AIRA_POCKET_TTS_CACHE_ROOT"
OFFLINE_ENVIRONMENT = "AIRA_POCKET_TTS_OFFLINE"

_TURN_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,119}\Z")
_REQUEST_ID = re.compile(r"[0-9a-f]{32}\Z")
_LOGGER = logging.getLogger(__name__)


class WorkerRequestError(RuntimeError):
    def __init__(self, status: HTTPStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class PocketTtsRuntime:
    """Own exactly one model and one cached Aanya state for the process."""

    def __init__(self, voice_state_path: Path, cache_root: Path) -> None:
        self.voice_state_path = voice_state_path
        self.cache_root = cache_root
        self._state_lock = threading.RLock()
        self._generation_lock = threading.Lock()
        self._status = "starting"
        self._message = "Pocket TTS initialization has not started."
        self._model: Any | None = None
        self._voice_state: Any | None = None
        self._pocket_tts_version: str | None = None
        self._active_turn_id: str | None = None
        self._active_request_id: str | None = None
        self._cancel_event: threading.Event | None = None

    def initialize(self) -> None:
        with self._state_lock:
            if self._status != "starting":
                return
            self._status = "loading"
            self._message = "Pocket TTS model and cached Aanya state are loading."
        try:
            voice_state = self.voice_state_path.resolve(strict=True)
            cache_root = self.cache_root.resolve(strict=True)
            if not voice_state.is_file() or voice_state.suffix.casefold() != ".safetensors":
                raise RuntimeError("The configured Aanya voice state is not a safetensors file.")
            if not cache_root.is_dir():
                raise RuntimeError("The configured Pocket TTS cache root is not a directory.")
            os.environ["HF_HOME"] = str(cache_root / "huggingface")
            os.environ["HF_HUB_CACHE"] = str(cache_root / "huggingface" / "hub")
            os.environ["XDG_CACHE_HOME"] = str(cache_root)
            if _environment_flag(OFFLINE_ENVIRONMENT, default=True):
                os.environ["HF_HUB_OFFLINE"] = "1"

            from pocket_tts import TTSModel

            model = TTSModel.load_model()
            if model.sample_rate != SAMPLE_RATE_HZ:
                raise RuntimeError(
                    f"Pocket TTS sample rate {model.sample_rate} is unsupported."
                )
            aanya_state = model.get_state_for_audio_prompt(str(voice_state))
            version = importlib.metadata.version("pocket-tts")
        except Exception as error:
            with self._state_lock:
                self._status = "failed"
                self._message = (
                    f"Pocket TTS initialization failed: {type(error).__name__}: {error}"
                )[:500]
            _LOGGER.exception("Pocket TTS worker initialization failed")
            return

        with self._state_lock:
            self._model = model
            self._voice_state = aanya_state
            self._pocket_tts_version = version
            self._status = "ready"
            self._message = "Pocket TTS model and cached Aanya state are ready."
        _LOGGER.info("Pocket TTS worker ready version=%s", version)

    def snapshot(self) -> dict[str, object]:
        with self._state_lock:
            return {
                "status": self._status,
                "message": self._message,
                "model_loaded": self._model is not None,
                "voice_loaded": self._voice_state is not None,
                "pocket_tts_version": self._pocket_tts_version,
                "sample_rate_hz": SAMPLE_RATE_HZ,
                "channels": CHANNELS,
                "encoding": ENCODING,
                "busy": self._generation_lock.locked(),
            }

    def stream_pcm(
        self, turn_id: str, request_id: str, text: str
    ) -> Iterator[bytes]:
        with self._state_lock:
            if self._status != "ready" or self._model is None or self._voice_state is None:
                raise WorkerRequestError(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "model_not_ready",
                    "Pocket TTS model and Aanya voice state are not ready.",
                )
        if not self._generation_lock.acquire(blocking=False):
            raise WorkerRequestError(
                HTTPStatus.CONFLICT,
                "worker_busy",
                "Pocket TTS is already synthesizing another request.",
            )

        cancel_event = threading.Event()
        with self._state_lock:
            self._active_turn_id = turn_id
            self._active_request_id = request_id
            self._cancel_event = cancel_event
            model = self._model
            voice_state = self._voice_state
        stream = None
        try:
            stream = model.generate_audio_stream(voice_state, text, copy_state=True)
            for tensor in stream:
                if cancel_event.is_set():
                    break
                pcm = _tensor_to_pcm16(tensor)
                for offset in range(0, len(pcm), MAX_PCM_FRAME_BYTES):
                    if cancel_event.is_set():
                        break
                    frame = pcm[offset : offset + MAX_PCM_FRAME_BYTES]
                    if frame:
                        yield frame
                if cancel_event.is_set():
                    break
        finally:
            if stream is not None:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()
            with self._state_lock:
                if self._active_request_id == request_id:
                    self._active_turn_id = None
                    self._active_request_id = None
                    self._cancel_event = None
            self._generation_lock.release()

    def cancel(self, turn_id: str) -> bool:
        with self._state_lock:
            if self._active_turn_id != turn_id or self._cancel_event is None:
                return False
            self._cancel_event.set()
            return True


class PocketTtsWorkerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], runtime: PocketTtsRuntime):
        super().__init__(server_address, PocketTtsRequestHandler)
        self.runtime = runtime


class PocketTtsRequestHandler(BaseHTTPRequestHandler):
    server: PocketTtsWorkerServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "alive"})
            return
        if self.path == "/ready":
            snapshot = self.server.runtime.snapshot()
            status = (
                HTTPStatus.OK
                if snapshot["status"] == "ready"
                else HTTPStatus.SERVICE_UNAVAILABLE
            )
            self._send_json(status, snapshot)
            return
        self._send_error_json(HTTPStatus.NOT_FOUND, "not_found", "Route not found.")

    def do_POST(self) -> None:  # noqa: N802
        try:
            payload = self._read_json_body()
            if self.path == "/v1/cancel":
                turn_id = _required_identifier(payload, "turn_id", _TURN_ID)
                cancelled = self.server.runtime.cancel(turn_id)
                self._send_json(HTTPStatus.OK, {"cancelled": cancelled})
                return
            if self.path == "/v1/synthesize":
                self._synthesize(payload)
                return
            raise WorkerRequestError(
                HTTPStatus.NOT_FOUND, "not_found", "Route not found."
            )
        except WorkerRequestError as error:
            self._send_error_json(error.status, error.code, error.message)
        except Exception:
            _LOGGER.exception("Unexpected Pocket TTS worker request failure")
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "worker_failure",
                "Pocket TTS worker request failed.",
            )

    def _synthesize(self, payload: dict[str, object]) -> None:
        turn_id = _required_identifier(payload, "turn_id", _TURN_ID)
        request_id = _required_identifier(payload, "request_id", _REQUEST_ID)
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise WorkerRequestError(
                HTTPStatus.BAD_REQUEST,
                "invalid_text",
                "Synthesis text must be non-empty.",
            )
        normalized = " ".join(text.split())
        if len(normalized) > MAX_TEXT_CHARACTERS:
            raise WorkerRequestError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "text_too_large",
                "Synthesis text exceeds the worker limit.",
            )

        stream = self.server.runtime.stream_pcm(turn_id, request_id, normalized)
        try:
            first_frame = next(stream)
        except StopIteration:
            raise WorkerRequestError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "empty_audio",
                "Pocket TTS returned no audio.",
            ) from None

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-aira-pcm-stream")
        self.send_header("X-Aira-Pcm-Encoding", ENCODING)
        self.send_header("X-Aira-Sample-Rate", str(SAMPLE_RATE_HZ))
        self.send_header("X-Aira-Channels", str(CHANNELS))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self._write_frame(first_frame)
            for frame in stream:
                self._write_frame(frame)
            self.wfile.write(struct.pack("!I", 0))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            self.server.runtime.cancel(turn_id)
        except Exception:
            self.server.runtime.cancel(turn_id)
            _LOGGER.exception(
                "Pocket TTS generation failed after streaming began turn_id=%s",
                turn_id,
            )
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
            self.close_connection = True

    def _write_frame(self, pcm: bytes) -> None:
        if not pcm or len(pcm) > MAX_PCM_FRAME_BYTES or len(pcm) % 2:
            raise RuntimeError("Pocket TTS produced an invalid PCM16 frame.")
        self.wfile.write(struct.pack("!I", len(pcm)))
        self.wfile.write(pcm)
        self.wfile.flush()

    def _read_json_body(self) -> dict[str, object]:
        if self.headers.get_content_type() != "application/json":
            raise WorkerRequestError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "invalid_content_type",
                "Content-Type must be application/json.",
            )
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise WorkerRequestError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "invalid_body_size",
                "Request body size is invalid.",
            )
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkerRequestError(
                HTTPStatus.BAD_REQUEST, "invalid_json", "Request JSON is invalid."
            ) from error
        if not isinstance(payload, dict):
            raise WorkerRequestError(
                HTTPStatus.BAD_REQUEST,
                "invalid_json",
                "Request JSON must be an object.",
            )
        return payload

    def _send_error_json(
        self, status: HTTPStatus, code: str, message: str
    ) -> None:
        self._send_json(status, {"code": code, "message": message})

    def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        _LOGGER.info(
            "Pocket TTS worker client=%s %s",
            self.client_address[0],
            format % args,
        )


def _required_identifier(
    payload: dict[str, object], key: str, pattern: re.Pattern[str]
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise WorkerRequestError(
            HTTPStatus.BAD_REQUEST,
            f"invalid_{key}",
            f"{key} is invalid.",
        )
    return value


def _tensor_to_pcm16(tensor: Any) -> bytes:
    try:
        import torch

        samples = tensor.detach().to(device="cpu", dtype=torch.float32).flatten()
        if samples.numel() == 0:
            raise ValueError("Audio tensor is empty.")
        samples = torch.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)
        pcm = (samples.clamp(-1.0, 1.0) * 32767.0).round().to(torch.int16)
        rendered = pcm.numpy().astype("<i2", copy=False).tobytes()
    except Exception as error:
        raise RuntimeError("Pocket TTS audio conversion failed.") from error
    if not rendered or len(rendered) % 2:
        raise RuntimeError("Pocket TTS produced invalid PCM16 audio.")
    return rendered


def _environment_flag(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean flag.")


def _loopback_host(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ValueError("Pocket TTS worker host must be a loopback IP address.") from error
    if not address.is_loopback:
        raise ValueError("Pocket TTS worker may bind only to a loopback address.")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--voice-state",
        type=Path,
        default=Path(os.environ.get(VOICE_STATE_ENVIRONMENT, DEFAULT_VOICE_STATE)),
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=Path(os.environ.get(CACHE_ROOT_ENVIRONMENT, DEFAULT_CACHE_ROOT)),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    host = _loopback_host(args.host)
    if not 1 <= args.port <= 65535:
        raise ValueError("Pocket TTS worker port must be between 1 and 65535.")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    runtime = PocketTtsRuntime(args.voice_state, args.cache_root)
    server = PocketTtsWorkerServer((host, args.port), runtime)
    loader = threading.Thread(
        target=runtime.initialize,
        name="pocket-tts-initialize",
        daemon=True,
    )
    loader.start()
    _LOGGER.info("Pocket TTS worker listening on http://%s:%s", host, args.port)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        _LOGGER.info("Pocket TTS worker stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
