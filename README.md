# victor_empire

Victor's canonical control plane and command center.

`victor_empire` now owns authoritative runtime state, governance, capability leases, event history, verification receipts, and outcomes. Other Massive Magnetics repositories are treated as capability/evidence organs behind adapters; they do not get to create competing truth.

## Operational closed loop

```text
capture -> work order -> govern -> lease -> execute -> verify -> commit outcome -> append event -> inspect state
```

The current production baseline is intentionally narrow: `artifact.write` is the only executable capability enabled by default. Unknown capabilities are denied, hosted model providers fail closed under `VICTOR_MODEL_SOVEREIGNTY`, and every meaningful transition is persisted in SQLite and the hash-chained event ledger.

## Command Center

Requirements: Python 3.10+; no third-party packages.

Run one complete closed loop:

```bash
python command_center.py run "Produce today's highest-leverage execution receipt"
```

Launch the local command center:

```bash
python command_center.py serve
```

Then open `http://127.0.0.1:8787`.

Inspect state or verify continuity:

```bash
python command_center.py status
python command_center.py verify-chain
```

Canonical state is stored under `.victor/victor.db` with SQLite WAL enabled. Runtime artifacts are stored under `artifacts/`. Both are ignored by git.

## Legacy migration

The original personal-grade runtime used `INBOX.txt`, `runway_log.jsonl`, `.active_work_order.json`, and individual work-order files. Import available legacy state as evidence without treating it as automatically true:

```bash
python migrate_legacy.py --root .
```

The original `nzt_session.py` workflow remains available during migration.

## Consolidated organs

`empire_manifest.json` defines the current integration map. The first named organs include:

- `dev-ville` — verification/evidence
- `victor-prime-agent` — bounded execution
- `autoresearch-win-rtx-victor` — local research
- `AGI` — experimental intelligence
- `supermegaomnirepo` — legacy archive/intake
- `victor-core` — legacy reference
- `Victor_Synthetic_Super_Intelligence` — continuity research
- `victor-corpus` — corpus/memory input
- `skills` — capability catalog
- `income-generator` — revenue experiments
- `iambandobandz` — public signal/distribution

Adapters are intentionally marked pending until each organ can accept a bounded lease, emit evidence, and pass independent verification.

## Verification

```bash
python -m unittest discover -s tests -v
```

CI runs the unit suite plus an end-to-end CLI smoke test on pushes and pull requests.

## Legacy NZT modules

The original six personal-grade modules remain in place while their state is migrated into the canonical runtime:

1. `victor_nzt_mode_router.py`
2. `work_order.py`
3. `single_thread_enforcer.py`
4. `runway_ledger.py`
5. `inbox_capture.py`
6. `nzt_session.py`

The command-center runtime supersedes their flat-file state as the source of truth.
