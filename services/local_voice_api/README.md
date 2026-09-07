# Local Voice Lab

This workspace designs short reference clips for the 20 checked-in Android
companion IDs and reuses explicitly approved clips for local speech synthesis.
Every profile is an original fictional adult AI voice, every reference
transcript identifies itself as AI, and no profile is intended to copy an
existing person's voice.

The model tools remain offline and never upload audio or transcripts. A
separate FastAPI process exposes the Aanya-only HTTP prototype and a mock-tested
realtime WebSocket foundation to a trusted local Android client. It has no
authentication or TLS and must never be exposed publicly. Because transcripts
are intentionally saved as plain UTF-8 text and JSON, the Windows E drive must
use encryption at rest (for example, BitLocker); files accessed through
`/mnt/e` inherit that volume protection.

## Runtime layout

Run the tool in WSL 2 with NVIDIA CUDA access. The runtime deliberately keeps
the virtual environment, package cache, temporary files, Hugging Face data,
PyTorch cache, and generated references on the E drive:

```text
/mnt/e/aira-local-runtime/.venv
/mnt/e/aira-local-runtime/cache
/mnt/e/aira-local-runtime/pip-cache
/mnt/e/aira-local-runtime/tmp
/mnt/e/aira-local-runtime/generated/voices
/mnt/e/aira-local-runtime/generated/runtime
/mnt/e/aira-local-runtime/generated/transcripts
/mnt/e/aira-local-runtime/generated/conversations
/mnt/e/aira-local-runtime/incoming
/mnt/e/aira-local-runtime/models/faster-whisper
/mnt/e/aira-local-runtime/llama/llama-b10715/llama-cli
/mnt/e/aira-local-runtime/llama-cache
/mnt/e/aira-local-runtime/huggingface
/mnt/e/aira-local-runtime/torch
```

The CLI refuses generation when its output or required runtime environment
variables point away from E drive.

## WSL setup

