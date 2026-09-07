"""FastAPI WebSocket transport for the bounded realtime session foundation."""

from __future__ import annotations

import asyncio
import logging
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
from .streaming import NoSpeechDetectedError, RealtimeInferenceUnavailableError

_LOGGER = logging.getLogger(__name__)


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

    async def send_json(self, event: dict[str, object]) -> None:
        event_type = event.get("type")
        if event_type == ServerEventType.THINKING.value:
            self._session.mark_thinking()
        elif event_type == ServerEventType.SPEAKING.value:
            self._session.mark_speaking()
        async with self._send_lock:
            await self._websocket.send_json(event)

    async def send_audio(self, audio: bytes) -> None:
        async with self._send_lock:
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
    ) -> None:
        self._websocket = websocket
        self._readiness_snapshot = readiness_snapshot
        self._processor = processor_factory()
        self._inference_available = inference_available
        self._readiness_probe = readiness_probe
        self._session = RealtimeSession()
        self._send_lock = asyncio.Lock()
        self._sink = _WebSocketSink(websocket, self._session, self._send_lock)
        self._turn_task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        await self._websocket.accept()
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
        can_process_turns = (
            self._inference_available and readiness.get("status") == "ready"
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
                    "No clear speech was detected. Hold the button a little longer and try again.",
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
        task = self._turn_task
        if task is not None and not task.done():
            await self._processor.cancel()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
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
                await self._processor.cancel()
            except Exception:
                _LOGGER.exception(
                    "Realtime processor cancellation failed session_id=%s",
                    self._session.session_id,
                )
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    _LOGGER.debug(
                        "Realtime task failed during cleanup.", exc_info=True
                    )
        try:
            await self._processor.close()
        except Exception:
            _LOGGER.exception(
                "Realtime processor close failed session_id=%s",
                self._session.session_id,
            )
        finally:
            self._session.close()

    async def _send_error(self, error: ProtocolError) -> None:
        try:
            await self._send_json(error_event(error))
        except Exception:
            pass

    async def _send_json(self, event: dict[str, object]) -> None:
        async with self._send_lock:
            await self._websocket.send_json(event)
