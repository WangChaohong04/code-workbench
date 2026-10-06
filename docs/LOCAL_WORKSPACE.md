# Local File Review and Version History

## Scope

This local-only workbench associates a folder chosen through a native dialog on the service host. Read and review UTF-8 files; stage additions, edits and deletions into isolated durable candidate records; inspect exact unified diffs; explicitly approve and apply to the original directory. The candidate records are data isolation, not executable sandboxes. No tests, scripts, hooks, package installation or deployment commands execute.

Prerequisites: Python with Tk support, Git on PATH, service running on the same interactive desktop as the user. Keep the server bound to 127.0.0.1. A remote browser would open the dialog on the service host, not the browser computer. Only an authenticated local administrator can select folders. Existing project .git data is excluded and never rewritten.

## Walkthrough

1. Start `python -m longflow.cli serve --host 127.0.0.1 --port 8765` and open `http://127.0.0.1:8765/#files`.
2. Click the folder selection command and choose a disposable project outside the application's data storage. Cancellation does not associate a directory.
3. The left column lists files. Select a file to read its original contents in the central editor.
4. Edit text, add a relative file path, or stage deletion. These actions do not write the original directory.
5. Generate a candidate. Read the changed-file list and complete unified diff before approving.
6. Check the review checkbox, choose approve-and-apply, and explicitly confirm. The approval binds user, workspace, candidate, source hash, candidate hash, freeze epoch and a five-minute expiry. It is single-use.
7. Verify the original files and inspect version history. An external bare Git snapshot repository records the baseline and each applied version, without changing source Git branches or history.
8. Select any two versions to compare; preview a selected file at an old version. Restore preview creates a candidate showing current-to-old changes. It does not restore automatically.
9. Review the restore diff and explicitly approve-and-apply. Restoration becomes a new version; earlier history remains available.

## Safety Checks

- A changed source tree or modified candidate invalidates the exact approval.
- Global freeze prevents candidate generation, approvals and source writes. Unfreeze does not revive old approvals.
- Relative paths, symlinks, reparse points and hardlinks are guarded. Secrets, runtime stores, dependencies and .git directories are excluded.
- Initial limits: 2,000 files, 16 MiB aggregate, 1 MiB per file. UTF-8 text only; binary or oversized projects are rejected rather than silently edited.
- Applying multiple files is journaled, not atomic. If interrupted, the source may be partially changed and future applies are blocked. There is no automatic rollback and no user-facing transaction-recovery endpoint yet; do not delete the journal or claim recovery succeeded. Preserve storage and original files for explicit investigation.
- Portable path/hash checks cannot protect against a hostile process racing directory replacements. Use one service process and trusted local directories; do not share these roots with untrusted local writers.

## Regression Commands

```powershell
python -m pytest tests/test_local_workspace.py tests/test_local_workspace_api.py -q -p no:cacheprovider
node tools/check_web.mjs longflow/web
node tests/test_safety_web.mjs
```

These tests use disposable sources and mock only the native picker. They do not assert autonomous coding, sandboxed execution, production deployment or full crash recovery.
