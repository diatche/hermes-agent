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

### 0. Create the fail-closed external backup

Run the established Samsung generation builder and wait for a successful exit:

```bash
~/.hermes/scripts/backup/backup_hermes_to_samsung.sh daily
```

It stages on the mounted Samsung volume, snapshots SQLite databases, tests the
zip, hashes the result, and publishes an immutable manifest. Prepare requires a
complete generation less than four hours old. The official updater's internal
full backup is intentionally disabled: this host's included set is about 32 GiB
while the internal disk has about 31 GiB free, and that updater backup is
best-effort rather than fail-closed.

### 1. Quiesce services the wrapper does not own

The maintenance wrapper stops only the default signed `HermesGateway.app`
wrapper. Before prepare, stop the separately supervised named profiles from an
independent Terminal and gracefully exit any manually served dashboard:

```bash
launchctl bootout "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/ai.hermes.gateway-crmwebhook.plist"
launchctl bootout "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/ai.hermes.gateway-recovery.plist"
# In the terminal serving a manual dashboard, press Ctrl-C and wait for exit.
```

If another named profile is active, run the corresponding
`./venv/bin/hermes --profile <name> gateway stop` too. Prepare calls the
controller's global `--assert-update-quiescence` after stopping the default
wrapper, so a loaded/running named service, manual gateway, dashboard, or
listener survivor blocks the updater and triggers recovery rather than being
killed by this script.

### 2. Prepare

Run the pinned command below:

```bash
./venv/bin/python scripts/hermes-maintenance-update.py \
  --pre --commit 345cd2b057a452236de401d3534b8502a7465e8d
```

Use the repository venv interpreter exactly. On this host, bare `python3` and
the script shebang resolve Miniconda Python 3.10, which cannot import the current
health stack. Prepare prints the post command with the exact `sys.executable`
that ran prepare so post uses the same Python 3.11 environment.

The pre step:

1. requires a fresh immutable Samsung backup generation;
2. validates the clean `diatche` checkout and custom gateway wrapper;
3. privately fetches `origin/main` and upstream release tags, deepening a
   shallow checkout by 4096 commits first so tag ancestry can be proven;
3. selects the newest numeric stable tag reachable from the fetched upstream tip;
4. reports the selected target, upstream tip, and commit skew as diagnostics;
   skew never pauses or refuses a supervised update;
5. checks mergeability and validates an isolated candidate;
6. rechecks checkout/ref invariants;
7. verifies that the exact updater executable supports `--no-backup`; this is
   safe only because step 0 has already passed the external backup gate;
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

### 3. Run the printed official update command

The printed command is equivalent to:

```bash
cd ~/.hermes/hermes-agent && \
  ./venv/bin/hermes update --branch main --no-backup --yes
```

Run it directly. Its stdout/stderr and any terminal interaction are therefore
visible and connected to your terminal rather than captured by the maintenance
wrapper. The official updater remains unchanged: it advances `main` to the
upstream tip it observes and performs its ordinary dependency, asset, migration,
cache, and managed-component synchronization. The custom wrapper is already
stopped and global update quiescence has been proved, so the updater has no
gateway/dashboard process to restart. Lifecycle ownership returns to the
maintenance script in the post step. The exact v0.21.3 parser does not expose
`--no-gateway-restart`; this workflow does not invent that unsupported flag.

### 4. Run the printed post command

The printed command is equivalent to:

```bash
./venv/bin/python scripts/hermes-maintenance-update.py --post
```

Run `--post` even if the official updater failed or was interrupted. The post
step validates the resulting Git state, then rebuilds and validates the pinned
candidate again against the dependency environment produced by the official
updater. If valid, one compare-and-swap transaction restores `main` to the
selected target and publishes that post-update candidate to `diatche`. It then
restores that checkout, applies the
narrow Hindsight compatibility guard, starts the custom wrapper, and runs live
health checks. `origin/main` remains at the tip observed by the updater, which
may be a descendant of the preflight tip if upstream advanced meanwhile. If the updater
left partial or invalid Git state, post attempts bounded owned-state recovery and
restarts the previous runtime when safe.

After post has restored and verified the default wrapper, restart the named
profile services that were stopped in step 0 and verify their profile-specific
health:

```bash
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/ai.hermes.gateway-crmwebhook.plist"
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/ai.hermes.gateway-recovery.plist"
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
