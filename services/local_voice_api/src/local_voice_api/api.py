"""Trusted-local-network HTTP bridge for the existing conversation pipeline."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import threading
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHttpException
from starlette.formparsers import MultiPartException
from starlette.types import ASGIApp, Receive, Scope, Send

from .conversation import (
    DEFAULT_RUNTIME_ROOT,
    ConversationTurnResult,
    run_conversation_turn,
)
from .synthesis import (
    MAX_SYNTHESIS_TEXT_CHARACTERS,
    shutdown_voice_clone_runtime,
)
from .transcription import (
    MAX_INPUT_BYTES,
    SUPPORTED_AUDIO_EXTENSIONS,
    is_e_drive_path,
    shutdown_transcription_runtime,
)

SERVICE_NAME = "aira-local-voice-api"
SUPPORTED_COMPANION_ID = "aanya"
DEFAULT_INCOMING_DIR = DEFAULT_RUNTIME_ROOT / "incoming"
MAX_UPLOAD_BYTES = MAX_INPUT_BYTES
UPLOAD_CHUNK_BYTES = 1024 * 1024
MAX_MULTIPART_OVERHEAD_BYTES = 64 * 1024
MAX_FORM_FIELD_BYTES = 4 * 1024

_LOGGER = logging.getLogger(__name__)
_SAFE_TURN_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,119}\Z")
_UPLOAD_BASENAME = re.compile(r"upload_[0-9a-f]{32}\.[a-z0-9]+\Z")
_DEFAULT_SHUTDOWN = object()
_TURN_PATH = "/v1/conversation/turn"
_REQUEST_TOO_LARGE_DETAIL = (
    "The multipart request exceeds the configured local upload limit."
)

_ALLOWED_MEDIA_TYPES = {
    ".wav": frozenset(
        {"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"}
    ),
    ".mp3": frozenset({"audio/mpeg", "audio/mp3"}),
    ".m4a": frozenset({"audio/mp4", "audio/x-m4a", "audio/m4a"}),
    ".ogg": frozenset({"audio/ogg", "application/ogg"}),
    ".webm": frozenset({"audio/webm", "video/webm"}),
}


class UploadStream(Protocol):
    """Minimal interface shared by FastAPI UploadFile and unit-test uploads."""

    filename: str | None
    content_type: str | None

    async def read(self, size: int = -1) -> bytes: ...

    async def close(self) -> None: ...


class ApiRequestError(RuntimeError):
    """An HTTP-safe API error that never embeds internal runtime details."""

    def __init__(self, status_code: int, error_code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.error_code = error_code
        self.detail = detail

    def payload(self) -> dict[str, str]:
        return {"error": self.error_code, "detail": self.detail}


class _RequestBodyTooLarge(MultiPartException):
    """Abort multipart parsing while allowing Starlette to close spool files."""


class ConversationRequestGuardMiddleware:
    """Bound and admit turn requests before multipart parsing starts."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self._app = app
        self._max_body_bytes = max_body_bytes
        self._state_lock = threading.Lock()
        self._turn_request_active = False

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method", "").upper() != "POST"
            or scope.get("path") != _TURN_PATH
        ):
            await self._app(scope, receive, send)
            return

        with self._state_lock:
            admitted = not self._turn_request_active
            if admitted:
                self._turn_request_active = True
        if not admitted:
            error = ApiRequestError(
                503,
                "service_busy",
                "Another local conversation turn is already in progress.",
            )
            await JSONResponse(
                status_code=error.status_code,
                content=error.payload(),
            )(scope, receive, send)
            return

        declared_length: int | None = None
        for name, value in scope.get("headers", ()):  # ASGI headers are bytes.
            if name.lower() != b"content-length":
                continue
            try:
                declared_length = int(value)
            except (TypeError, ValueError):
                declared_length = None
            break
        if (
            declared_length is not None
            and declared_length > self._max_body_bytes
        ):
            error = _invalid_audio(_REQUEST_TOO_LARGE_DETAIL)
            try:
                await JSONResponse(
                    status_code=error.status_code,
                    content=error.payload(),
                )(scope, receive, send)
            finally:
                with self._state_lock:
                    self._turn_request_active = False
            return

        received_bytes = 0
        response_started = False

        async def bounded_receive():
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self._max_body_bytes:
                    raise _RequestBodyTooLarge(_REQUEST_TOO_LARGE_DETAIL)
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self._app(scope, bounded_receive, tracked_send)
        except _RequestBodyTooLarge:
            if response_started:
                raise
            error = _invalid_audio(_REQUEST_TOO_LARGE_DETAIL)
            await JSONResponse(
                status_code=error.status_code,
                content=error.payload(),
            )(scope, receive, send)
        finally:
            with self._state_lock:
                self._turn_request_active = False


