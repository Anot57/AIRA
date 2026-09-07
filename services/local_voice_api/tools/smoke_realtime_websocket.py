#!/usr/bin/env python3
"""Send one WAV through Aira realtime v1 without third-party client packages."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import time
import wave
from pathlib import Path
from urllib.parse import urlsplit

PROTOCOL_VERSION = 1
INPUT_SAMPLE_RATE_HZ = 16_000
MAX_FRAME_BYTES = 64 * 1024


class WebSocket:
    def __init__(self, url: str, timeout: float) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "ws" or not parsed.hostname:
            raise ValueError("Realtime URL must use ws:// and include a host.")
        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.path = parsed.path or "/"
        self.timeout = timeout
        self.socket: socket.socket | None = None

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), self.timeout)
        sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request)
        response = self._read_http_headers(sock)
        status = response[0].decode("iso-8859-1", errors="replace")
        headers = {}
        for line in response[1:]:
            name, separator, value = line.partition(b":")
            if separator:
                headers[name.strip().lower()] = value.strip().lower()
        expected = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).lower()
        if " 101 " not in f" {status} " or headers.get(b"sec-websocket-accept") != expected:
            sock.close()
            raise RuntimeError(f"WebSocket handshake failed: {status}")
        self.socket = sock

    def send_json(self, payload: dict[str, object]) -> None:
        self._send_frame(0x1, json.dumps(payload, separators=(",", ":")).encode())

    def send_binary(self, payload: bytes) -> None:
        self._send_frame(0x2, payload)

    def receive(self) -> tuple[int, bytes]:
        sock = self._require_socket()
        while True:
            first, second = self._read_exact(sock, 2)
            opcode = first & 0x0F
            length = second & 0x7F
            if second & 0x80:
                raise RuntimeError("Server frames must not be masked.")
            if length == 126:
                length = struct.unpack("!H", self._read_exact(sock, 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(sock, 8))[0]
            if length > 16 * 1024 * 1024:
                raise RuntimeError("Server WebSocket frame is unexpectedly large.")
            payload = self._read_exact(sock, length)
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            return opcode, payload

    def close(self) -> None:
        if self.socket is None:
            return
        try:
            self._send_frame(0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        self.socket.close()
        self.socket = None

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        sock = self._require_socket()
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes((0x80 | opcode, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytes((0x80 | opcode, 0xFE)) + struct.pack("!H", length)
        else:
            header = bytes((0x80 | opcode, 0xFF)) + struct.pack("!Q", length)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        sock.sendall(header + mask + masked)

    def _require_socket(self) -> socket.socket:
        if self.socket is None:
            raise RuntimeError("WebSocket is not connected.")
        return self.socket

    @staticmethod
    def _read_http_headers(sock: socket.socket) -> list[bytes]:
        content = bytearray()
        while b"\r\n\r\n" not in content:
            if len(content) > 16 * 1024:
                raise RuntimeError("WebSocket response headers are too large.")
            content.extend(WebSocket._read_exact(sock, 1))
        return bytes(content[:-4]).split(b"\r\n")

    @staticmethod
    def _read_exact(sock: socket.socket, length: int) -> bytes:
        content = bytearray()
        while len(content) < length:
            chunk = sock.recv(length - len(content))
            if not chunk:
                raise EOFError("WebSocket closed unexpectedly.")
            content.extend(chunk)
        return bytes(content)


def _input_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav_file:
        if (
            wav_file.getnchannels() != 1
            or wav_file.getsampwidth() != 2
            or wav_file.getframerate() != INPUT_SAMPLE_RATE_HZ
            or wav_file.getcomptype() != "NONE"
        ):
            raise ValueError("Input must be uncompressed 16 kHz mono PCM16 WAV.")
        return wav_file.readframes(wav_file.getnframes())


def _write_output(path: Path, pcm: bytes, sample_rate_hz: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate_hz)
        wav_file.writeframes(pcm)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_wav", type=Path)
    parser.add_argument("output_wav", type=Path)
    parser.add_argument("--url", default="ws://127.0.0.1:8765/v1/realtime")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument(
        "--pace-realtime",
        action="store_true",
        help="Send PCM at approximately realtime microphone speed.",
    )
    parser.add_argument(
        "--frame-ms",
        type=float,
        default=40.0,
        help="PCM frame duration when --pace-realtime is enabled (default: 40 ms).",
    )
    args = parser.parse_args()
    pcm = _input_pcm(args.input_wav)
    client = WebSocket(args.url, args.timeout)
    output = bytearray()
    output_rate: int | None = None
    pending_audio_bytes: int | None = None
    started_at: float | None = None
    first_audio_ms: float | None = None
    try:
        client.connect()
        client.send_json(
            {
                "type": "session_start",
                "protocol_version": PROTOCOL_VERSION,
                "companion": "aanya",
                "audio_format": {
                    "encoding": "pcm_s16le",
                    "sample_rate_hz": INPUT_SAMPLE_RATE_HZ,
                    "channels": 1,
                },
            }
        )
        opcode, payload = client.receive()
        ready = json.loads(payload) if opcode == 0x1 else {}
        print("session_ready:", json.dumps(ready, indent=2))
        if not ready.get("can_process_turns"):
            raise RuntimeError("Server is not ready to process realtime turns.")
        if args.pace_realtime:
            if args.frame_ms <= 0 or args.frame_ms > 1000:
                raise ValueError("--frame-ms must be greater than 0 and at most 1000.")

            bytes_per_second = INPUT_SAMPLE_RATE_HZ * 2
            frame_bytes = int(bytes_per_second * (args.frame_ms / 1000.0))
            frame_bytes -= frame_bytes % 2
            frame_bytes = max(2, min(frame_bytes, MAX_FRAME_BYTES))

            next_send_at = time.perf_counter()

            for offset in range(0, len(pcm), frame_bytes):
                client.send_binary(pcm[offset : offset + frame_bytes])

                next_send_at += args.frame_ms / 1000.0
                delay = next_send_at - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
        else:
            for offset in range(0, len(pcm), MAX_FRAME_BYTES):
                client.send_binary(pcm[offset : offset + MAX_FRAME_BYTES])

        client.send_json(
            {"type": "end_of_turn", "protocol_version": PROTOCOL_VERSION}
        )
        started_at = time.perf_counter()

        while True:
            opcode, payload = client.receive()
            if opcode == 0x8:
                raise RuntimeError("Server closed before turn_complete.")
            if opcode == 0x2:
                if pending_audio_bytes is None or len(payload) != pending_audio_bytes:
                    raise RuntimeError("Binary audio did not match its audio_chunk header.")
                if first_audio_ms is None and started_at is not None:
                    first_audio_ms = (time.perf_counter() - started_at) * 1000.0
                    print(f"client-observed first binary audio: {first_audio_ms:.3f} ms")
                output.extend(payload)
                pending_audio_bytes = None
                continue
            if opcode != 0x1:
                continue
            event = json.loads(payload)
            event_type = event.get("type")
            print("event:", event_type)
            if event_type in {"text_delta", "text_sentence", "stt_final"}:
                print(
                    "payload:",
                    json.dumps(event, ensure_ascii=False),
                )
            if event_type == "audio_chunk":
                pending_audio_bytes = int(event["byte_length"])
                rate = int(event["sample_rate_hz"])
                if output_rate is not None and rate != output_rate:
                    raise RuntimeError("Output sample rate changed during the turn.")
                output_rate = rate
            elif event_type in {"fatal_error", "recoverable_error"}:
                raise RuntimeError(json.dumps(event))
            elif event_type == "turn_complete":
                print("server metrics:", json.dumps(event.get("metrics"), indent=2))
                break
        client.send_json(
            {"type": "session_end", "protocol_version": PROTOCOL_VERSION}
        )
    finally:
        client.close()

    if not output or output_rate is None:
        raise RuntimeError("Realtime turn returned no audio.")
    _write_output(args.output_wav, bytes(output), output_rate)
    print("output:", args.output_wav.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
