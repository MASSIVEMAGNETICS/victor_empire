# Victor ABI v1

Victor is the model-independent continuity and authority kernel. Organs remain
replaceable and independently testable.

## Ownership

- `victor_empire`: protocol, state, policy, leases, routing, causal history.
- `dev-ville`: artifact-producing machine-labor organ.
- `autoresearch-win-rtx-victor`: bounded experiment organ.
- `victor-prime-agent`: optional scheduling/agent-loop adapter after sovereignty hardening.

No organ can directly mark its own work `DONE`. It reports an artifact; an
independent verifier reports evidence; the kernel decides the legal transition.

## Canonical loop

```text
CAPTURED -> PLANNED -> AUTHORIZED -> LEASED -> RUNNING -> VERIFYING
                                                               |-- DONE
                                                               `-- REWORK
```

Every acknowledged state change commits its event, Chronos receipt, state
projection, and dispatch intent in one SQLite `BEGIN IMMEDIATE` transaction.

## Sovereignty

The kernel contains no hosted-model client and performs no silent inference
fallback. Network, filesystem, and process authority default to deny and must be
granted through a time-bounded `CapabilityLease`.

## First organ contract

The next milestone connects `dev-ville` through `VictorOrgan`. It must accept a
versioned `WorkOrder` and valid `CapabilityLease`, then return artifacts and
observations. Verification remains independent.

