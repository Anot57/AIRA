# Local Voice Lab

This workspace designs short reference clips for the 20 checked-in Android
companion IDs and reuses explicitly approved clips for local speech synthesis.
Every profile is an original fictional adult AI voice, every reference
transcript identifies itself as AI, and no profile is intended to copy an
existing person's voice.

The Local Voice Lab is offline tooling only. It does not add an API server,
connect to the Flutter app, or change the call screen. Reference audio,
synthesis text, transcription input, and transcript output remain local and are
never uploaded by these tools. Because transcripts are intentionally saved as
plain UTF-8 text and JSON, the Windows E drive must use encryption at rest
(for example, BitLocker); files accessed through `/mnt/e` inherit that volume
protection.

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
mkdir -p /mnt/e/aira-local-runtime/generated/voices /mnt/e/aira-local-runtime/generated/runtime /mnt/e/aira-local-runtime/generated/transcripts /mnt/e/aira-local-runtime/generated/conversations /mnt/e/aira-local-runtime/models/faster-whisper /mnt/e/aira-local-runtime/llama-cache
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
