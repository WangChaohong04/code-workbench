# LongFlow Workbench

LongFlow is a local software-engineering workbench for coordinating coding, test, and deployment-safety workflows. It provides a recoverable task graph, evidence and approval boundaries, audit events, and a browser workspace. This repository does **not** claim to implement an autonomous coding agent, test runner, deployment system, or external research service unless the corresponding runtime is explicitly configured and verified.

## Setup

Requirements: Python 3.11+, Git, and Node.js 22+ for static web checks.

```powershell
python -m pip install -r requirements.txt
python -m longflow.cli serve --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765/>. The default configuration is local and credential-free. Runtime data is stored under `data/`; preserve that directory when cleaning or upgrading a checkout.

## Configuration

`config/longflow.yaml` contains the local model gateway, server, and execution limits. API credentials, when an explicitly supported integration requires them, must be supplied through environment variables and must never be committed. `.env.example` documents only the currently supported model/database variables; it is not loaded automatically.

The repository intentionally contains no procurement, payment, notification, map, weather, car-research, or general web-research domain packs. No removed sample should be interpreted as a supported product capability.

## Checks

```powershell
python -m pytest -q -rs -p no:cacheprovider
node tools/check_web.mjs
node tests/test_safety_web.mjs
```

These checks cover the remaining Python behavior plus frontend and safety static checks. They do not prove that an external model, code executor, test environment, deploy target, or third-party service is configured.

## Boundaries and security

The application is intended for a local, single-machine environment. Keep the server on loopback unless you have separately designed authentication, TLS, authorization, and operational controls. Plugins or extensions are trusted local code, not a sandbox. Review [SECURITY.md](SECURITY.md) and [docs/SAFETY_IMPLEMENTATION.md](docs/SAFETY_IMPLEMENTATION.md) before enabling new integrations.

The internal design contract is [SPEC.md](SPEC.md). Do not commit credentials, runtime databases, logs, user imports, or generated artifacts.