@dataclass(frozen=True, slots=True)
class TurnAudioRecord:
    """A validated process-local mapping from a safe ID to one generated WAV."""

    turn_id: str
    wav_path: Path


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _invalid_audio(detail: str) -> ApiRequestError:
    return ApiRequestError(400, "invalid_audio", detail)


def _missing_audio() -> ApiRequestError:
    return ApiRequestError(
        404,
        "audio_not_found",
        "The requested conversation audio is unavailable.",
    )


def _conversation_failed() -> ApiRequestError:
    return ApiRequestError(
        500,
        "conversation_failed",
        "The local conversation runtime could not complete this turn. "
        "Check the trusted server logs and local runtime configuration.",
    )


def _upload_storage_failed() -> ApiRequestError:
    return ApiRequestError(
        500,
        "upload_storage_failed",
        "The local API could not store the uploaded audio in its runtime directory.",
    )


def _new_turn_id() -> str:
    return f"{SUPPORTED_COMPANION_ID}_turn_{uuid.uuid4().hex}"


def _audio_signature_is_valid(extension: str, header: bytes) -> bool:
    if extension == ".wav":
        return (
            len(header) >= 12
            and header[:4] == b"RIFF"
            and header[8:12] == b"WAVE"
        )
    if extension == ".mp3":
        return header.startswith(b"ID3") or (
            len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0
        )
    if extension == ".m4a":
        return len(header) >= 8 and header[4:8] == b"ftyp"
    if extension == ".ogg":
        return header.startswith(b"OggS")
    if extension == ".webm":
        return header.startswith(b"\x1a\x45\xdf\xa3")
    return False