These commands assume Ubuntu 24.04 in WSL and Python 3.12. Confirm that
`nvidia-smi` works in WSL before installing the Python runtime.

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv libsndfile1 sox
mkdir -p /mnt/e/aira-local-runtime/cache /mnt/e/aira-local-runtime/cuda-cache /mnt/e/aira-local-runtime/huggingface/hub /mnt/e/aira-local-runtime/numba-cache /mnt/e/aira-local-runtime/pip-cache /mnt/e/aira-local-runtime/pycache /mnt/e/aira-local-runtime/tmp /mnt/e/aira-local-runtime/torch /mnt/e/aira-local-runtime/torchinductor-cache /mnt/e/aira-local-runtime/triton-cache
mkdir -p /mnt/e/aira-local-runtime/generated/voices /mnt/e/aira-local-runtime/generated/runtime /mnt/e/aira-local-runtime/generated/transcripts /mnt/e/aira-local-runtime/generated/conversations /mnt/e/aira-local-runtime/incoming /mnt/e/aira-local-runtime/models/faster-whisper /mnt/e/aira-local-runtime/llama-cache
python3.12 -m venv /mnt/e/aira-local-runtime/.venv
cat > /mnt/e/aira-local-runtime/activate.sh <<'EOF'
#!/usr/bin/env bash
source /mnt/e/aira-local-runtime/.venv/bin/activate
export HF_HOME=/mnt/e/aira-local-runtime/huggingface
export HF_HUB_CACHE=/mnt/e/aira-local-runtime/huggingface/hub
export HF_XET_CACHE=/mnt/e/aira-local-runtime/huggingface/xet
export HF_ASSETS_CACHE=/mnt/e/aira-local-runtime/huggingface/assets
export HF_MODULES_CACHE=/mnt/e/aira-local-runtime/huggingface/modules
export HF_TOKEN_PATH=/mnt/e/aira-local-runtime/huggingface/token
export TORCH_HOME=/mnt/e/aira-local-runtime/torch
export XDG_CACHE_HOME=/mnt/e/aira-local-runtime/cache
export PIP_CACHE_DIR=/mnt/e/aira-local-runtime/pip-cache
export TMPDIR=/mnt/e/aira-local-runtime/tmp
export CUDA_CACHE_PATH=/mnt/e/aira-local-runtime/cuda-cache
export NUMBA_CACHE_DIR=/mnt/e/aira-local-runtime/numba-cache
export TORCHINDUCTOR_CACHE_DIR=/mnt/e/aira-local-runtime/torchinductor-cache
export TRITON_CACHE_DIR=/mnt/e/aira-local-runtime/triton-cache
export PYTHONPYCACHEPREFIX=/mnt/e/aira-local-runtime/pycache
export LLAMA_CACHE=/mnt/e/aira-local-runtime/llama-cache
EOF
chmod 700 /mnt/e/aira-local-runtime/activate.sh
source /mnt/e/aira-local-runtime/activate.sh
cd /mnt/e/female-voice-ai
python -m pip install --upgrade pip
python -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
python -m pip install -r services/local_voice_api/requirements-runtime.txt
python -m pip install -r services/local_voice_api/requirements-stt.txt
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CUDA unavailable')"
```

Do not install `flash-attn`; neither runtime requires it. Reference design keeps
its existing `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign` runtime settings. Approved
voice synthesis uses `Qwen/Qwen3-TTS-12Hz-0.6B-Base` and selects its runtime by
CUDA compute capability: GPUs below major 8, including RTX 20-series/Turing
(7.5), use `torch.float32` with eager attention; GPUs at major 8 or newer use
`torch.float16` with PyTorch SDPA. RTX 20-series FP32 compatibility mode is
slower, but the 0.6B Base model is expected to fit within 8 GB VRAM.

## Milestone 1: voice reference design

Listing profiles is dependency-light: it does not import `torch` or `qwen_tts`
and does not access model weights.

```bash
source /mnt/e/aira-local-runtime/activate.sh
cd /mnt/e/female-voice-ai
python services/local_voice_api/tools/generate_voice_reference.py --list
```

**Do not run the following final generation command during implementation or
lightweight testing.** It invokes `from_pretrained`, which downloads model
weights to the E-drive Hugging Face cache on first use:

```bash
python services/local_voice_api/tools/generate_voice_reference.py --companion aanya --output-dir /mnt/e/aira-local-runtime/generated/voices --seed 20260830
```

The default companion is `aanya`. The default output directory is
`/mnt/e/aira-local-runtime/generated/voices`, and the default seed is
`20260830`.

Each successful run writes `<companion>_seed_<seed>.wav` and a matching JSON
file. Metadata records the companion ID, transcript, voice description, model
ID, requested seed, returned sample rate, AI-generated and adult flags, and a
UTC creation timestamp.

## Milestone 2: approve and reuse a reference

Approval is explicit. The checked-in
`config/approved_voice_references.json` manifest contains relative filenames,
never machine-specific absolute paths. It initially approves only these exact
files for Aanya:

```text
aanya_seed_20260831.wav
aanya_seed_20260831.json
```

Place both files in `/mnt/e/aira-local-runtime/generated/voices`. The runtime
selects only the manifest filenames; it never scans for or silently chooses the
newest reference. Before model loading it verifies the companion ID, seed,
adult and AI-generated flags, nonempty transcript, VoiceDesign model ID,
sample rate, metadata filename, and WAV filename and contents.

List approvals without importing PyTorch, SoundFile, or Qwen:

```bash
source /mnt/e/aira-local-runtime/activate.sh
cd /mnt/e/female-voice-ai
python services/local_voice_api/tools/synthesize_companion_voice.py --list-approved
```

**The first synthesis downloads
`Qwen/Qwen3-TTS-12Hz-0.6B-Base` into the E-drive Hugging Face cache.** It is not
downloaded by listing approvals or running lightweight tests.

```bash
python services/local_voice_api/tools/synthesize_companion_voice.py --companion aanya --text "I'm glad you called. Tell me what happened, and we can think through it together." --reference-dir /mnt/e/aira-local-runtime/generated/voices --output-dir /mnt/e/aira-local-runtime/generated/runtime --seed 20260901
```

Synthesis accepts 1 to 500 characters. It creates a WAV and matching JSON with
the companion ID, synthesized text, Base model ID, approved reference WAV and
seed, output seed, returned sample rate, resolved dtype, attention backend,
CUDA compute capability, adult and AI-generated flags, and UTC timestamp.

Within a long-running Python process, the synthesis module keeps one Base model
and one reusable voice-clone prompt per validated companion. Call
`shutdown_voice_clone_runtime()` once at process shutdown to release prompt and
model references and clear CUDA cache. Shutdown is terminal for that process.
After a CUDA device-side assertion, do not retry synthesis in that process;
change the Base precision/runtime settings and start a fresh process.

## Milestone 3: private local speech-to-text

Short conversational turns are transcribed locally with Faster-Whisper
`base.en`. The reusable runtime uses CPU INT8 inference with six CPU threads and
one worker, leaving the GPU and its VRAM available for Qwen TTS. It requests
English, beam size 1, VAD filtering, and disables conditioning on previous text.

Install the STT dependency separately so the existing Qwen/PyTorch environment
is not replaced unnecessarily:

```bash
source /mnt/e/aira-local-runtime/activate.sh
cd /mnt/e/female-voice-ai
python -m pip install -r services/local_voice_api/requirements-stt.txt
```

Inspect all fixed settings and paths without importing Faster-Whisper:

```bash
python services/local_voice_api/tools/transcribe_audio.py --show-config
```

**The first real transcription downloads `base.en` into
`/mnt/e/aira-local-runtime/models/faster-whisper`.** Configuration display and
lightweight tests do not download or run the model.

```bash
python services/local_voice_api/tools/transcribe_audio.py --audio /mnt/e/aira-local-runtime/generated/runtime/aanya_runtime_seed_20260902_fc889e0ad1ec.wav --output-dir /mnt/e/aira-local-runtime/generated/transcripts --model-dir /mnt/e/aira-local-runtime/models/faster-whisper --cpu-threads 6
```

The runtime validates and hashes the source locally before model loading. It
never uploads or copies the source audio. A successful run writes only a UTF-8
transcript text file and JSON metadata containing provenance, language,
duration, timestamped segments, runtime settings, and a UTC timestamp under the
E-drive transcript directory. Call `shutdown_transcription_runtime()` once at
process shutdown to release the cached model; shutdown is terminal for that
process.

## Milestone 4C: one-command local conversation turn

The conversation tool reuses the CPU Faster-Whisper runtime, the existing
cached `Qwen3-1.7B-Q4_K_M.gguf` through llama.cpp, and the exact approved Aanya
reference `aanya_seed_20260831.wav`. It does not scan for a newer voice
reference, use a network LLM endpoint, or download an LLM. llama.cpp runs with
offline mode, Qwen thinking disabled, and FlashAttention disabled. Approved
voice synthesis retains the existing automatic RTX 2070/Turing
`float32`/`eager` configuration.

Run the first complete Aanya turn from WSL with this exact one-command example:

```bash
cd /mnt/e/female-voice-ai && source /mnt/e/aira-local-runtime/activate.sh && python services/local_voice_api/tools/run_local_conversation.py --companion aanya --audio /mnt/e/aira-local-runtime/input/amman_test.wav
```

Each run creates a unique directory below
`/mnt/e/aira-local-runtime/generated/conversations`. The final WAV, component
STT/TTS provenance, and `turn.json` remain there. `turn.json` records the input
path, raw and Aanya-normalized transcripts, clean assistant response, output
WAV, exact STT/LLM/TTS provenance, segment timestamps, UTC stage timestamps,
and available audio/stage durations. Transcript and audio content are sensitive;
keep the E drive encrypted at rest and delete test turns when they are no longer
needed.

Milestone 4C currently has a persona and an approved voice only for `aanya`.
Name normalization changes the whole-word STT variants `Anna`, `Anya`, and
`Ana` to `Aanya` only while Aanya is the active companion.

## Milestone 5A: trusted local HTTP bridge

Milestone 5A exposes the existing one-turn orchestration to a future Android
push-to-talk client. The API does not recreate or alter STT, llama.cpp, or TTS:
it stores a validated upload and calls `run_conversation_turn()` once. Flutter
is not connected yet.

Install only the HTTP bridge dependencies into the existing E-drive runtime:

```bash
cd /mnt/e/female-voice-ai && source /mnt/e/aira-local-runtime/activate.sh && python -m pip install -r services/local_voice_api/requirements-api.txt
```

Start the single-worker development server on the required host and port:

```bash
cd /mnt/e/female-voice-ai && source /mnt/e/aira-local-runtime/activate.sh && python services/local_voice_api/tools/run_local_api.py --host 0.0.0.0 --port 8765
```

`0.0.0.0` listens on every local interface. This server has no authentication
or TLS and is only for a trusted local-network development setup. Never expose
or port-forward port 8765 to the public internet, do not use it on an untrusted
network, and keep the host firewall enabled. For same-machine testing only,
override the bind address with `--host 127.0.0.1`.

In another WSL shell, check the dependency-light health endpoint. It does not
load Whisper, llama.cpp, Qwen TTS, CUDA, or model files:

```bash
curl --fail-with-body http://127.0.0.1:8765/health
```

Expected JSON:

```json
{"status":"ok","service":"aira-local-voice-api"}
```

Run the first real HTTP Aanya turn with:

```bash
curl --fail-with-body -X POST http://127.0.0.1:8765/v1/conversation/turn -F 'companion=aanya' -F 'audio=@/mnt/e/aira-local-runtime/input/amman_test.wav;type=audio/wav'
```

A successful response contains the safe turn ID, raw and normalized transcript,
plain assistant response, explicit AI disclosure, and a process-local audio URL:

```json
{
  "ai_disclosure": "Aanya is an adult fictional AI companion, not a human.",
  "turn_id": "aanya_turn_...",
  "companion": "aanya",
  "raw_transcript": "...",
  "normalized_transcript": "...",
  "response": "...",
  "audio_url": "/v1/conversation/turns/aanya_turn_.../audio"
}
```

Uploads are streamed with a 50 MiB limit into uniquely named files under
`/mnt/e/aira-local-runtime/incoming`; supplied filenames are never used as
filesystem paths. Extensions, media types, and basic file signatures are
checked before model work. Partial, rejected, and failed-turn uploads are
removed. Successful uploads remain beside the existing generated conversation
artifacts so `turn.json` provenance stays valid. Treat all of these files as
sensitive and keep the E drive encrypted at rest.

Generated WAV paths are stored in a locked in-memory registry and never accepted
from an HTTP request. Audio URLs therefore work only for turns completed by the
current server process and return 404 after a restart. Turn IDs are validated
and never joined to a request-supplied filesystem path.

Known Faster-Whisper no-speech results return HTTP `422` with
`error: "no_speech"`; unexpected runtime failures retain the path-safe generic
`500` response.

## Realtime foundation: readiness, warmup, and WebSocket v1

`GET /health` remains the exact dependency-light liveness check documented
above. `GET /ready` is separate, returns `Cache-Control: no-store`, and reports
safe process-wide state for `stt`, `llm`, and `tts`. It returns `200` only when
all configured warmup steps completed and `503` for `starting`, `warming`,
`degraded`, or `failed`.

Expensive startup warmup is **off by default**. Opt in explicitly for a local
server process:

```bash
cd /mnt/e/female-voice-ai
source /mnt/e/aira-local-runtime/activate.sh
AIRA_MODEL_WARMUP=1 python services/local_voice_api/tools/run_local_api.py --host 0.0.0.0 --port 8765
```

Warmup runs once in a worker thread without blocking liveness. With no realtime
provider configured, it loads the CPU Faster-Whisper runtime, validates the
local llama.cpp executable and cached GGUF without launching inference, then
loads Qwen Base and creates the approved Aanya voice-clone prompt. Shutdown
waits for an in-progress in-process model load because Python thread
cancellation cannot safely stop it. Do not enable this switch during routine
tests.

### Pocket TTS realtime worker

Pocket TTS 3.1.0 runs in its existing dedicated Windows Python 3.13 / PyTorch
2.14 CPU environment. The WSL backend does not import it. A persistent,
loopback-only worker loads the model once, loads the cached Aanya safetensors
state once, and exposes health, readiness, cancellation, and length-prefixed
streaming PCM. The worker never binds to a non-loopback address and the backend
rejects a non-loopback worker URL. This is a local-development trust boundary,
not a public service.

Start the worker in Windows PowerShell:

```powershell
$env:PYTHONPATH="E:\female-voice-ai\services\local_voice_api\src"
$env:AIRA_POCKET_TTS_VOICE_STATE="E:\aira-local-runtime\pocket-tts\aanya_voice.safetensors"
$env:AIRA_POCKET_TTS_CACHE_ROOT="E:\aira-local-runtime\pocket-tts\cache"
$env:AIRA_POCKET_TTS_OFFLINE="1"
& "E:\aira-local-runtime\venvs\pocket-tts\Scripts\python.exe" -m local_voice_api.pocket_tts_worker --host 127.0.0.1 --port 8766
```

The HTTP and realtime paths remain unchanged unless the provider is explicitly
selected. Start the WSL backend only after the worker reports ready:

```bash
curl --fail-with-body http://127.0.0.1:8766/ready
cd /mnt/e/female-voice-ai
source /mnt/e/aira-local-runtime/activate.sh
AIRA_REALTIME_TTS_PROVIDER=pocket_worker \
AIRA_POCKET_TTS_WORKER_URL=http://127.0.0.1:8766 \
AIRA_MODEL_WARMUP=1 \
python services/local_voice_api/tools/run_local_api.py --host 0.0.0.0 --port 8765
```

This localhost path requires WSL networking that can reach the Windows
loopback service (for example WSL mirrored networking). It intentionally does
not fall back to a LAN bind. If `/ready` fails from WSL, fix that local
networking boundary rather than exposing the unauthenticated worker publicly.

With the Pocket provider selected, the `tts` warmup component checks that the
worker has both its model and cached Aanya state loaded and is reporting 24 kHz
mono PCM16. A missing, loading, failed, or format-incompatible worker keeps
backend readiness unavailable. `/ready` and each realtime handshake repeat the
lightweight worker status/format probe, so a worker that exits after warmup is
not advertised as currently usable. The Qwen HTTP conversation implementation
is unchanged and remains available as before.

The versioned endpoint is `ws://<trusted-host>:8765/v1/realtime`. The first text
frame must be `session_start` with `protocol_version: 1`, `companion: "aanya"`,
and mono 16 kHz `pcm_s16le`. Microphone samples then use binary frames, followed
by versioned `end_of_turn`, `cancel_turn`, or `session_end` text events. A turn
is limited to 2 MiB and each binary frame to 64 KiB.

