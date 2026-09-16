# Hermes maintenance update

`scripts/hermes-maintenance-update.py` provides a guarded three-step update for
the local Hermes branch model:

- `main` records the selected release/commit after a successful update;
- `diatche` remains the validated local integration/runtime branch;
- feature branches are not changed;
- the official updater runs directly in the operator's terminal, with its normal
  output and input handling.

This is intentionally a **single-operator local script**. Pavel runs one
foreground maintenance sequence at a time and does not separately edit Git
refs/worktrees between prepare, the official updater, and post. The configured
upstream and official updater are trusted. This is not a multi-user service,
fleet controller, or adversarial boundary; do not add distributed leases,
provenance proofs, or hypothetical concurrent-writer policy to it.

## Usage

### 0. Quiesce services the wrapper does not own

The maintenance wrapper stops only the default signed `HermesGateway.app`
wrapper. Before prepare, stop the separately supervised named profiles from an
independent Terminal and gracefully exit any manually served dashboard:

```bash
cd ~/.hermes/hermes-agent
./venv/bin/hermes --profile crmwebhook gateway stop
./venv/bin/hermes --profile recovery gateway stop
# In the terminal serving a manual dashboard, press Ctrl-C and wait for exit.
```

If another named profile is active, run the corresponding
`./venv/bin/hermes --profile <name> gateway stop` too. Prepare calls the
controller's global `--assert-update-quiescence` after stopping the default
wrapper, so a loaded/running named service, manual gateway, dashboard, or
listener survivor blocks the updater and triggers recovery rather than being
killed by this script.

### 1. Prepare

Run the command with no arguments (equivalent to `--pre --stable`):

```bash
./venv/bin/python scripts/hermes-maintenance-update.py
```

Use the repository venv interpreter exactly. On this host, bare `python3` and
the script shebang resolve Miniconda Python 3.10, which cannot import the current
health stack. Prepare prints the post command with the exact `sys.executable`
that ran prepare so post uses the same Python 3.11 environment.

The pre step:

1. validates the clean `diatche` checkout and custom gateway wrapper;
2. privately fetches `origin/main` and upstream release tags, deepening a
   shallow checkout by 4096 commits first so tag ancestry can be proven;
3. selects the newest numeric stable tag reachable from the fetched upstream tip;
4. reports the selected target, upstream tip, and commit skew as diagnostics;
   skew never pauses or refuses a supervised update;
5. checks mergeability and validates an isolated candidate;
6. rechecks checkout/ref invariants;
7. verifies that the exact updater executable supports both `--backup` and
   `--no-gateway-restart`, failing before interruption if either is absent;
8. stops the custom gateway wrapper and requires global update quiescence;
9. moves local `main` to the selected target when it is behind upstream tip;
10. records an `awaiting-official-update` journal;
11. prints the exact update and post commands.

Alternative target modes are explicit:

```bash
# Explicit stable mode (also the default)
./venv/bin/python scripts/hermes-maintenance-update.py --stable

# Current upstream main tip
./venv/bin/python scripts/hermes-maintenance-update.py --latest

# A particular commit reachable from upstream main
./venv/bin/python scripts/hermes-maintenance-update.py --commit <SHA>
```

### 2. Run the printed official update command

The printed command is equivalent to:

```bash
cd ~/.hermes/hermes-agent && \
  ./venv/bin/hermes update --branch main --backup --yes --no-gateway-restart
```

Run it directly. Its stdout/stderr and any terminal interaction are therefore
visible and connected to your terminal rather than captured by the maintenance
wrapper. The official updater remains unchanged: it advances `main` to the
upstream tip it observes and performs its ordinary dependency, asset, migration,
cache, and managed-component synchronization. The custom wrapper is already
stopped, and the current official updater does not relaunch it; lifecycle
ownership returns to the maintenance script in the post step.

The wrapper probes the invoked executable's `update --help` before stopping
anything. It does not print an unsupported restart-suppression option and does
not silently omit it. The exact installed/v0.21.3 parser assessed for this run
does **not** expose `--no-gateway-restart`, while the currently fetched upstream
parser does; prepare therefore fails closed until the executable that will run
the update has the supported option.

### 3. Run the printed post command

The printed command is equivalent to:

```bash
./venv/bin/python scripts/hermes-maintenance-update.py --post
```

Run `--post` even if the official updater failed or was interrupted. The post
step validates the resulting Git state. If valid, one compare-and-swap
transaction restores `main` to the selected target and publishes the
prevalidated candidate to `diatche`. It then restores that checkout, applies the
narrow Hindsight compatibility guard, starts the custom wrapper, and runs live
health checks. `origin/main` remains at the tip observed by the updater, which
may be a descendant of the preflight tip if upstream advanced meanwhile. If the updater
left partial or invalid Git state, post attempts bounded owned-state recovery and
restarts the previous runtime when safe.

After post has restored and verified the default wrapper, restart the named
profile services that were stopped in step 0 and verify their profile-specific
health:

```bash
cd ~/.hermes/hermes-agent
./venv/bin/hermes --profile crmwebhook gateway start
./venv/bin/hermes --profile recovery gateway start
```

The post step cannot observe the separate updater process's exit code. It judges
success from the required resulting Git state. Installation synchronization is
owned by and trusted to the official updater; this wrapper does not inspect or
repeat its dependency, build, migration, profile, cache, or managed-component
phases. A partial or unexpected Git state is rejected.

## Live upstream preflight only

To fetch and check the current live upstream state without stopping Hermes:

```bash
./venv/bin/python scripts/hermes-maintenance-update.py --check
```

`--check` privately fetches `origin/main` and, in stable mode, release tags under
`refs/hermes-maintenance/`. It reports target/upstream identity and diagnostic
skew. It does not move branch or remote-tracking refs, write `FETCH_HEAD`, alter
the checkout, or stop/restart Hermes. In a shallow repository it does update
Git's shallow boundary by fetching up to 4096 additional commits; if that is
still insufficient, the failure prints the explicit `git fetch --deepen=4096
origin main` preparation command rather than weakening the ancestry check. Git's
`merge-tree --write-tree` may leave harmless unreachable temporary objects;
normal Git maintenance can reclaim them. Private fetch refs are retained as
an audit trail and may be pruned by maintenance cleanup.

`-h` and `--help` describe the public `--pre`, `--post`, `--check`, `--stable`,
`--latest`, and `--commit` interface. Path, timeout, and structured-output
overrides used by isolated tests are intentionally hidden.

## Safety and recovery boundaries

- Every pre/post phase acquires the exclusive maintenance lock.
- Only the current `state.json` journal drives interrupted recovery; historical
  `runs/*.json` files remain records and are not replayed.
- Publication atomically restores selected `main` and publishes `diatche`, with
  compare-and-swap checks for current `main`, `origin/main`, the pinned private
  target ref, and the old `diatche` ref.
- The custom wrapper owns only local integration and lifecycle policy. The
  official updater owns and is trusted for installation synchronization.
- Recovery restores the known pre/update/post Git states from the current
  journal. Dirty or structurally inconsistent checkout state is not overwritten.
- Health checks remain mandatory and bounded. Their default budget is 900 seconds
  because the live multi-gigabyte state database exceeded a 300-second
  `--check-only --no-state --json` observation; timeout is still a hard failure
  and recovery trigger, not permission to skip validation.
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
