# Local Voice Lab - milestone 1

This workspace designs short reference clips for the 20 checked-in Android
companion IDs. Every profile is an original fictional adult AI voice, every
reference transcript identifies itself as AI, and no profile is intended to
copy an existing person's voice.

Milestone 1 is an offline preparation tool only. It does not add an API server,
connect to the Flutter app, or change the call screen.

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
/mnt/e/aira-local-models/huggingface
/mnt/e/aira-local-models/torch
```

The CLI refuses generation when its output or required runtime environment
variables point away from E drive.

## WSL setup

These commands assume Ubuntu 24.04 in WSL and Python 3.12. Confirm that
`nvidia-smi` works in WSL before installing the Python runtime.

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv libsndfile1 sox
mkdir -p /mnt/e/aira-local-runtime/cache /mnt/e/aira-local-runtime/pip-cache /mnt/e/aira-local-runtime/tmp /mnt/e/aira-local-runtime/generated/voices
mkdir -p /mnt/e/aira-local-models/huggingface/hub /mnt/e/aira-local-models/torch
python3.12 -m venv /mnt/e/aira-local-runtime/.venv
cat > /mnt/e/aira-local-runtime/activate.sh <<'EOF'
#!/usr/bin/env bash
source /mnt/e/aira-local-runtime/.venv/bin/activate
export HF_HOME=/mnt/e/aira-local-models/huggingface
export HF_HUB_CACHE=/mnt/e/aira-local-models/huggingface/hub
export HF_XET_CACHE=/mnt/e/aira-local-models/huggingface/xet
export HF_ASSETS_CACHE=/mnt/e/aira-local-models/huggingface/assets
export HF_MODULES_CACHE=/mnt/e/aira-local-models/huggingface/modules
export HF_TOKEN_PATH=/mnt/e/aira-local-models/huggingface/token
export TORCH_HOME=/mnt/e/aira-local-models/torch
export XDG_CACHE_HOME=/mnt/e/aira-local-runtime/cache
export PIP_CACHE_DIR=/mnt/e/aira-local-runtime/pip-cache
export TMPDIR=/mnt/e/aira-local-runtime/tmp
export CUDA_CACHE_PATH=/mnt/e/aira-local-runtime/cache/nvidia
export NUMBA_CACHE_DIR=/mnt/e/aira-local-runtime/cache/numba
export TORCHINDUCTOR_CACHE_DIR=/mnt/e/aira-local-runtime/cache/torchinductor
export TRITON_CACHE_DIR=/mnt/e/aira-local-runtime/cache/triton
export PYTHONPYCACHEPREFIX=/mnt/e/aira-local-runtime/cache/pycache
EOF
chmod 700 /mnt/e/aira-local-runtime/activate.sh
source /mnt/e/aira-local-runtime/activate.sh
cd /mnt/e/female-voice-ai
python -m pip install --upgrade pip
python -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
python -m pip install -r services/local_voice_api/requirements-runtime.txt
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CUDA unavailable')"
```

Do not install `flash-attn`. The tool uses standard PyTorch SDPA when available
and selects eager attention when SDPA is unavailable. It always loads exactly
one model, `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign`, with `torch.float16` on
`cuda:0`.

## Workflow

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

## Lightweight verification

These checks use only the Python standard library and never import the model
runtime:

```bash
source /mnt/e/aira-local-runtime/activate.sh
cd /mnt/e/female-voice-ai
python -m json.tool services/local_voice_api/config/voice_profiles.json >/dev/null
python -m compileall -q services/local_voice_api
python -m unittest discover -s services/local_voice_api/tests -v
```
