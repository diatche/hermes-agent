# Hermes maintenance update

`scripts/hermes-maintenance-update.py` is a single foreground updater for the
local Hermes branch model:

- `main` mirrors the fetched immutable `origin/main` tip;
- `diatche` remains the local integration branch;
- feature branches are not changed.

## Usage

Run the update in a terminal and wait for it to finish:

```bash
python3 scripts/hermes-maintenance-update.py
```

The command prints each major phase immediately while it runs, including fetch,
isolated candidate construction, gateway stop/start, ref publication, Hindsight
compatibility, and live health verification. It does not call the broad
`hermes update` command, so there is no hidden updater subprocess or buffered
updater output behind these messages.

Optionally run the preflight without stopping or restarting Hermes:

```bash
python3 scripts/hermes-maintenance-update.py --check
```

`--check` reads the currently recorded `origin/main` remote-tracking ref; it does
not fetch, write Git refs or `FETCH_HEAD`, alter the checkout, or stop/restart
Hermes. Git's `merge-tree --write-tree` may leave harmless unreachable temporary
objects in the repository; normal Git maintenance can reclaim them. The
foreground update itself always fetches and pins the current remote tip before
preflight.

`-h` and `--help` describe this public interface. Path, timeout, and output
overrides used by isolated tests are intentionally hidden.

## Safety sequence

1. Acquire an exclusive maintenance lock and automatically recover a valid
   interrupted current journal before starting a new transaction. Historical
   run files are retained as records but are not replayed.
2. Require the working checkout to be clean, on `diatche`, and free of an
   unfinished Git operation.
3. Fetch `origin/main` without writing `FETCH_HEAD`, pin its exact object ID,
   and prove the merge is conflict-free before stopping the runtime.
4. Build and validate the merge candidate in a temporary detached worktree, then
   immediately re-prove checkout cleanliness and all pinned refs before stop.
5. Force-stop through the custom wrapper, then atomically compare-and-swap both
   `main` and `diatche` refs.
6. Restore the exact `diatche` checkout, enforce the Hindsight
   `huggingface-hub>=1.5.0,<2.0` compatibility guard, restart through the custom
   wrapper, and verify wrapper status plus the health probe.
7. On ordinary failure, keep the original lock and one-shot signal guard through
   recovery and final state publication. Roll back only a ref/checkout state
   proven to have been produced by this transaction; concurrent ref, index, or
   file changes fail closed and are preserved.
8. Before hard-crash recovery mutates refs or checkout files, force-stop any
   runtime that cannot be proven to be the healthy original runtime.

Every child command is bounded and runs in its own process group so timeout or
interruption terminates descendants. The script does not invoke the broad
`hermes update` flow, build the application, synchronize profile/config/cache
state, or install itself.

The Hindsight compatibility repair uses `pip --no-deps` and is deliberately
outside the Git rollback boundary: once repaired, that shared-venv invariant is
retained even if a later runtime check requires source rollback.

## State

Transaction journals and the exclusive lock live under:

```text
~/.hermes/local/update/
```

A successful run prints the resulting `diatche` commit. A blocked preflight or
failed update writes the reason to standard error and exits nonzero.
