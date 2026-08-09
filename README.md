# victor_empire

Victor's canonical control plane and command center.

`victor_empire` owns authoritative runtime state, governance, capability leases, event history, verification receipts, and outcomes. Other Massive Magnetics repositories are capability/evidence organs behind adapters; they do not create competing truth.

## Operational closed loop

```text
capture -> work order -> govern -> lease -> dispatch -> execute -> verify -> commit outcome -> append event -> inspect state
```

Two bounded execution capabilities are currently enabled:

- `artifact.write` — local receipt/artifact worker
- `devville.project.build` — Dev-Ville software-build organ

Unknown capabilities are denied. Hosted model providers fail closed under `VICTOR_MODEL_SOVEREIGNTY`. Every meaningful transition is persisted in SQLite and the hash-chained event ledger.

## Command Center

Requirements: Python 3.10+; no third-party packages for the Victor control plane.

Run one local closed loop:

```bash
python command_center.py run "Produce today's highest-leverage execution receipt"
```

Run a Dev-Ville software build when `dev-ville` is cloned as a sibling repository and contains `victor_adapter.py`:

```bash
python command_center.py run --organ dev-ville "Create a minimal backend API with validation and tests"
```

If Dev-Ville lives elsewhere:

```bash
python command_center.py --devville-root /path/to/dev-ville run --organ dev-ville "Build the project"
```

You can also set `VICTOR_DEVVILLE_ROOT`.

Launch the local command center:

```bash
python command_center.py serve
```

Then open `http://127.0.0.1:8787`. The UI requires explicit organ selection; Victor does not silently escalate a natural-language command to a more powerful execution capability.

Inspect state or verify continuity:

```bash
python command_center.py status
python command_center.py verify-chain
```

Canonical state is stored under `.victor/victor.db` with SQLite WAL enabled. Runtime artifacts and organ receipts are stored under `artifacts/`. Both are ignored by git.

## Dev-Ville organ contract

Dev-Ville is the first live external organ.

```text
Victor work order
    -> Ethica capability check
    -> expiring lease
    -> victor.organ.job.v1
    -> dev-ville/victor_adapter.py
    -> bounded Company.start_project/work_cycle execution
    -> victor.organ.receipt.v1
    -> independent Victor re-hash of receipt + every artifact
    -> canonical outcome commit
```

Victor launches the adapter with an exact Python command, `shell=False`, a stripped environment, a bounded timeout, and a scoped output directory. Dev-Ville's adapter additionally rejects unsafe paths and denies network/subprocess events through a Python audit hook during execution.

That audit hook is defense in depth, not an OS sandbox. Hostile code should still run under a restricted OS account, container, VM, or equivalent isolation.

If Dev-Ville is missing, times out, violates the contract, returns an incomplete project, or returns a bad digest, Victor fails the work order, closes the lease, marks the organ degraded, and appends the failure to causal history.

## Legacy migration

The original personal-grade runtime used `INBOX.txt`, `runway_log.jsonl`, `.active_work_order.json`, and individual work-order files. Import available legacy state as evidence without treating it as automatically true:

```bash
python migrate_legacy.py --root .
```

The original `nzt_session.py` workflow remains available during migration.

## Consolidated organs

`empire_manifest.json` defines the integration map.

- `dev-ville` — **live bounded software-build + verification/evidence organ**
- `victor-prime-agent` — bounded execution, adapter pending
- `autoresearch-win-rtx-victor` — local research, adapter pending
- `AGI` — experimental intelligence, adapter pending
- `supermegaomnirepo` — legacy archive/intake, evidence only
- `victor-core` — legacy reference, evidence only
- `Victor_Synthetic_Super_Intelligence` — continuity research, adapter pending
- `victor-corpus` — corpus/memory input, adapter pending
- `skills` — capability catalog, adapter pending
- `income-generator` — revenue experiments, adapter pending
- `iambandobandz` — public signal/distribution, adapter pending

An organ is not promoted to live until it can accept a bounded lease, emit evidence, and pass independent control-plane verification.

## Verification

```bash
python -m unittest discover -s tests -v
```

CI runs the unit suite plus a command-center smoke test. The Dev-Ville repository separately runs its regression suite, adapter probe, and real organ contract test.

## Legacy NZT modules

The original six personal-grade modules remain in place while their state is migrated into the canonical runtime:

1. `victor_nzt_mode_router.py`
2. `work_order.py`
3. `single_thread_enforcer.py`
4. `runway_ledger.py`
5. `inbox_capture.py`
6. `nzt_session.py`

The command-center runtime supersedes their flat-file state as the source of truth.
