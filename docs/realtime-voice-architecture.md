# Realtime Voice Architecture

## Purpose and scope

Aira's realtime voice path is an Android-first, self-hosted foundation for
short spoken turns with Aanya. Aanya is an adult fictional AI companion, never
a human, romantic partner, therapist, crisis worker, or emergency service.
Realtime routing is intentionally Aanya-only; another companion ID must be
rejected rather than silently borrowing Aanya's persona or approved voice.

This is local-development infrastructure. The service has no authentication or
TLS, so it is suitable only for a trusted private network with the host firewall
enabled. Do not expose or port-forward it to the public internet. Voice audio
and transcripts are sensitive data and remain under
`/mnt/e/aira-local-runtime`; the E drive should be encrypted at rest.

The existing `POST /v1/conversation/turn` request/response path remains
available as a debugging and fallback path. It is not the intended production
realtime architecture.

## Measured baseline and target

The validated synchronous baseline on the current RTX 2070 Max-Q system is:

- Qwen3-TTS Base cold model load: 202.8 seconds
- full cold conversation turn: 329 seconds
- full warm conversation turn: 104.956 seconds

The primary product metric is **time to first audio (TTFA)**: the interval from
server detection of `end_of_turn` until the first meaningful Aanya audio is
ready to send. It is deliberately different from the time required to finish
the whole reply.

Engineering targets are p50 below 700 ms, p95 approximately at or below 1.2
seconds, and p99 at or below 2 seconds. These are goals, not claims or hard
guarantees. Mobile scheduling, local-network variance, model inference, and
audio buffering all vary in practice.

Qwen3-TTS 0.6B Base in the required RTX 2070 compatibility mode (CUDA,
`torch.float32`, eager attention, and no FlashAttention) is far outside that
budget today. Warmup removes cold-load cost from a user turn but does not make
the current synthesis engine realtime-fast. The architecture therefore keeps
TTS behind a chunk-oriented interface so a lower-latency self-hosted engine can
be evaluated without changing the transport or session protocol.

## Operational terms

- **Liveness** means the FastAPI process can answer a lightweight request.
  `GET /health` proves only this and never loads a model.
- **Readiness** describes whether STT, LLM, and TTS can serve a conversation.
  A listening Uvicorn socket is not conversational readiness.
- **TTFA** measures end-of-turn to first meaningful audio ready to send.
- **Full completion** measures the remainder of generation and delivery through
  `turn_complete`; it is expected to be later than TTFA.

Readiness can move through `starting`, `warming`, `ready`, `degraded`, and
`failed`. Component-level state lets an Android client distinguish a healthy
HTTP process from a still-loading or failed inference runtime without exposing
model paths or other machine details.

## Target data flow

```text
Android PCM16 microphone chunks
        |
        v
persistent /v1/realtime WebSocket
        |
        v
bounded realtime session and turn state
        |
        +--> streaming STT adapter --> partial/final transcript
        |
        +--> streaming LLM adapter --> token deltas --> speakable chunks
        |
        +--> chunk TTS adapter --> playable PCM/audio chunks
        |
        v
Android begins playback while the rest of the reply is generated
```

The interfaces separate transport and orchestration from model-specific code:

- a streaming transcriber accepts bounded PCM chunks, can finish or cancel a
  turn, and can initially use Faster-Whisper batch transcription for the final
  result;
- a streaming LLM yields deltas rather than requiring a complete response;
- a text chunker emits useful phrase or sentence boundaries for downstream TTS;
- a realtime synthesizer accepts those chunks, reports timing, and may initially
  return complete audio per chunk while Qwen itself remains non-streaming.

No component should repeatedly invoke Whisper for every tiny network packet,
use canned acknowledgement audio to manipulate TTFA, or introduce artificial
delays that make metrics look better.

## WebSocket protocol

The versioned endpoint is `ws://<trusted-host>:8765/v1/realtime`. Control and
metadata events are JSON text frames. Microphone and synthesized audio bytes use
binary frames so audio is not inflated by base64 encoding.

The initial microphone contract is signed 16-bit little-endian PCM, mono, at
16 kHz. A binary frame is audio for the current listening turn; it must be
bounded in size and total buffered audio must also be bounded. The server does
not require a complete WAV before it can accept audio.

Every JSON client event carries `type` and `protocol_version: 1`. The protocol
prepares these client events:

- `session_start`: select `companion: "aanya"` and negotiate the audio format;
- binary frames: PCM samples for the currently listening turn (`audio_chunk` is
  reserved as a server event and rejected as client JSON in version 1);
- `end_of_turn`: finalize buffered speech exactly once;
- `cancel_turn`: release the active turn and prepare for a future barge-in;
- `session_end`: close the logical session cleanly.

Server events include:

- `session_ready`
- `stt_partial`
- `stt_final`
- `thinking`
- `text_delta`
- `text_sentence`
- `audio_chunk`
- `speaking`
- `turn_complete`
- `recoverable_error`
- `fatal_error`

Malformed JSON, an unsupported protocol version, unsupported audio settings,
oversized data, invalid transitions, and non-Aanya routing are rejected with a
typed error. A client disconnect always runs session cleanup.

`session_ready` carries a structured readiness snapshot plus
`can_process_turns`. If models or an inference processor are unavailable, the
server sends that truthful snapshot with `can_process_turns: false` and closes
with code `1013`, allowing the Android client to retry without enabling the
microphone. Synthesized PCM is mono signed 16-bit at a declared 8 to 48 kHz rate.
Each JSON `audio_chunk` header is followed immediately by one binary frame of at
most 64 KiB; sequences begin at zero.

## Session state and cancellation

A session has explicit transitions through connecting, ready, listening,
finalizing STT, thinking, speaking, cancelled, closed, and error states. It
allows one active turn at a time. Duplicate `end_of_turn`, a turn requested
while another is active, and audio outside a listening turn are invalid.