`session_ready` includes the explicit AI disclosure, the input format, the full
readiness snapshot, and `can_process_turns`. A server without both ready models
and an enabled inference processor reports `can_process_turns: false` and closes
with WebSocket code `1013`; this is observable startup/unavailability, not a
claim that inference is ready. Server turn events are `stt_partial`, `stt_final`,
`thinking`, `text_delta`, `text_sentence`, `speaking`, `audio_chunk`,
`turn_complete`, `recoverable_error`, and `fatal_error`. Each `audio_chunk` JSON
header is immediately followed by its binary PCM16 frame. Output is mono PCM16
at a declared 8–48 kHz rate, starts at sequence zero, and remains bounded to 64
KiB per frame.

The production app still uses the existing HTTP Aanya screen. No realtime
processor is enabled by default. Explicitly selecting `pocket_worker` composes
the existing bounded batch STT and complete-response llama.cpp adapters with
the streaming Pocket synthesizer; no WebSocket protocol or Flutter behavior is
changed. Pocket audio is forwarded as 24 kHz mono PCM16 as soon as each genuine
`generate_audio_stream` chunk arrives. The worker is single-flight and rejects
concurrent synthesis instead of creating an unbounded queue.

Cancellation invalidates the adapter generation, closes the active stream, and
sends the active turn ID to the worker. Both sides check cancellation between
chunks, so already-cancelled/stale chunks are not forwarded to a new turn. The
underlying Pocket call cannot be force-preempted while it is inside one model
step, but the worker remains reusable after its generator unwinds.

