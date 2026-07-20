# Guarded Hermes maintenance update

`hermes-maintenance-update.py` preserves Pavel's local branch model while still
using the official Hermes updater for dependencies, configuration migrations,
and bundled assets.

## Branch contract

- `main` is an exact mirror of `origin/main` after a successful run.
- `diatche` is the validated local integration branch.
- `feature/*` branches are never merged automatically.
- Merge conflicts are never resolved automatically.

## Installed commands

```bash
~/.hermes/local/bin/hermes-maintenance-update --check
~/.hermes/local/bin/hermes-maintenance-update --detach
~/.hermes/local/bin/hermes-maintenance-update --status
```

`--detach` starts a one-shot LaunchAgent (`nz.diatche.hermes-maintenance-update`)
so the update process does not run as a child of the gateway it must stop. The
queued request carries a unique token; duplicate queued or active requests are
rejected instead of kickstarting a second worker. The caller reports success
only after the worker acknowledges that exact token.

## Safety sequence

1. Acquire an exclusive lock and a durable lifecycle marker that continues to
   exclude installs or new runs through failure recovery and final state write.
2. Require a clean `diatche` checkout with no merge/rebase/cherry-pick state.
3. Fetch `origin/main` and record the exact SHA.
4. Simulate the merge with `git merge-tree --write-tree`.
5. Snapshot `main`, `diatche`, and every `feature/*` tip under timestamped
   `backup/maintenance-*` refs.
6. Build the merge from the pinned SHAs in a detached temporary worktree,
   validate it, and retain the exact tested commit under an immutable pre-update
   candidate ref; maintained branches are not moved yet.
7. Run `git diff --check`, Python compilation, and focused restart/todo/display
   tests. Failure leaves the live checkout and wrapper untouched.
8. Stop the signed `HermesGateway.app` LaunchAgent and verify its app process,
   gateway child, and dashboard listener are all gone. Refuse the update if any
   other profile service, manual Hermes gateway, or dashboard listener remains;
   this prevents the official updater from spawning detached restart actors.
9. Run `hermes update --branch main --backup --yes --no-gateway-restart`
   while no gateway is running. The maintenance-only flag suppresses all official
   updater gateway/dashboard stop, restart, and detached relaunch behavior; then
   re-prove quiescence before reasserting the custom supervisor policy.
10. Rebuild and rerun candidate validation into a separate immutable post-update
    candidate ref against the updater's resulting Python environment and exact
    upstream SHA (including any upstream drift); never auto-resolve conflicts.
11. In one Git ref transaction, verify `main`, `origin/main`, the post-update
    candidate, and the old `diatche` tip, then compare-and-swap `diatche` to the
    tested candidate. Verify the checkout, branch ref, candidate ref,
    cleanliness, and upstream ancestry again before restart.
12. Validate configuration, runtime imports, and a real Hindsight recall while
    the gateway remains stopped.
13. Restart the custom wrapper, verify stable exact child/listener identities,
    then run the topology-aware live health check and re-prove branch, HEAD,
    refs, cleanliness, and unfinished-operation absence. Any mismatch stops the
    wrapper.

If the updater fails, times out, or receives SIGINT/SIGTERM, its subprocess group
is terminated and the wrapper is restarted only after the exact prior/candidate
runtime, configuration, dependencies, and health checks still pass. If recovery
checks fail, the gateway remains stopped and the lifecycle marker remains in
place, blocking new runs, installation, detach, and wrapper restart until manual
repair; inspect `--status` and the logs before manual recovery.

## Logs and state

```text
~/.hermes/local/update/state.json
~/.hermes/logs/hermes-maintenance-update.log
~/.hermes/logs/hermes-maintenance-update.error.log
~/.hermes/logs/hermes-gateway-wrapper.restart.log
```

## Installation

From a reviewed checkout:

```bash
python3 scripts/hermes-maintenance-update.py --install
```

Installation validates a complete staged updater/controller/plist set and writes
a durable transaction journal plus backups before replacing anything. It then
fsyncs the backups/journal/replacements, atomically replaces each file, and
verifies the replacement LaunchAgent. Rollback attempts every target and
aggregates restoration failures while retaining the journal. A
bootstrap failure or caught interruption restores the prior files and previously
loaded job; after a hard process/host interruption, the next `--install` first
replays the journaled rollback. It does **not** run an update or restart the
gateway.
