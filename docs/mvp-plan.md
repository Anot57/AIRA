# MVP plan

## Goal

Deliver a minimal, testable Android-first AI voice companion that clearly identifies itself as AI, supports short voice conversations, handles scheduled conversations, and respects consent-based memory.

## Milestone 1: repository and product guardrails

**Scope**

- confirm Android-only repository scope
- create architecture and repo governance docs
- define AI disclosure and safety guardrails
- ensure memory and crisis boundaries are explicitly documented

**Acceptance criteria**

- repo includes root README, AGENTS.md, architecture docs, and MVP plan
- product clearly states that it is AI
- no non-Android targets are introduced

## Milestone 2: Android app shell and consent flows

**Scope**

- keep the existing Flutter app functional and unchanged in UI style
- prepare the app structure for onboarding, consent, and session screens
- add clear AI disclosure messaging in product copy and configuration paths

**Acceptance criteria**

- user can review a consent screen before long-term memory is enabled
- app copy consistently states the assistant is AI
- no romantic, human, therapist, or emergency impersonation language is present

## Milestone 3: mock voice conversation backend

**Scope**

- set up the backend skeleton for FastAPI
- create a mock STT/LLM/TTS adapter pipeline
- validate session creation and request/response flow

**Acceptance criteria**

- a local mock conversation can run without paid APIs
- backend returns structured responses and session metadata
- conversation flow does not emit misleading identity claims

## Milestone 4: scheduled conversations and reminders

**Scope**

- define reminder scheduling service and queue model
- support scheduled voice or text prompts
- allow user-managed reminder preferences

**Acceptance criteria**

- a scheduled prompt can be created and stored
- a user can disable or delete a reminder
- reminders are clearly labeled as AI-generated

## Milestone 5: consent-based memory with deletion

**Scope**

- add memory storage behind explicit consent
- store only summarized data needed for personalization
- implement deletion endpoints and user-facing controls

**Acceptance criteria**

- memory is stored only after explicit consent
- user can delete memory records
- prompt history and memory views do not expose unexpected private data

## Milestone 6: safety and crisis handling

**Scope**

- enforce emotional-safety boundaries
- route crisis signals to appropriate resource guidance
- require AI disclosure in all outputs and flows

**Acceptance criteria**

- non-emergency assistance remains bounded and non-manipulative
- crisis indicators trigger resource guidance and do not masquerade as emergency response
- safety reviews catch any risky identity or dependency claims

## Definition of done for MVP

The MVP is complete when all of these are true:

- Android app shell and onboarding are in place
- mock voice conversation works locally
- scheduled conversations are supported
- memory is consent-based and deletable
- the product always identifies itself as AI
- emotional-safety and crisis-resource boundaries are enforced
- no paid APIs are required during local development

## Suggested test checks

Each milestone should include a small, testable validation such as:

- consent toggle is required before memory storage
- AI disclosure text appears in onboarding and session state
- mock backend responds without external credentials
- a deleted memory cannot be recalled
- crisis scenarios route to emergency or local crisis resources