class LocalVoiceApiService:
    """Dependency-light request handling around run_conversation_turn()."""

    def __init__(
        self,
        *,
        conversation_runner: Callable[..., ConversationTurnResult] = (
            run_conversation_turn
        ),
        max_upload_bytes: int = MAX_UPLOAD_BYTES,
    ) -> None:
        if type(max_upload_bytes) is not int or max_upload_bytes <= 0:
            raise ValueError("Maximum upload size must be a positive integer.")
        self._runtime_root_hint = Path(DEFAULT_RUNTIME_ROOT)
        self._incoming_dir_hint = self._runtime_root_hint / "incoming"
        self._conversation_runner = conversation_runner
        self._max_upload_bytes = max_upload_bytes
        self._conversation_lock = asyncio.Lock()
        self._runtime_thread_lock = threading.Lock()
        self._registry_lock = threading.RLock()
        self._turn_audio: dict[str, TurnAudioRecord] = {}

    @staticmethod
    def health() -> dict[str, str]:
        """Return a constant health response without touching any model runtime."""

        return {"status": "ok", "service": SERVICE_NAME}

    def _runtime_root(self) -> Path:
        candidate = self._runtime_root_hint.expanduser()
        if candidate.is_symlink() or not is_e_drive_path(candidate):
            raise _upload_storage_failed()
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise _upload_storage_failed() from error
        if not resolved.is_dir() or not is_e_drive_path(resolved):
            raise _upload_storage_failed()
        return resolved

    def _run_conversation_locked(
        self,
        *,
        audio_path: Path,
        runtime_root: Path,
        turn_id: str,
    ) -> ConversationTurnResult:
        """Keep the shared model runtimes single-threaded, even on cancellation."""

        with self._runtime_thread_lock:
            return self._conversation_runner(
                companion_id=SUPPORTED_COMPANION_ID,
                audio_path=audio_path,
                runtime_root=runtime_root,
                turn_id=turn_id,
            )

    def prepare_incoming_directory(self) -> Path:
        """Create and validate the one canonical upload directory."""

        root = self._runtime_root()
        candidate = self._incoming_dir_hint.expanduser()
        if candidate.is_symlink():
            raise _upload_storage_failed()
        try:
            unresolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise _upload_storage_failed() from error
        expected = (root / "incoming").resolve(strict=False)
        if unresolved != expected or not _is_within(unresolved, root):
            raise _upload_storage_failed()
        try:
            unresolved.mkdir(parents=False, exist_ok=True)
            resolved = unresolved.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise _upload_storage_failed() from error
        if (
            resolved != expected
            or not resolved.is_dir()
            or not _is_within(resolved, root)
        ):
            raise _upload_storage_failed()
        return resolved

    async def _close_upload(self, upload: UploadStream | None) -> None:
        if upload is None:
            return
        try:
            await upload.close()
        except Exception:
            _LOGGER.debug("Could not close multipart upload cleanly.", exc_info=True)

    def _validated_upload_extension(self, upload: UploadStream) -> str:
        supplied_filename = upload.filename
        if not isinstance(supplied_filename, str) or not supplied_filename.strip():
            raise _invalid_audio("The audio upload must include a filename.")
        normalized_filename = supplied_filename.replace("\\", "/")
        extension = PurePosixPath(normalized_filename).suffix.casefold()
        if extension not in SUPPORTED_AUDIO_EXTENSIONS:
            supported = ", ".join(sorted(SUPPORTED_AUDIO_EXTENSIONS))
            raise _invalid_audio(
                f"Unsupported audio extension. Supported extensions: {supported}."
            )

        raw_content_type = upload.content_type
        if not isinstance(raw_content_type, str) or not raw_content_type.strip():
            raise _invalid_audio("The audio upload must include a supported media type.")
        content_type = raw_content_type.partition(";")[0].strip().casefold()
        if content_type not in _ALLOWED_MEDIA_TYPES[extension]:
            raise _invalid_audio(
                "The audio media type does not match its supported file extension."
            )
        return extension

    def _allocate_upload_path(self, incoming_dir: Path, extension: str) -> Path:
        for _attempt in range(8):
            candidate = incoming_dir / f"upload_{uuid.uuid4().hex}{extension}"
            if not candidate.exists() and not candidate.is_symlink():
                return candidate
        raise _upload_storage_failed()

    def _remove_partial_upload(self, path: Path | None, incoming_dir: Path) -> None:
        if path is None:
            return
        try:
            if (
                path.parent == incoming_dir
                and _UPLOAD_BASENAME.fullmatch(path.name)
                and not path.is_dir()
            ):
                path.unlink(missing_ok=True)
        except OSError:
            _LOGGER.warning("Could not remove a partial local upload.", exc_info=True)

    async def _store_upload(self, upload: UploadStream) -> Path:
        destination: Path | None = None
        incoming_dir: Path | None = None
        try:
            extension = self._validated_upload_extension(upload)
            incoming_dir = self.prepare_incoming_directory()
            destination = self._allocate_upload_path(incoming_dir, extension)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(destination, flags, 0o600)
            byte_count = 0
            header = bytearray()
            with os.fdopen(descriptor, "wb") as output_file:
                while True:
                    chunk = await upload.read(UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise _invalid_audio("The audio upload returned invalid data.")
                    byte_count += len(chunk)
                    if byte_count > self._max_upload_bytes:
                        raise _invalid_audio(
                            "The audio upload exceeds the configured local limit "
                            f"of {self._max_upload_bytes} bytes."
                        )
                    if len(header) < 16:
                        header.extend(chunk[: 16 - len(header)])
                    output_file.write(chunk)

            if byte_count == 0:
                raise _invalid_audio("The uploaded audio file is empty.")
            if not _audio_signature_is_valid(extension, bytes(header)):
                raise _invalid_audio(
                    "The uploaded bytes do not match the declared audio format."
                )
            return destination
        except ApiRequestError:
            if incoming_dir is not None:
                self._remove_partial_upload(destination, incoming_dir)
            raise
        except asyncio.CancelledError:
            if incoming_dir is not None:
                self._remove_partial_upload(destination, incoming_dir)
            raise
        except Exception as error:
            if incoming_dir is not None:
                self._remove_partial_upload(destination, incoming_dir)
            _LOGGER.exception("Failed to store a local audio upload.")
            raise _upload_storage_failed() from error
        finally:
            await self._close_upload(upload)

    def _validate_and_register_result(
        self,
        turn_id: str,
        result: ConversationTurnResult,
    ) -> TurnAudioRecord:
        root = self._runtime_root()
        if not _SAFE_TURN_ID.fullmatch(turn_id):
            raise ValueError("Conversation runtime returned an unsafe turn ID.")

        metadata_path = Path(result.metadata_path)
        if metadata_path.is_symlink() or metadata_path.name != "turn.json":
            raise ValueError("Conversation metadata path is invalid.")
        turn_dir = metadata_path.parent.resolve(strict=True)
        expected_turn_dir = (
            root / "generated" / "conversations" / turn_id
        ).resolve(strict=False)
        if (
            turn_dir != expected_turn_dir
            or not metadata_path.is_file()
            or not _is_within(turn_dir, root)
        ):
            raise ValueError("Conversation metadata escaped its expected turn directory.")

        unresolved_wav = Path(result.output_wav_path)
        if unresolved_wav.is_symlink():
            raise ValueError("Conversation WAV must not be a symlink.")
        wav_path = unresolved_wav.resolve(strict=True)
        if (
            not wav_path.is_file()
            or wav_path.suffix.casefold() != ".wav"
            or not _is_within(wav_path, turn_dir)
            or not _is_within(wav_path, root)
        ):
            raise ValueError("Conversation WAV escaped its expected turn directory.")

        for field_name in (
            "raw_transcript",
            "normalized_transcript",
            "assistant_response",
        ):
            value = getattr(result, field_name, None)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Conversation result {field_name} is invalid.")
        if len(result.assistant_response) > MAX_SYNTHESIS_TEXT_CHARACTERS:
            raise ValueError("Conversation response exceeds the synthesis limit.")

        record = TurnAudioRecord(turn_id=turn_id, wav_path=wav_path)
        with self._registry_lock:
            self._turn_audio[turn_id] = record
        return record

    async def create_turn(
        self,
        companion: str | None,
        audio: UploadStream | None,
    ) -> dict[str, str]:
        """Store one upload, call Milestone 4 once, and register its WAV."""

        if companion != SUPPORTED_COMPANION_ID:
            await self._close_upload(audio)
            raise ApiRequestError(
                400,
                "invalid_companion",
                "Milestone 5A supports only companion=aanya.",
            )
        if audio is None:
            raise _invalid_audio("A multipart audio file is required.")

        uploaded_path = await self._store_upload(audio)
        turn_id = _new_turn_id()
        try:
            async with self._conversation_lock:
                result = await asyncio.to_thread(
                    self._run_conversation_locked,
                    audio_path=uploaded_path,
                    runtime_root=self._runtime_root(),
                    turn_id=turn_id,
                )
            self._validate_and_register_result(turn_id, result)
        except ApiRequestError:
            self._remove_partial_upload(uploaded_path, uploaded_path.parent)
            raise
        except Exception as error:
            self._remove_partial_upload(uploaded_path, uploaded_path.parent)
            _LOGGER.exception("Local conversation turn %s failed.", turn_id)
            raise _conversation_failed() from error

        return {
            "ai_disclosure": (
                "Aanya is an adult fictional AI companion, not a human."
            ),
            "turn_id": turn_id,
            "companion": SUPPORTED_COMPANION_ID,
            "raw_transcript": result.raw_transcript,
            "normalized_transcript": result.normalized_transcript,
            "response": result.assistant_response,
            "audio_url": f"/v1/conversation/turns/{turn_id}/audio",
        }

    def audio_path_for(self, turn_id: str) -> Path:
        """Return only a previously registered generated WAV for a safe ID."""

        if not isinstance(turn_id, str) or not _SAFE_TURN_ID.fullmatch(turn_id):
            raise _missing_audio()
        with self._registry_lock:
            record = self._turn_audio.get(turn_id)
        if record is None:
            raise _missing_audio()

        candidate = record.wav_path
        root = self._runtime_root()
        expected_turn_dir = (
            root / "generated" / "conversations" / turn_id
        ).resolve(strict=False)
        try:
            if candidate.is_symlink():
                raise OSError("symlinked audio")
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            raise _missing_audio() from None
        if (
            not resolved.is_file()
            or resolved.suffix.casefold() != ".wav"
            or not _is_within(resolved, expected_turn_dir)
            or not _is_within(resolved, root)
        ):
            raise _missing_audio()
        return resolved


def _shutdown_local_runtimes() -> None:
    try:
        shutdown_transcription_runtime()
    finally:
        shutdown_voice_clone_runtime()


def create_app(
    *,
    service: LocalVoiceApiService | None = None,
    conversation_runner: Callable[..., ConversationTurnResult] | None = None,
    max_upload_bytes: int = MAX_UPLOAD_BYTES,
    shutdown_callback: Callable[[], None] | None | object = _DEFAULT_SHUTDOWN,
) -> FastAPI:
    """Create the local-only FastAPI app with injectable lightweight seams."""

    if service is not None and conversation_runner is not None:
        raise ValueError("Pass either service or conversation_runner, not both.")
    selected_service = service or LocalVoiceApiService(
        conversation_runner=(
            run_conversation_turn
            if conversation_runner is None
            else conversation_runner
        ),
        max_upload_bytes=max_upload_bytes,
    )
    selected_shutdown = (
        _shutdown_local_runtimes
        if shutdown_callback is _DEFAULT_SHUTDOWN
        else shutdown_callback
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        incoming_dir = selected_service.prepare_incoming_directory()
        previous_tempdir = tempfile.tempdir
        previous_tmpdir = os.environ.get("TMPDIR")
        tempfile.tempdir = str(incoming_dir)
        os.environ["TMPDIR"] = str(incoming_dir)
        try:
            yield
        finally:
            tempfile.tempdir = previous_tempdir
            if previous_tmpdir is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = previous_tmpdir
            if callable(selected_shutdown):
                await asyncio.to_thread(selected_shutdown)

    application = FastAPI(
        title="Aira Local Voice API",
        description=(
            "Local-development bridge for an explicitly identified fictional AI. "
            "Not for public-internet deployment."
        ),
        version="5A",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.add_middleware(
        ConversationRequestGuardMiddleware,
        max_body_bytes=max_upload_bytes + MAX_MULTIPART_OVERHEAD_BYTES,
    )
    application.state.voice_service = selected_service

    @application.exception_handler(ApiRequestError)
    async def api_error_handler(
        _request: Request, error: ApiRequestError
    ) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content=error.payload())

    @application.get("/health")
    async def health() -> dict[str, str]:
        return selected_service.health()

    @application.post("/v1/conversation/turn")
    async def conversation_turn(request: Request) -> dict[str, str]:
        content_type = request.headers.get("content-type", "")
        if not content_type.casefold().startswith("multipart/form-data;"):
            raise _invalid_audio(
                "A multipart/form-data audio upload is required."
            )

        try:
            form = await request.form(
                max_files=1,
                max_fields=1,
                max_part_size=MAX_FORM_FIELD_BYTES,
            )
        except StarletteHttpException as error:
            if error.status_code != 400:
                raise
            detail = (
                _REQUEST_TOO_LARGE_DETAIL
                if error.detail == _REQUEST_TOO_LARGE_DETAIL
                else "The multipart audio upload is invalid."
            )
            raise _invalid_audio(detail) from error
        except MultiPartException as error:
            detail = (
                _REQUEST_TOO_LARGE_DETAIL
                if error.message == _REQUEST_TOO_LARGE_DETAIL
                else "The multipart audio upload is invalid."
            )
            raise _invalid_audio(detail) from error

        try:
            field_names = {name for name, _value in form.multi_items()}
            companion_values = form.getlist("companion")
            audio_values = form.getlist("audio")
            companion = (
                companion_values[0]
                if len(companion_values) == 1
                and isinstance(companion_values[0], str)
                else None
            )
            audio = (
                audio_values[0]
                if len(audio_values) == 1
                and isinstance(audio_values[0], UploadFile)
                else None
            )
            if not field_names.issubset({"companion", "audio"}):
                raise _invalid_audio(
                    "The multipart request contains unsupported fields."
                )
            return await selected_service.create_turn(companion, audio)
        finally:
            await form.close()

    @application.get("/v1/conversation/turns/{turn_id}/audio")
    async def conversation_audio(turn_id: str) -> FileResponse:
        audio_path = selected_service.audio_path_for(turn_id)
        return FileResponse(
            path=str(audio_path),
            media_type="audio/wav",
            filename=f"{turn_id}.wav",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return application


app = create_app()