Queues and audio buffers are bounded. Cancellation and disconnect signal every
active adapter, release buffered audio, and must not leave a shared Aanya model
lock permanently held. A turn exception is scoped to that session unless the
underlying CUDA process is itself unsafe; unrelated future sessions should
remain usable.

Silent or too-short input is a recoverable outcome, not an opaque server error.
The realtime path emits a recoverable no-speech error and returns the session to
a state in which the user can try again. The HTTP fallback maps the same known
condition to a typed client response while retaining generic, path-safe handling
for unexpected inference failures.

## Readiness and warmup

Warmup is controlled and observable. It never runs inside `GET /health`, never
blocks liveness, and never runs more than once concurrently. Expensive warmup
must remain explicitly configurable for local development so unit tests and
routine server starts cannot unexpectedly spend minutes loading CUDA models.
It is disabled by default and enabled only when the server process starts with
`AIRA_MODEL_WARMUP=1`.

The default warmup sequence acquires the CPU Faster-Whisper runtime, verifies
the existing llama.cpp executable and cached GGUF without running inference,
loads the process-wide Qwen Base model once, and creates the approved Aanya
voice-clone prompt once. Qwen's existing `_ensure_base_model()` and prompt
caches remain authoritative for the HTTP path.

When `AIRA_REALTIME_TTS_PROVIDER=pocket_worker` is explicitly selected, the TTS
readiness step instead verifies a loopback-only Pocket TTS worker. That worker
runs in the dedicated Windows Python environment, loads one model and the
cached Aanya voice state once, and reports ready only with 24 kHz mono PCM16
output. The WSL API never imports Pocket TTS. Readiness records worker failure
without claiming successful inference, and no throwaway utterance is generated.

## Latency observability

Server-side stage timers use `time.perf_counter()` or an injected monotonic
clock. Wall-clock timestamps may be recorded separately for provenance, but are
not used to calculate elapsed time. Structured timing logs carry a generated
turn/session ID and never raw audio bytes or transcript content.

The synchronous fallback records route admission, multipart parsing, upload
validation/write, audio readiness, STT acquisition and inference,
normalization, LLM generation, TTS total time, Qwen Base cold load, prompt cache
lookup or creation, synthesis, WAV serialization, metadata/result validation,
JSON construction, and totals where each boundary is reached.

The realtime path additionally measures:

- STT final latency after `end_of_turn`;
- TTFT, time to the first LLM token;
- TTFS, time to the first speakable text chunk;
- TTFAS, time to the first synthesized audio sample available to the transport;
- TTFA, time to the first meaningful binary audio frame successfully sent;
- total turn time and per-chunk synthesis time;
- audio duration and realtime factor when sample count and rate are known.

These timestamps are observations of real boundaries. Missing work is reported
as unavailable rather than synthesized or estimated.

## Current limitations and performance blockers

The route, state machine, framing, and client parser are mock-tested
foundations. The default server deliberately advertises
`can_process_turns: false`. An opt-in production
`StreamingRealtimeTurnProcessor` factory is now available for the Pocket worker;
it is selected only through explicit environment configuration and successful
readiness. The Flutter realtime client is still not connected to the existing
call-screen microphone and playback flow; that screen continues to use the
compatible HTTP path.

- **STT:** Faster-Whisper is currently batch-oriented; the adapter boundary is
  ready for true incremental transcription, but rolling-window work must be
  measured and rate-limited before it is enabled.
- **LLM:** llama.cpp must expose and deliver useful deltas early, and the first
  phrase must be chunked without waiting for the full response.
- **TTS:** Qwen3-TTS FP32/eager remains unsuitable for realtime. Pocket TTS
  produced genuine streamed chunks on Windows CPU with measured TTS-only first
  playable audio of 128.86/130.81/152.59 ms min/median/max and median RTF about
  0.394. This does not establish end-to-end TTFA.
- **Transport:** WebSocket framing removes the full-WAV request barrier, but
  trusted-LAN WebSocket without TLS/authentication is development-only.
- **Hardware:** 8 GB Turing hardware restricts precision and optimized attention
  options for the validated Qwen setup.
- **Android playback:** continuous PCM capture, jitter buffering, early playback,
  and barge-in still need device-level integration and measurement.

The most useful next experiment is an end-to-end Pocket worker smoke and timing
run through the real WebSocket, followed by repeated TTFA trials. Measure STT,
process-per-turn llama.cpp TTFT, first phrase availability, worker IPC, first
binary send, and Android playback separately. Pocket's first TTS chunk is now
unlikely to be the largest latency term; the complete-response llama.cpp adapter
and batch STT are the leading architectural blockers to sub-second TTFA.

## Local diagnostics

Start the trusted-network development server using the existing E-drive virtual
environment:

```bash
cd /mnt/e/female-voice-ai
source /mnt/e/aira-local-runtime/activate.sh
python services/local_voice_api/tools/run_local_api.py --host 0.0.0.0 --port 8765
```

Liveness and readiness are separate checks:

```bash
curl --fail-with-body http://127.0.0.1:8765/health
curl --fail-with-body http://127.0.0.1:8765/ready
```

With warmup disabled (the default), `/ready` truthfully returns HTTP 503 and a
`starting` snapshot. Setting `AIRA_MODEL_WARMUP=1` opts into model preparation.
Realtime turns additionally require the explicit `pocket_worker` provider; its
worker must already report ready. See the backend README for exact Windows
worker, WSL backend, and standard-library smoke-client commands.

Do not run a full HTTP conversation or expensive warmup as part of a routine
diagnostic. Use a WebSocket client to send a protocol-versioned `session_start`
only after reviewing the server's local-development and warmup settings.
