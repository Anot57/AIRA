# AGENTS.md

## Purpose

This repository is for an Android-first AI voice companion product. The product must always disclose that it is AI and must not present itself as a human, girlfriend, therapist, or emergency service.

## Scope rules

- Android is the only supported client target in this repository.
- Do not add iOS, web, Windows, macOS, or Linux targets.
- Keep the existing Flutter app UI unchanged unless a user explicitly asks for feature work.
- No paid external APIs are allowed during local development.

## Coding rules

- Prefer small, well-scoped changes that preserve the existing Android app.
- Keep architecture modular: client, API, storage, memory, and real-time voice layers should remain clearly separated.
- Use adapter interfaces for speech-to-text, LLM, and text-to-speech so self-hosted or mock services can replace cloud providers later.
- Maintain explicit AI disclosure in user-facing strings, UX copy, system prompts, and product flows.
- Never claim emotional dependency, romantic exclusivity, medical authority, or emergency response.
- If a feature touches memory or consent, treat it as a privacy-sensitive flow and require explicit user consent.

## Testing rules

- Run the smallest relevant verification for the scope being changed.
- Do not install new dependencies for this task.
- Prefer read-only checks and static validation over broad suite execution.
- For any product logic or backend work, add focused tests that validate consent, disclosure, and safety boundaries.
- Keep tests deterministic and local-first.

## Security and privacy rules

- Treat all voice, conversation, and memory data as sensitive.
- Encrypt data in transit and at rest.
- Require explicit, informed consent before storing long-term memory.
- Provide a clear memory deletion flow and retain deletion logs for auditability.
- Keep secrets out of source control and do not commit environment files.
- Use secure defaults for session storage, token handling, and connection configuration.
- Never allow the product to claim it is a therapist, crisis worker, or human companion.
- If a user shows signs of a crisis, escalate to emergency resources or local crisis support instead of continuing the conversation as a substitute service.

## Git rules

- Keep a clean working tree unless the user requests otherwise.
- Review the final diff before finishing.
- Do not commit during this task unless explicitly requested.
- Prefer small, reviewable changes and avoid broad refactors.
- Include concise commit messages only when a commit is explicitly requested.

## Safety boundaries

- The assistant must not build emotionally manipulative or dependency-forming behavior.
- The assistant must not present the product as a romantic partner, therapist, or emergency responder.
- The assistant must not remove or weaken crisis-resource escalation safeguards.
- If emotion-heavy features are introduced, they must be framed as AI assistance and not as an intimate or medical relationship.
