# Architecture Overview

## Product intent

This repository supports an Android-first AI voice companion that is clearly identified as an AI product. It provides conversational voice experiences, scheduled check-ins, and optional memory with explicit user consent. The product must not misrepresent itself as human, romantic, therapeutic, or emergency support.

## System architecture

### 1. Android client

The Flutter app in `apps/mobile` is the primary user interface. It handles:

- onboarding and subscription state
- conversation session lifecycle
- voice recording and playback
- scheduling and reminders
- consent toggles for memory
- display of AI disclosure language

The client should stay Android-only and should not introduce other desktop or web targets.

### 2. API gateway

A FastAPI service provides the application backend. It is responsible for:

- user authentication and profile management
- subscription enforcement and entitlement checks
- session orchestration
- consent and memory flow management
- scheduling jobs and reminders
- safe prompt assembly and policy checks

### 3. Real-time audio layer

LiveKit/WebRTC carries the live audio connection for voice conversations. It supports low-latency streaming between the Android client and the backend. The same layer should support both real-time conversation and scheduled check-ins.

### 4. Model and adapter layer

The system uses replaceable adapters for:

- speech-to-text (STT)
- LLM / conversational reasoning
- text-to-speech (TTS)

Each adapter is interface-based so the implementation can evolve from mock/local mode to self-hosted Whisper, Qwen, and a licensed custom female voice later.

### 5. Data and memory layer

PostgreSQL with pgvector stores structured user data and vectorized memory embeddings.

Redis is used for:

- session caching
- rate limits and concurrency control
- job queueing for scheduled conversations
- ephemeral state for active streaming sessions

### 6. Memory and consent management

Long-term memory is optional and must be user-initiated. The system should:

- ask for explicit consent before storing preferences, memories, or conversational summaries
- separate memory storage from immediate session context
- allow deletion of stored memories by user request
- provide an auditable recall and deletion trail
- minimize retention to what is necessary for user benefit

## Data flow

1. User opens the Android app and authenticates.
2. The app creates a conversation session with subscription metadata and user consent state.
3. The client streams audio to the backend through LiveKit/WebRTC.
4. The backend routes speech to the STT adapter.
5. The conversation service sends text to the LLM adapter with policy and safety constraints.
6. The TTS adapter converts the reply into audio.
7. The app plays the audio back to the user.
8. If consent exists, selected data may be summarized and stored in vector memory for future personalization.

## Local development mode

During local development, the system should run in a mock mode that avoids paid APIs. In this mode:

- voice capture can be simulated
- text responses can be deterministic local stubs
- TTS output can be a placeholder or generated local sample
- the infrastructure can be run without cloud billing

This preserves a development path toward later self-hosted Whisper, Qwen, and licensed custom TTS.

## Product safety and disclosures

The system must ensure:

- every user-facing conversation clearly identifies the assistant as AI
- no claims of being human, romantic partner, therapist, or emergency responder
- emotional support boundaries that avoid dependency or manipulation
- crisis-resource escalation when the user appears in danger or crisis

## Security and privacy requirements

- Use TLS for all internet communication.
- Encrypt sensitive data at rest.
- Restrict access to memory and profile data using least-privilege permissions.
- Store secrets in environment variables or a managed secret store, never in source control.
- Keep deletion workflows explicit and easy to use.
- Log privacy-relevant actions without exposing raw content to unauthorized operators.

## Non-goals for this repository

- No iOS, web, Windows, macOS, or Linux app targets
- No paid external API dependencies during local development
- No deployment or production hosting in this documentation phase
