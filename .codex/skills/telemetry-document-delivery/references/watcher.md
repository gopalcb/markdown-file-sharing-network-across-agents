# Root Watcher Operation

## Installed runtime

The repository-root `watcher.py` is a local bootstrap. It resolves the project from
its own location, rejects unsafe symlinked runtime paths, selects the managed
watcher virtual environment when present, and injects the exact project root.

`watcher.py` and `.codex/tools/folder-watcher/` are installed local runtime assets
and may be absent from a fresh checkout. If either is missing, report that Work
Organizer bootstrap is required. Do not commit the runtime, copy it from an unknown
checkout, or invent its config.

Run commands from the project root. Do not add `--project`; the root launcher adds
it itself.

## Commands

```bash
python3 watcher.py ensure
python3 watcher.py status
python3 watcher.py once
python3 watcher.py login --username <cognito-username>
python3 watcher.py stop
python3 watcher.py logout
```

- `ensure` starts continuous watching when needed and confirms its control socket
  is ready.
- `status` inspects only the owned watcher process and summarized ledger state.
- `once` runs a bounded two-pass scan. It succeeds only when tracked documents and
  deletion events are settled; stop a continuous watcher first if one owns the lock.
- `login` runs the private Cognito terminal flow, remembers the approved account,
  and registers the local device when required.
- `stop` stops the owned watcher.
- `logout` stops it and removes the saved uploader session through the supported
  runtime behavior.

The launcher also supports `run`, but prefer `ensure` for normal background startup.

## Private authentication

The current watcher login is a private terminal flow. Password and email OTP input
is read without echo. The agent may start the login command when the user asks, but
the human must enter secrets directly into the private prompt. Never ask the user
to paste an OTP, password, token, device key, or signed header into chat or an agent
tool call.

Authentication success does not by itself prove upload or persistence. Check the
watcher result and exact document receipt separately.

## Storage and receipts

The root watcher scans Markdown recursively while excluding Git internals and its
runtime. It uploads through the approved native route with `project-path-v1`:

- document identity is stable for the authenticated owner and canonical project
  path;
- only a leading `docs/` is stripped from the stored project path;
- file group remains metadata rather than determining the path;
- reuploads update the same document identity and preserve linked task state.

An upload is stored only after the watcher receives HTTP 201 and validates the
exact basename, SHA-256, canonical relative path, `READY` status, nonempty
`file_id`, and `project-path-v1` layout. When a task is returned, its ID must match
the file ID.

## Reconciliation and troubleshooting

The watcher records an uncertainty marker before network transport. If a request
may have reached storage but its receipt is unavailable, `uncertain_hash` prevents
a blind retry. Preserve that state and reconcile the stable document identity with
an authorized remote read before retrying. Do not delete `state.json`, clear the
marker, or create a second uploader ledger.

Use the narrowest observation that answers the question:

1. `python3 watcher.py status` for process and state counts.
2. `python3 watcher.py once` for one bounded delivery attempt when no continuous
   watcher holds the lock.
3. The managed watcher log only for a concrete startup or delivery failure; do not
   dump credentials or full runtime configuration.

Report `pending`, `login-required`, `device-unavailable`, `uncertain`, scan errors,
and unsettled deletions as incomplete states, not successful uploads.

## Source of truth

Verify behavior against the installed root `watcher.py`, the managed
`.codex/tools/folder-watcher/watcher.py`, `AGENTS.md`, and `ARCHITECTURE.md` before
changing this reference.
