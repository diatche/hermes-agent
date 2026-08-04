# Hermes maintenance update

`scripts/hermes-maintenance-update.py` provides a guarded three-step update for
the local Hermes branch model:

- `main` mirrors the pinned `origin/main` tip;
- `diatche` remains the validated local integration/runtime branch;
- feature branches are not changed;
- the official updater runs directly in the operator's terminal, with its normal
  output and input handling.

## Usage

### 1. Prepare

Run the command with no arguments (equivalent to `--pre`):

```bash
python3 scripts/hermes-maintenance-update.py
```

The pre step:

1. validates the clean `diatche` checkout and custom gateway wrapper;
2. fetches and pins `origin/main`;
3. checks mergeability and validates an isolated candidate;
4. rechecks checkout/ref invariants;
5. stops the custom gateway wrapper;
6. records an `awaiting-official-update` journal;
7. prints the exact update and post commands.

### 2. Run the printed official update command

The printed command is equivalent to:

```bash
cd ~/.hermes/hermes-agent && \
  ./venv/bin/hermes update --branch main --no-backup --yes --no-gateway-restart
```

Run it directly. Its stdout/stderr and any terminal interaction are therefore
visible and connected to your terminal rather than captured by the maintenance
wrapper.

### 3. Run the printed post command

The printed command is equivalent to:

```bash
python3 scripts/hermes-maintenance-update.py --post
```

Run `--post` even if the official updater failed or was interrupted. The post
step validates the resulting Git state. If valid, it atomically publishes the
prevalidated candidate to `diatche`, restores that checkout, applies the narrow
Hindsight compatibility guard, starts the custom wrapper, and runs live health
checks. If the updater left partial or invalid Git state, post attempts the
bounded owned-state recovery and restarts the previous runtime when safe.

The post step cannot observe the separate updater process's exit code. It judges
success from the required resulting Git state. A nonzero updater exit that still
produced the complete required state may therefore proceed; a partial or
unexpected state is rejected.

## Live upstream preflight only

To fetch and check the current live upstream state without stopping Hermes:

```bash
python3 scripts/hermes-maintenance-update.py --check
```

`--check` fetches `origin/main` into a unique private ref under
`refs/hermes-maintenance/fetches/`. It does not move branch or remote-tracking
refs, write `FETCH_HEAD`, alter the checkout, or stop/restart Hermes. Git's
`merge-tree --write-tree` may leave harmless unreachable temporary objects;
normal Git maintenance can reclaim them. Private fetch refs are retained as
an audit trail and may be pruned by maintenance cleanup.

`-h` and `--help` describe the public `--pre`, `--post`, and `--check`
interface. Path, timeout, and structured-output overrides used by isolated tests
are intentionally hidden.

## Safety and recovery boundaries

- Every pre/post phase acquires the exclusive maintenance lock.
- Only the current `state.json` journal drives interrupted recovery; historical
  `runs/*.json` files remain records and are not replayed.
- Publication uses compare-and-swap checks for `main`, `origin/main`, the pinned
  private fetch ref, and the old `diatche` ref.
- Recovery changes only Git refs/checkout state proven to be owned by the
  transaction. Concurrent ref, index, worktree, or untracked changes fail closed
  and are preserved.
- Official-updater external state is not transactional: dependencies, generated
  assets, bundled skills, config migrations, caches, and backups may remain
  after updater or post failure.
- The Hindsight `huggingface-hub==1.24.0` repair uses `pip --no-deps`, matches
  the shared lazy-dependency/lockfile pin, and is intentionally outside the Git
  rollback boundary.
- The stock Hermes gateway service remains disabled; lifecycle control stays
  with the custom HermesGateway wrapper.

## State

Transaction journals and the exclusive lock live under:

```text
~/.hermes/local/update/
```

After pre, the gateway intentionally remains stopped until the operator runs the
printed update and post commands. Starting another pre while a stale handoff is
present invokes interrupted-state recovery rather than silently replacing it.
