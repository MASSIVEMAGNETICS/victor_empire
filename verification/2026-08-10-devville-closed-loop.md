# Dev-Ville closed-loop verification — 2026-08-10

Purpose: trigger an independently inspectable pull-request CI run of Victor's canonical unit/smoke tests and the real cross-repository Dev-Ville organ integration.

Acceptance criteria:

- Victor unit tests pass.
- Command-center local smoke loop passes and the event chain verifies.
- `MASSIVEMAGNETICS/dev-ville` probes successfully through `victor_adapter.py`.
- Victor dispatches a bounded `devville.project.build` job.
- Dev-Ville returns a `victor.organ.receipt.v1` receipt with generated artifacts.
- Victor independently verifies the receipt/artifact hashes and commits the outcome.
- The final event chain verifies.

This file is verification evidence/trigger metadata only; it does not change runtime behavior.