HTTP turns emit structured `[AIRA TIMING]` logs for request parsing, upload
validation/write, audio readiness, STT, normalization, LLM, TTS, result
validation, response construction, metadata writing, and totals when those
boundaries are reached. Realtime `turn_complete.metrics` measures STT final,
TTFT, first speakable text, first synthesized sample (`ttfas_ms`), first binary
audio successfully sent (`ttfa_ms`), and total time from server `end_of_turn`.
Logs contain generated IDs and numeric/safe fields, not audio or transcript text.

The existing Qwen measurements remain: cold model load 202.8 seconds, cold full
conversation about 329 seconds, and warm full conversation 104.956 seconds.
The separate Pocket benchmark measured 128.86/130.81/152.59 ms
min/median/max first playable TTS audio, 106.69 ms for the short phrase, and a
median RTF of about 0.394. Those are TTS-only Windows CPU measurements, not an
end-to-end Aira TTFA result. STT, process-per-turn llama.cpp, phrase availability,
IPC, WebSocket delivery, and Android playback still must be measured together.

For a manual end-to-end smoke test, provide a non-sensitive uncompressed 16 kHz
mono PCM16 input WAV and run this after both readiness checks return HTTP 200:

```bash
cd /mnt/e/female-voice-ai
source /mnt/e/aira-local-runtime/activate.sh
curl --fail-with-body http://127.0.0.1:8765/health
curl --fail-with-body http://127.0.0.1:8765/ready
python services/local_voice_api/tools/smoke_realtime_websocket.py \
  /mnt/e/aira-local-runtime/incoming/realtime-smoke-input.wav \
  /mnt/e/aira-local-runtime/generated/realtime-smoke-output.wav
```

The diagnostic prints the client-observed first binary arrival and the server's
`ttfas_ms`/`ttfa_ms` metrics, then writes the streamed output WAV for listening.
It uses only the standard library and performs real STT, LLM, and TTS; do not run
it as part of automated tests.

## Lightweight verification

These checks use only the Python standard library and never import the model
runtime:

```bash
source /mnt/e/aira-local-runtime/activate.sh
cd /mnt/e/female-voice-ai
python -m json.tool services/local_voice_api/config/voice_profiles.json >/dev/null
python -m json.tool services/local_voice_api/config/approved_voice_references.json >/dev/null
python -m compileall -q services/local_voice_api
python -m unittest discover -s services/local_voice_api/tests -v
```
