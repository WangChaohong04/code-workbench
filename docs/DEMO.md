# Engineering workflow demo

This document describes a local safety-oriented workflow only. It does not claim autonomous code generation, test execution, deployment, procurement, payment, notification, map, weather, or general web-research integrations.

## Start

```powershell
python -m pip install -r requirements.txt
python -m longflow.cli serve --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765/` and inspect the health/configuration views before creating a task. The default model driver is local and deterministic. An OpenAI-compatible model can be configured through process environment variables or `tools/api_keys_bootstrap.ps1`; external credentials are never stored by the repository.

## Local folder and version workflow

Open `http://127.0.0.1:8765/#files` for the local three-column file workspace. See [Local File Review and Version History](LOCAL_WORKSPACE.md) for native folder selection, staged additions/edits/deletions, exact-diff approval, external Git history and restore preview. Git and Python Tk support are required. Original files change only after explicit approval. Candidate records are isolated data, not execution sandboxes.

## Safe review loop

1. State a software-engineering objective and its constraints.
2. Confirm the proposed task graph and required approvals before any side effect.
3. Review evidence, tool results, and audit events; failed or unsupported actions must remain visibly failed or unsupported.
4. Treat generated code as a candidate for human review, not as an automatically applied change.
5. Keep runtime databases and imported user files under local runtime storage; do not commit them.

## Verification

```powershell
python -m pytest -q -rs -p no:cacheprovider
node tools/check_web.mjs
node tests/test_safety_web.mjs
```

These checks verify repository behavior and static safety checks only. They do not validate an external model, code executor, test environment, deployment target, or production security posture.
