# Engineering Capability Test Matrix

This matrix distinguishes shipped adapters from available runtimes. Passing mocks does not verify a real model, Docker isolation, remote Git, CI/CD, production deployment or a production database.

## Layers

| Capability | Offline/unit evidence | Real integration prerequisite | Production permission |
|---|---|---|---|
| Folder/read/edit/diff/version restore | Local workspace + API suite | Local Git and interactive Tk | Explicit exact-diff approval before source writes |
| AI project understanding/planning/code proposal | Structured output, scope and injection rejection | Configured OpenAI-compatible endpoint, code transmission consent | Never grants apply authority |
| Long-running checkpoints | Persistent state, CAS attempts, pause/stop/restart tests | Model + approved execution adapter | Wait for each exact source apply |
| Test/static checks | Mock Docker lifecycle, command restrictions | Docker, reviewed digest-pinned image containing pytest/ruff | Explicit candidate+command authorization |
| Git branch/merge/push | Proposal hashes/epoch/expiry/policy tests | Trusted native Git repository and reviewed remote | Separate explicit approval; push default closed |
| CI/CD/deployment | Unconfigured rejection tests | Reviewed provider adapter + environment/credential inventory | Separate production approval |
| Database migration/restore | Unconfigured rejection tests | Identified database, backup proof, migration/restore verifier | Separate destructive-operation approval |
| Crash recovery | Journal drift/freeze/preview tests | Disposable local workspace interrupted transaction | Never silently modify original without recovery consent |

## Current Execution Boundaries

No Docker executable was detected during implementation. The default model driver is local (deterministic rules), not an AI coding model. No CI provider, deployment target or production database has been identified. These capabilities must remain not_connected/blocked until explicitly configured; do not substitute host shell execution or fabricate success.

## Test Commands

The test suite is retained, not automatically executed. On your own machine, from the repository root:

```powershell
python -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File tools/test_engineering.ps1 -Suite All
```

Select a smaller group with `-Suite Core`, `AI`, `Runs`, `Sandbox`, `Operations` or `Production`. The default suite uses mocked model HTTP (not a real model), mocked Docker transport (not real container isolation), disposable local Git repositories (not remote push), and temporary SQLite/workspace data. It does not start Docker, call a model provider, push a real remote or connect to production.

For full existing regression, run separately:

```powershell
python -B -m pytest -q -rs -p no:cacheprovider --basetemp=.lfwork/manual-full-unique
```

Use a new basetemp name on every run; do not point it at user data. Pytest removes its temporary directory. The final output must be inspected for `failed`, `errors` and `skipped`. A skipped real integration test is not a verified capability.

### Delivery Status

- AI analyze/plan/propose endpoints are wired into the local workspace API, with code-transmission consent and source/epoch binding. File-workspace controls expose analysis, planning and exact-scope proposals.
- Durable run create/read/confirm/step/pause/resume/stop/retry endpoints are wired. Advancement is explicit and manual; no background autonomous worker has been installed. Missing executor blocks verification. The record-applied endpoint verifies server-derived durable application receipts and actual local snapshot evidence, never client-reported success.
- Docker execution now has explicit candidate approval, single-use execution approval, bounded evidence persistence and owner-scoped evidence reads. It remains disabled unless a trusted digest-pinned configuration is supplied.
- Restricted native Git operations expose preview/execute API routes and require a trusted native repository exactly matching the selected source. Operation-route regressions are maintained in `tests/test_operations_api.py`; the compatibility module is intentionally not part of the runner. Journal recovery has its own LocalWorkspace-only preview/confirmation adapter and does not require native Git configuration. Local durable candidate formats are supported by the Docker snapshot adapter.
- Git/recovery and read-only production capability panels are wired into the engineering console. Production capability API tests cover empty adapters, factory injection rejection, non-mapping/error/malformed or non-whitelisted capability-item status fallback, and deployment-only/database-only callback mappings, including the HTTP status and wired/automatic-execution flags. CI/CD, production deployment and database migration have injectable proposal/approval/evidence contracts in production_adapters, default `not_connected`, not actual provider implementations. Database restore has an exact backup/recovery-point proposal contract and explicit HTTP approval routes, but no actual provider. Production proposal routes are wired only for an injected trusted ProductionAdapters instance; they require explicit per-proposal confirmation and provider evidence. An empty injected adapter is still reported as not wired. Deployment, migration and restore routes are owner/workspace scoped and burn confirmed attempts before provider calls. Production test callbacks are mocks only. The HTTP production adapter is opt-in via the exact `state.production_adapters` instance; configuration alone cannot create provider access. Git previews remain `not_connected` until trusted native repository configuration is supplied; recovery previews require an actual pending journal.
- General Git merges are unsupported; only fast-forward inactive-ref updates are implemented by the native adapter.
- Crash recovery requires new explicit approval; it is not automatic unapproved source rewriting.

A prior test run reported 144 passed, 5 failed, 1 skipped before the latest adapter, API and UI changes. Those reported failures were addressed and new callback/API/UI tests were added, but the revised suite has not been rerun. Do not call the current suite passing without a fresh manual run.

Run unit and API suites with pytest using a fresh workspace-local temporary directory when Windows temp access is restricted. Git tests must only use disposable repositories. Real integration cases are opt-in and must skip with concrete missing prerequisites rather than count simulated results as real.

Do not run a production migration, restore, deployment or remote push as part of the default test suite. A test environment with disposable resources is required for provider validation.
