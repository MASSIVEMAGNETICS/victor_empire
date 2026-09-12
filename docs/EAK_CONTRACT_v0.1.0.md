# Empire Autonomy Kernel Contract v0.1.0

Status: canonical candidate. This change does not supersede `victor_runtime.VictorKernel`; it coordinates bounded autonomous capabilities on top of the existing Victor canonical control plane.

## Prime invariants

1. `victor_empire` remains the sole canonical authority/state/lease/event owner.
2. Every autonomous ability must be registered before it can receive a task.
3. EAK v0.1 may execute only A0–A2 capabilities. A3–A5 fail closed.
4. A5 sovereign actions are never autonomous.
5. Human STOP may be set or cleared only by BANDO or TORI and blocks execution, verification promotion, and receipt finalization.
6. Capability deactivation is authoritative for already-triggered work: a task cannot begin or finalize after its capability becomes inactive.
7. A2 reversible execution must return rollback evidence.
8. Every successful task must pass an explicit verifier and commit an immutable EAK receipt.
9. Failed verification is quarantined, not promoted.
10. Capability contracts cannot mutate in place; a changed contract requires a new capability id/version.
11. The existing Victor event ledger records EAK task/receipt transitions.

## Authority classes

- A0 OBSERVE — read-only perception.
- A1 THINK — proposals/simulation/reasoning.
- A2 REVERSIBLE — internal reversible changes with rollback evidence.
- A3 CONSEQUENTIAL — external controlled effects; disabled in v0.1.
- A4 ECONOMIC — real payment/spend/customer actions; disabled in v0.1.
- A5 SOVEREIGN — constitution/identity/authority/irreversible actions; never autonomous.

## Task lifecycle

`TRIGGERED -> SCORED -> EXECUTING -> VERIFYING -> CLOSED`

Failure transitions are fail-closed to `ABORTED` or `QUARANTINED`.

A task executes only if its registered capability is active, its trigger is registered, Human STOP is clear, its score is >= 0.62, its authority is <= A2, its executor exists, and its verifier passes.

Human STOP and capability activity are not one-time admission checks. EAK re-checks them after executor return and serializes final receipt/task closure behind a SQLite `BEGIN IMMEDIATE` finalization gate. A STOP or capability revocation that wins before that finalization transaction prevents the task from being promoted to `CLOSED` or receiving a verification receipt. If closure wins first, a later STOP applies to subsequent/in-flight work rather than retroactively invalidating an already committed receipt.

This v0.1 contract does not claim hard preemption of arbitrary Python executor code at the instruction level. Executors therefore remain limited to A0–A2 bounded/reversible work; stronger consequence-producing capabilities require a separately reviewed killable execution substrate before promotion.

## B Heard paid-intake sandbox

`bheard.paid_intake.sandbox` is intentionally A2, not A4. It does not charge a card or send a customer message. It consumes a Stripe-compatible test-mode webhook payload as evidence and verifies:

- HMAC signature and timestamp tolerance;
- event type `checkout.session.completed`;
- `livemode == false`;
- `payment_status == paid`;
- pinned price `$19.00` and currency `usd`;
- intake existence;
- immutable event-id/payload replay identity.

A verified sandbox event creates an immutable payment receipt, triggers a fulfillment-draft task, writes a local draft, requires BANDO/TORI approval, and only then permits a sandbox delivery receipt. The delivery step explicitly performs no external send.

Live Stripe checkout, real charging, refunds, outbound customer contact, publishing, spending, autonomous price changes, and A4 authority are outside v0.1.

## Acceptance gate

This candidate is reviewable when repository CI passes on the exact branch head. It is not eligible for live economic authority without a separately reviewed change and evidence that sandbox receipt, Human STOP, rollback, authorization and replay protections are reliable.
