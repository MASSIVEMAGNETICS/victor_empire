# victor_empire

Victor's sovereign, model-independent kernel and control plane.

Victor is not a model and this repository is not an omnirepo. It defines the
versioned protocol, authority, state, causal history, and verification rules
that independently testable organs must obey.

## Current milestone: ABI + transactional kernel

Implemented:

- Victor ABI v1 objects: `WorkOrder`, `CapabilityLease`, `Informatron`,
  `VerificationReceipt`, `OrganManifest`, and `ExperimentResult`;
- strict WorkOrder state machine;
- SQLite WAL persistence with `synchronous=FULL` and foreign keys;
- atomic event + Chronos receipt + projection + outbox transactions;
- default-deny bounded capability leases;
- independent verification gate: organs cannot set `DONE`;
- idempotent WorkOrder capture and crash-recovery tests;
- typed `VictorOrgan` interface for `dev-ville`, autoresearch, and future organs.

See [Victor ABI v1](docs/VICTOR_ABI.md).

## Run tests

```bash
python -m unittest discover -s tests -v
```

The kernel supports Python 3.11+ using only the standard library.

## Legacy personal CLI

The original NZT session modules remain available during migration:

```bash
python nzt_session.py --start --goal "Ship MVP" --next "Draft CLI" --inbox "idea: add web UI"
```

