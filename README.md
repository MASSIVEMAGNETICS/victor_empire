# victor_empire

Victor's canonical control plane.

## Canonical kernel

The new `victor_kernel/` package is the authoritative M0/M1 control-plane slice. It owns:

- `WorkOrder` state and legal transitions;
- HMAC-signed bounded `CapabilityLease` grants;
- SQLite WAL persistence with `foreign_keys=ON` and `synchronous=FULL`;
- Informatron events and Chronos tamper-evident receipts;
- idempotent command handling;
- transactional outbox dispatch to replaceable organs;
- independent `VerificationReceipt` gating for `DONE`;
- restart/integrity recovery.

Read [`KERNEL.md`](./KERNEL.md) for invariants, state machine, security boundary and verification details.

```bash
export VICTOR_KERNEL_SIGNING_KEY='replace-with-a-long-random-secret'
python -m victor_kernel.cli --db victor_kernel.db init
python -m victor_kernel.cli --db victor_kernel.db verify
python -m unittest discover -s tests -v
```

`dev-ville`, autoresearch, Prime adapters and future workers remain separate organs. They execute work through the kernel contract; they do not own Victor's identity, state, authority or definition of `DONE`.

## Legacy NZT personal CLI

The original personal-grade NZT CLI remains available while its useful focus/work primitives are migrated behind the kernel interfaces:

1. `victor_nzt_mode_router.py`
2. `work_order.py`
3. `single_thread_enforcer.py`
4. `runway_ledger.py`
5. `inbox_capture.py`
6. `nzt_session.py`

```bash
python nzt_session.py --start --goal "Ship MVP" --next "Draft CLI" --inbox "idea: add web UI"
```

The legacy CLI creates a JSON work order, appends the JSONL runway ledger, captures inbox items, and enforces a single active work order. New authoritative orchestration should use `victor_kernel` instead of extending the flat-file runtime.
