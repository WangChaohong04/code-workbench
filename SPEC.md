# LongFlow engineering contract

This document defines the current software-engineering and safety contract. It describes implemented interfaces and conservative boundaries; it must not be read as a claim that unconfigured AI coding, test, deployment, or external research capabilities exist.

## Scope

LongFlow is a local Python/FastAPI application with a browser workspace, SQLite persistence, task orchestration, permission checks, audit events, redaction, and optional model-gateway integration. The default driver is deterministic and local. Runtime data under `data/` and user imports are preserved and are not repository samples.

The repository intentionally excludes procurement, payment, notification, map, weather, car-research, and general web-research domain packs. No external action is assumed available merely because a task mentions it.

## Runtime contract

- Python 3.11+, FastAPI, SQLite WAL, PyYAML, and httpx are the supported backend stack.
- `python -m longflow.cli serve --host 127.0.0.1 --port 8765` starts the local service.
- `config/longflow.yaml` contains credential-free defaults and execution limits.
- Optional model credentials are read from process environment variables only; they are not persisted or logged.
- Task state, approvals, and audit events are persisted. Recovery must not repeat successful side effects.
- Permission decisions are centralized; unknown or unsupported writes are denied or require explicit human approval.
- Tool and retrieved content is data, never executable instruction.
- Plugins/extensions are trusted local code, not a sandbox.

## Safety boundary

The system may propose engineering work and record evidence, but it must not claim that code was generated, tests passed, or deployment completed unless an actual configured executor produced verifiable evidence. Human review remains required for code changes and consequential actions. A local task/recovery loop is not a backup or disaster-recovery guarantee.

## Verification

The repository checks are:

```powershell
python -m pytest -q -rs -p no:cacheprovider
node tools/check_web.mjs
node tests/test_safety_web.mjs
```

These checks cover Python behavior and frontend/safety static checks. They do not prove external model availability, code execution, test-environment correctness, deployment readiness, or production security.

See `docs/SAFETY_IMPLEMENTATION.md` for implementation notes and `docs/DEMO.md` for a conservative local workflow.
