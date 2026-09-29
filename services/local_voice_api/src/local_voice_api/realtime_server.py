"""FastAPI WebSocket transport for the bounded realtime session foundation."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Mapping

from fastapi import WebSocket, WebSocketDisconnect

from .realtime_pipeline import (
    RealtimeEventSink,
    RealtimeTurnProcessor,
    UnavailableRealtimeTurnProcessor,
)
from .realtime_protocol import (
    AI_DISCLOSURE,
    ClientEventType,
    PcmAudioFormat,
    ProtocolError,
    ServerEventType,
    SessionStartEvent,
    error_event,
    parse_client_event,
    server_event,
)
from .realtime_session import RealtimeSession, RealtimeSessionState, RealtimeTurn
from .streaming import (
    NoSpeechDetectedError,
    RealtimeInferenceUnavailableError,
    RealtimeSttTimeoutError,
)

_LOGGER = logging.getLogger(__name__)
_CANCEL_CLEANUP_TIMEOUT_SECONDS = 0.25
_PROCESSOR_CLOSE_TIMEOUT_SECONDS = 0.5


def _monotonic_us() -> int:
    return time.monotonic_ns() // 1_000


class _WebSocketSink(RealtimeEventSink):
    def __init__(
        self,
        websocket: WebSocket,
        session: RealtimeSession,
        send_lock: asyncio.Lock,
    ) -> None:
        self._websocket = websocket
        self._session = session
        self._send_lock = send_lock
        self._cancelled_turn_ids: set[str] = set()
        self._pending_audio_should_send: bool | None = None

    def cancel_turn(self, turn_id: str | None) -> None:
        if turn_id is not None:
            self._cancelled_turn_ids.add(turn_id)

    async def send_json(self, event: dict[str, object]) -> None:
        event_type = event.get("type")
        async with self._send_lock:
            turn_id = event.get("turn_id")
            cancelled = (
                isinstance(turn_id, str) and turn_id in self._cancelled_turn_ids
            )
            if event_type == ServerEventType.AUDIO_CHUNK.value:
                self._pending_audio_should_send = not cancelled
            if cancelled:
                return
            if event_type == ServerEventType.THINKING.value:
                self._session.mark_thinking()
            elif event_type == ServerEventType.SPEAKING.value:
                self._session.mark_speaking()
            await self._websocket.send_json(event)

    async def send_audio(self, audio: bytes) -> None:
        async with self._send_lock:
            should_send = self._pending_audio_should_send
            self._pending_audio_should_send = None
            if should_send is False:
                return
            await self._websocket.send_bytes(audio)


class RealtimeWebSocketHandler:
    """Own one connection, one processor, and at most one active turn task."""

    def __init__(
        self,
        websocket: WebSocket,
        *,
        readiness_snapshot: Callable[[], Mapping[str, object]],
        processor_factory: Callable[[], RealtimeTurnProcessor] = (
            UnavailableRealtimeTurnProcessor
        ),
        inference_available: bool = False,
        readiness_probe: Callable[[], None] | None = None,
        capacity_snapshot: Callable[[], Mapping[str, object]] | None = None,
    ) -> None:
        self._websocket = websocket
        self._readiness_snapshot = readiness_snapshot
        self._processor = processor_factory()
        self._inference_available = inference_available
        self._readiness_probe = readiness_probe
        self._capacity_snapshot = capacity_snapshot
        self._session = RealtimeSession()
        self._send_lock = asyncio.Lock()
        self._sink = _WebSocketSink(websocket, self._session, self._send_lock)
        self._turn_task: asyncio.Task[None] | None = None
        self._connection_id = f"connection_{uuid.uuid4().hex}"

    async def run(self) -> None:
        await self._websocket.accept()
        _LOGGER.info(
            "[AIRA REALTIME TIMING] event=websocket_accepted connection_id=%s "
            "session_id=%s generation=0 monotonic_us=%d",
            self._connection_id,
            self._session.session_id,
            _monotonic_us(),
        )
        try:
            if not await self._receive_session_start():
                return
            while self._session.state is not RealtimeSessionState.CLOSED:
                message = await self._websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                await self._handle_message(message)
        except WebSocketDisconnect:
            pass
        except Exception:
            self._session.fail()
            _LOGGER.exception(
                "Unexpected realtime session failure session_id=%s",
                self._session.session_id,
            )
            await self._send_error(
                ProtocolError(
                    "session_failure",
                    "The realtime session failed and must be reconnected.",
                    fatal=True,
                )
            )
        finally:
            await self._cleanup()

    async def _receive_session_start(self) -> bool:
        try:
            message = await self._websocket.receive()
        except WebSocketDisconnect:
            return False
        if message["type"] == "websocket.disconnect":
            return False
        raw = message.get("text")
        if raw is None:
            await self._send_error(
                ProtocolError(
                    "session_start_required",
                    "The first realtime message must be session_start JSON.",
                    fatal=True,
                )
            )
            await self._websocket.close(code=1008)
            return False
        try:
            event = parse_client_event(raw)
            if not isinstance(event, SessionStartEvent):
                raise ProtocolError(
                    "session_start_required",
                    "The first realtime message must be session_start JSON.",
                    fatal=True,
                )
            self._session.start(event.companion, event.audio_format)
            self._session.begin_listening()
            self._log_session_timing("session_start_received")
        except ProtocolError as error:
            # Before a session exists there is no recoverable state to return
            # to. Close deterministically instead of entering the main loop in
            # CONNECTING and rejecting every subsequent message.
            handshake_error = ProtocolError(
                error.code,
                error.message,
                fatal=True,
            )
            await self._send_error(handshake_error)
            await self._websocket.close(code=1008)
            return False

        readiness = dict(self._readiness_snapshot())
        if (
            self._inference_available
            and self._readiness_probe is not None
            and readiness.get("status") == "ready"
        ):
            try:
                await asyncio.to_thread(self._readiness_probe)
            except Exception:
                raw_components = readiness.get("components")
                components = (
                    dict(raw_components)
                    if isinstance(raw_components, Mapping)
                    else {}
                )
                if "tts" in components:
                    components["tts"] = "degraded"
                readiness.update(
                    {
                        "status": "degraded",
                        "components": components,
                        "message": (
                            "The configured realtime TTS worker is unavailable."
                        ),
                    }
                )

        if self._inference_available and readiness.get("status") == "ready":
            try:
                prepare = getattr(self._processor, "prepare", None)
                if prepare is not None:
                    await prepare()
                    self._log_session_timing("processor_prepared")
            except Exception:
                raw_components = readiness.get("components")
                components = (
                    dict(raw_components)
                    if isinstance(raw_components, Mapping)
                    else {}
                )
                if "llm" in components:
                    components["llm"] = "degraded"
                readiness.update(
                    {
                        "status": "degraded",
                        "components": components,
                        "message": (
                            "The persistent local inference connection is unavailable."
                        ),
                    }
                )

        can_process_turns = (
            self._inference_available and readiness.get("status") == "ready"
        )
        if self._capacity_snapshot is not None:
            try:
                readiness["queues"] = dict(
                    await asyncio.to_thread(self._capacity_snapshot)
                )
            except Exception:
                _LOGGER.warning(
                    "Realtime capacity snapshot unavailable connection_id=%s",
                    self._connection_id,
                )
        await self._send_json(
            server_event(
                ServerEventType.SESSION_READY,
                session_id=self._session.session_id,
                companion="aanya",
                ai_disclosure=AI_DISCLOSURE,
                audio_format=PcmAudioFormat().payload(),
                readiness=readiness,
                can_process_turns=can_process_turns,
            )
        )
        self._log_session_timing("session_ready_sent")
        if not can_process_turns:
            self._session.close()
            await self._websocket.close(code=1013)
            return False
        return True

    async def _handle_message(self, message: dict[str, object]) -> None:
        binary = message.get("bytes")
        if binary is not None:
            try:
                self._session.push_audio(binary)
                _LOGGER.debug(
                    "Realtime input frame connection_id=%s session_id=%s bytes=%d buffered_bytes=%d received_at=%.6f",
                    self._connection_id,
                    self._session.session_id,
                    len(binary),
                    self._session.buffered_audio_bytes,
                    time.monotonic(),
                )
            except ProtocolError as error:
                await self._send_error(error)
                if error.code == "turn_audio_too_large":
                    self._session.recover_turn()
            return

        raw = message.get("text")
        if raw is None:
            await self._send_error(
                ProtocolError("invalid_message", "Expected JSON text or PCM binary audio.")
            )
            return
        try:
            event = parse_client_event(raw)
            if event.type is ClientEventType.SESSION_START:
                raise ProtocolError(
                    "duplicate_session_start", "session_start may be sent only once."
                )
            if event.type is ClientEventType.END_OF_TURN:
                await self._end_turn()
            elif event.type is ClientEventType.CANCEL_TURN:
                await self._cancel_turn()
            elif event.type is ClientEventType.SESSION_END:
                self._session.close()
                await self._websocket.close(code=1000)
        except ProtocolError as error:
            await self._send_error(error)
            if error.fatal:
                self._session.close()
                await self._websocket.close(code=1008)

    async def _end_turn(self) -> None:
        try:
            turn = self._session.finish_audio()
        except ProtocolError as error:
            await self._send_error(error)
            if error.code == "no_speech":
                self._session.recover_turn()
            return
        _LOGGER.info(
            "Realtime end_of_turn connection_id=%s session_id=%s turn_id=%s generation=%d input_chunks=%d input_bytes=%d first_audio_at=%.6f last_audio_at=%.6f end_of_turn_at=%.6f",
            self._connection_id,
            turn.session_id,
            turn.turn_id,
            turn.generation,
            turn.input_chunk_count,
            len(turn.audio),
            turn.first_audio_received_at,
            turn.last_audio_received_at,
            turn.end_of_turn_received_at,
        )
        turn.metrics.log_event(
            _LOGGER,
            "websocket_turn_received",
            session_id=turn.session_id,
            generation=turn.generation,
            input_chunks=turn.input_chunk_count,
            input_bytes=len(turn.audio),
        )
        turn.metrics.log_event(
            _LOGGER,
            "end_of_turn_received",
            session_id=turn.session_id,
            generation=turn.generation,
            input_chunks=turn.input_chunk_count,
            input_bytes=len(turn.audio),
        )
        if self._turn_task is not None and not self._turn_task.done():
            self._session.recover_turn()
            await self._send_error(
                ProtocolError("turn_busy", "A realtime turn is already being processed.")
            )
            return
        self._turn_task = asyncio.create_task(
            self._process_turn(turn), name=f"realtime-{turn.turn_id}"
        )

    async def _process_turn(self, turn: RealtimeTurn) -> None:
        try:
            await self._processor.process_turn(
                turn, self._sink, turn.cancel_event
            )
            if self._session.state not in {
                RealtimeSessionState.CANCELLED,
                RealtimeSessionState.CLOSED,
            }:
                self._session.complete_turn()
        except asyncio.CancelledError:
            return
        except NoSpeechDetectedError:
            await self._send_error(
                ProtocolError(
                    "no_speech",
                    "No clear speech was detected. Please speak a little longer; listening will continue automatically.",
                )
            )
            self._recover_if_open()
        except RealtimeSttTimeoutError:
            await self._send_error(
                ProtocolError(
                    "stt_timeout",
                    "Speech recognition timed out. Please try speaking again.",
                )
            )
            self._recover_if_open()
        except RealtimeInferenceUnavailableError:
            await self._send_error(
                ProtocolError(
                    "realtime_inference_unavailable",
                    "Realtime inference is not enabled; the HTTP debug path remains available.",
                )
            )
            self._recover_if_open()
        except Exception:
            _LOGGER.exception("Realtime turn failed turn_id=%s", turn.turn_id)
            await self._send_error(
                ProtocolError(
                    "turn_failed",
                    "The realtime turn failed. You can try speaking again.",
                )
            )
            self._recover_if_open()

    async def _cancel_turn(self) -> None:
        try:
            turn_id = self._session.cancel_turn()
        except ProtocolError as error:
            await self._send_error(error)
            return
        self._sink.cancel_turn(turn_id)
        task = self._turn_task
        if task is not None and not task.done():
            task.cancel()
            cancel_task = asyncio.create_task(self._processor.cancel())
            done, pending = await asyncio.wait(
                {task, cancel_task},
                timeout=_CANCEL_CLEANUP_TIMEOUT_SECONDS,
            )
            for pending_task in pending:
                pending_task.cancel()
            for completed in done:
                try:
                    completed.result()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    _LOGGER.warning(
                        "Realtime cancellation hook failed session_id=%s turn_id=%s",
                        self._session.session_id,
                        turn_id,
                    )
            if pending:
                _LOGGER.warning(
                    "Realtime cancellation cleanup timed out session_id=%s turn_id=%s",
                    self._session.session_id,
                    turn_id,
                )
        self._turn_task = None
        await self._send_json(
            server_event(
                ServerEventType.RECOVERABLE_ERROR,
                code="turn_cancelled",
                message="The active realtime turn was cancelled.",
                turn_id=turn_id,
            )
        )
        self._session.recover_turn()

    def _recover_if_open(self) -> None:
        if self._session.state not in {
            RealtimeSessionState.CLOSED,
            RealtimeSessionState.CANCELLED,
        }:
            self._session.recover_turn()

    async def _cleanup(self) -> None:
        self._log_session_timing("cleanup_started")
        task = self._turn_task
        if task is not None and not task.done():
            try:
                if self._session.state in {
                    RealtimeSessionState.LISTENING,
                    RealtimeSessionState.FINALIZING_STT,
                    RealtimeSessionState.THINKING,
                    RealtimeSessionState.SPEAKING,
                }:
                    self._session.cancel_turn()
                else:
                    self._session.cancel_requested.set()
                task.cancel()
                cancel_task = asyncio.create_task(self._processor.cancel())
                done, pending = await asyncio.wait(
                    {task, cancel_task},
                    timeout=_CANCEL_CLEANUP_TIMEOUT_SECONDS,
                )
                for pending_task in pending:
                    pending_task.cancel()
                for completed in done:
                    try:
                        completed.result()
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        _LOGGER.debug(
                            "Realtime cleanup task failed.", exc_info=True
                        )
                if pending:
                    _LOGGER.warning(
                        "Realtime disconnect cleanup timed out session_id=%s",
                        self._session.session_id,
                    )
            except Exception:
                _LOGGER.exception(
                    "Realtime processor cancellation failed session_id=%s",
                    self._session.session_id,
                )
        try:
            await asyncio.wait_for(
                self._processor.close(),
                timeout=_PROCESSOR_CLOSE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            _LOGGER.warning(
                "Realtime processor close timed out session_id=%s",
                self._session.session_id,
            )
        except Exception:
            _LOGGER.exception(
                "Realtime processor close failed session_id=%s",
                self._session.session_id,
            )
        finally:
            self._session.close()
            self._log_session_timing("cleanup_complete")

    async def _send_error(self, error: ProtocolError) -> None:
        try:
            await self._send_json(error_event(error))
        except Exception:
            pass

    async def _send_json(self, event: dict[str, object]) -> None:
        async with self._send_lock:
            await self._websocket.send_json(event)

    def _log_session_timing(self, event: str) -> None:
        _LOGGER.info(
            "[AIRA REALTIME TIMING] event=%s connection_id=%s session_id=%s "
            "generation=%d monotonic_us=%d",
            event,
            self._connection_id,
            self._session.session_id,
            self._session.turn_generation,
            _monotonic_us(),
        )
