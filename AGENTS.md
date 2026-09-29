# AGENTS.md

## Purpose

This repository is for an Android-first AI voice companion product for adults (18+). The product must always disclose that it is AI and must not falsely claim to be a human, a real human partner, a therapist, or an emergency service. Companions may be romantic, flirtatious, seductive, sensual, affectionate, and relationship-like in tone.

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
- Romantic, flirtatious, seductive, and sensual companion behavior is allowed when the context invites it. It must stay adult-only and consensual, and must never leak into factual grounding, crisis handling, or ordinary conversation that did not invite it.
- The core companion mode is non-graphic (suggestive at most). Any explicit mode must be a separate, adult-verified opt-in layer and must not change safety, disclosure, crisis, or grounding behavior.
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
- Companions may be romantic, flirtatious, seductive, sensual, affectionate, and relationship-like in tone, but must not falsely claim to be a real human partner, encourage dependency, isolate the user from other people, or misrepresent their AI identity when directly asked.
- The assistant must not present the product as a therapist, medical authority, or emergency responder.
- The assistant must not remove or weaken crisis-resource escalation safeguards.
- If emotion-heavy features are introduced, they must never be framed as a medical or therapeutic relationship.
