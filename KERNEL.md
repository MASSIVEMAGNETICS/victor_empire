# Victor Kernel v0.1 — M0/M1

This is the canonical control-plane slice for Victor.

It does **not** make `victor_empire` a model, an agent swarm, or a mega-runtime. It gives replaceable organs one governed substrate for authority, state, causal history, dispatch and verification.

## Invariants

1. **State + event are atomic.** Every authoritative WorkOrder transition and its Informatron/Chronos receipt commit in one SQLite transaction.
2. **SQLite WAL is the durable substrate.** `journal_mode=WAL`, `foreign_keys=ON`, `synchronous=FULL`.
3. **No organ can mark itself DONE.** Only a passing `VerificationReceipt` may transition `VERIFYING → DONE`.
4. **Execution requires a lease.** Capability leases are bounded by actor, organ, capabilities, scope, expiry and use count, then HMAC-signed by the kernel.
5. **TRACE is not authority.** Events preserve what happened; authority labels identify why a transition was legal.
6. **Retries are safe.** Mutating API calls accept idempotency keys and persist command results.
7. **Dispatch is transactional.** Organ/verifier commands are written to an outbox in the same transaction as the state transition that caused them.
8. **Restart does not rewrite history.** Recovery verifies SQLite, foreign keys, WorkOrder projections, DONE receipts and the Chronos chain before resuming.

## State machine

```text
CAPTURED
   ↓
AUTHORIZED
   ↓
LEASED
   ↓
RUNNING
   ↓
VERIFYING
  ↙      ↘
REWORK   DONE
  ↓
AUTHORIZED
```

`BLOCKED`, `FAILED` and `CANCELLED` are explicit terminal/recovery branches. `DONE` cannot be reached through the generic transition function.

## Canonical path

```text
PIR / Human decision
        ↓
create WorkOrder
        ↓
Ethica/policy authorization
        ↓
signed CapabilityLease
        ↓
transactional outbox
        ↓
dev-ville organ
        ↓
content-addressed artifact
        ↓
independent verifier
        ↓
VerificationReceipt
        ↓
Chronos + WorkOrder transition
        ↓
DONE or REWORK
```

## Quick start

```bash
export VICTOR_KERNEL_SIGNING_KEY='replace-with-a-long-random-secret'
python -m victor_kernel.cli --db victor_kernel.db init
python -m victor_kernel.cli --db victor_kernel.db verify
python -m victor_kernel.cli --db victor_kernel.db recover
```

## Tests

```bash
python -m unittest discover -s tests -v
```

Verified locally on 2026-08-09: **11 tests passed, 0 failed**.

The suite covers:

- verified happy path;
- impossible unverified `DONE`;
- failed verification → `REWORK`;
- idempotent retries;
- expired lease rejection/recovery;
- actor/capability rejection;
- hard restart with causal state + outbox preservation;
- Chronos tamper detection;
- durable outbox acknowledgement;
- lease signature stability across mutable usage state;
- outbox → organ → artifact dispatch.

## Security boundary

The kernel signs lease and verification payloads with HMAC-SHA256. The signing secret is **not** stored in SQLite and must be supplied through `VICTOR_KERNEL_SIGNING_KEY` or an injected secret manager.

This is not OS sandboxing. A lease represents authorization, but the executor must still enforce filesystem/network/process scope using an actual sandbox (Windows AppContainer/job object, container, VM, seccomp, etc.).

`dev-ville` stays a separate organ. The kernel owns contracts, state, authorization, causal history and verification gates; the organ owns execution.
