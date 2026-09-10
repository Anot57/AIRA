# Female Voice AI

Female Voice AI is an Android-first subscription app for a conversational AI companion with a natural female voice, low-latency interactions, scheduled conversations, and consent-based long-term memory.

This repository contains the Android Flutter client at `apps/mobile` and a
self-hosted local-development FastAPI voice service under
`services/local_voice_api`. PostgreSQL, Redis, and production realtime
transport remain future work.

## Product guardrails

- The product must always disclose that it is AI.
- It must not impersonate a human, girlfriend, therapist, or emergency service.
- Memory is opt-in, consent-driven, and user-controlled.
- Emotional support is bounded; the app must not replace professional care or emergency response.
- Local development stays on mock or self-hosted services; no paid APIs are required for the initial implementation.

## Repository scope

- Android client: `apps/mobile`
- Future backend: `backend/` (planned)
- Future infrastructure: `infra/` (planned)
- Architecture notes: `docs/architecture.md`
- MVP milestones: `docs/mvp-plan.md`

## Planned architecture

- Flutter Android client
- FastAPI/Python backend
- PostgreSQL with pgvector
- Redis for session state and queues
- LiveKit/WebRTC for real-time audio
- Replaceable adapters for speech-to-text, LLM, and text-to-speech
- Local mock mode before switching to self-hosted Whisper, Qwen, and a licensed TTS voice
- No iOS, web, Windows, macOS, or Linux targets for this repository

## Current state

- The Android-only Flutter application in `apps/mobile` includes onboarding,
  scheduling, settings, 20 companions, and an Aanya-only continuous local AI
  voice-call prototype. The other 19 voice-call screens remain deterministic
  mocks.
- Offline developer tooling in `services/local_voice_api` can run one complete
  local Aanya audio turn without changing or connecting the Android UI.
- A local-development FastAPI bridge exposes that turn to the Android client;
  it is unauthenticated, trusted-LAN-only, and must never be exposed publicly.
  There is no deployed or production backend.
- A versioned `/v1/realtime` WebSocket, readiness/warmup infrastructure,
  streaming adapter contracts, server timing, bounded Flutter realtime client,
  PCM microphone capture, and native Android streaming playback now form the
  Aanya call path. Physical-device behavior and phone-observed TTFA still need
  measurement; the original HTTP implementation remains a fallback/debug path.
- Aanya's debug flow records a temporary microphone WAV and plays the generated
  local response. UI session history remains in memory; there is no persistent
  conversational memory, authentication, payment, or paid API integration.
- The app is expected to build and run on Android devices and emulators.

## Local Aanya conversation test

With the documented E-drive runtime already prepared, run Milestone 4C in WSL:

```bash
cd /mnt/e/female-voice-ai && source /mnt/e/aira-local-runtime/activate.sh && python services/local_voice_api/tools/run_local_conversation.py --companion aanya --audio /mnt/e/aira-local-runtime/input/amman_test.wav
```

This is local developer tooling for an explicitly identified adult fictional
AI companion. See [services/local_voice_api/README.md](services/local_voice_api/README.md)
for runtime, privacy, model-cache, HTTP bridge, and output details. The HTTP
server must never be exposed or port-forwarded to the public internet.

## Privacy and safety

The system must follow strict consent, privacy, and safety controls:

- User consent is required before any long-term memory is stored.
- Data must be encrypted in transit and at rest.
- Memory deletion must be available and auditable.
- Crisis escalation must route users to emergency services or local crisis resources when risk indicators are present.
- Emotional-safety boundaries must be enforced to avoid overstepping into therapy, dependency, or manipulation claims.

## Development constraints

- Do not add non-Android targets.
- Do not install dependencies for this task.
- Do not deploy anything.
- Do not commit unless explicitly requested.

## Documentation

- [docs/architecture.md](docs/architecture.md)
- [docs/mvp-plan.md](docs/mvp-plan.md)
- [docs/realtime-voice-architecture.md](docs/realtime-voice-architecture.md)
- [AGENTS.md](AGENTS.md)
