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
isolated candidate construction, the official update, gateway stop/start, ref
publication, Hindsight compatibility, and live health verification. Its managed
installation step is exactly:

```text
<repo>/venv/bin/hermes update --branch main --backup --yes --no-gateway-restart
```

The official updater owns dependency synchronization, assets, bundled skills,
configuration migration, caches, and backups. The wrapper supplies the local
Git-branch and custom-supervisor policy around it rather than imitating those
internals.

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
5. Force-stop through the custom wrapper, then invoke the official updater with
   gateway restart disabled. Require it to leave the clean checkout on `main`,
   with `main`, `origin/main`, and the private fetch ref all equal to the pinned
   upstream SHA while `diatche` is still at its old SHA.
6. Atomically compare-and-swap only `diatche` to the prebuilt candidate while
   verifying `main`, `origin/main`, the private fetch ref, and old `diatche` in
   the same ref transaction.
7. Restore the exact `diatche` checkout, enforce the Hindsight
   `huggingface-hub>=1.5.0,<2.0` compatibility guard, restart through the custom
   wrapper, and verify wrapper status plus the health probe.
8. On ordinary failure, keep the original lock and one-shot signal guard through
   recovery and final state publication. Roll back only a ref/checkout state
   proven to have been produced by this transaction; concurrent ref, index, or
   file changes fail closed and are preserved.
9. Before hard-crash recovery mutates refs or checkout files, force-stop any
   runtime that cannot be proven to be the healthy original runtime.

Every child command is bounded and runs in its own process group so timeout or
interruption terminates descendants. Git refs and checkout files owned by the
orchestration transaction are recovered only when their identity and clean
state are safely provable. Official-updater external state is deliberately not
transactional: dependency/environment changes, generated assets, bundled skill
or config synchronization, caches, and updater backups may remain after updater
or later orchestration failure. Recovery restores the prior Git runtime and
restarts it when safe; it does not claim to restore the whole installation.

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
