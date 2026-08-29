# Female Voice AI

Female Voice AI is an Android-first subscription app for a conversational AI companion with a natural female voice, low-latency interactions, scheduled conversations, and consent-based long-term memory.

This repository currently contains the Android Flutter client at `apps/mobile`, with the plan to add a Python FastAPI service, PostgreSQL with pgvector, Redis, and LiveKit/WebRTC for real-time voice sessions.

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

- Android-only Flutter application scaffold exists in `apps/mobile`.
- The app is expected to build and run on Android devices and emulators.
- This task does not change the Flutter UI; it focuses on repository-level product, architecture, and governance documentation.

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
- Do not commit during this documentation-only phase.

## Documentation

- [docs/architecture.md](docs/architecture.md)
- [docs/mvp-plan.md](docs/mvp-plan.md)
- [AGENTS.md](AGENTS.md)
