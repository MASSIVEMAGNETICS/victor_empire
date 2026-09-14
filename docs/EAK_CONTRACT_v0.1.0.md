# Empire Autonomy Kernel Contract v0.1.0

Status: canonical candidate. This change does not supersede `victor_runtime.VictorKernel`; it coordinates bounded autonomous capabilities on top of the existing Victor canonical control plane.

## Prime invariants

1. `victor_empire` remains the sole canonical authority/state/lease/event owner.
2. Every autonomous ability must be registered before it can receive a task.
3. EAK v0.1 may execute only A0–A2 capabilities. A3–A5 fail closed.
4. A5 sovereign actions are never autonomous.
5. The API accepts only BANDO or TORI actor labels for Human STOP, and STOP blocks execution admission, verification promotion, and receipt finalization. Actor-label authentication is not implemented by this repository and remains a promotion blocker.
6. Capability deactivation is authoritative for already-triggered work: a task cannot begin or finalize after its capability becomes inactive.
7. A2 reversible execution must return rollback evidence.
8. Every successful task must pass an explicit verifier and commit an immutable EAK receipt.
9. Failed verification is quarantined, not promoted.
10. Capability contracts cannot mutate in place; a changed contract requires a new capability id/version.
11. The existing Victor event ledger records EAK task/receipt transitions.
12. The trusted score floor is 0.62. Callers may request a stricter threshold but cannot lower the floor.
13. `CLOSED`, `ABORTED`, and `QUARANTINED` tasks are terminal and immutable. Each task may receive at most one verification receipt.
14. Capability executor and verifier callables cannot be rebound in-process under an existing capability id; a new implementation requires a new capability version.
15. Task-state changes and their provenance events commit in the same SQLite transaction.
16. B Heard sandbox ingress is bounded before expensive decode/parse work: webhook bodies are capped at 64 KiB, decoded JSON is subject to the generic node/depth budget, signature headers at 4096 characters, intake text fields have explicit maximum lengths, webhook identifiers are bounded text, and the internally generated payment task payload is capped at 4096 serialized bytes.
17. A verified payment and its continuation task are durably linked. An identical retry resumes the same continuation rather than silently returning before a fulfillment task exists or creating duplicate payment/task chains.
18. Generic task admission accepts only bounded JSON objects: at most 64 KiB canonical UTF-8, 2,048 structural nodes, and 16 nesting levels. Admission validation completes before a SQLite write transaction begins, and stored task payloads are revalidated before use.
19. B Heard sandbox drafts are created and verified relative to a held workspace directory descriptor. Final-entry symlinks and pre-existing paths fail closed; draft content is byte-bounded and hashed from the bytes actually written.
20. B Heard intake, draft, approval, sandbox-delivery, and outcome state transitions commit atomically with their provenance events; an event append failure rolls back the associated database state.

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

Scoring is an exact `TRIGGERED -> SCORED` compare-and-set. Execution admission is an exact `SCORED -> EXECUTING` transition inside a SQLite `BEGIN IMMEDIATE` transaction that checks Human STOP, capability activity, authority, the policy-owned score floor, and callback availability. Verification admission and receipt closure use equivalent guarded transitions. A terminal task cannot be re-scored or replayed, and a database uniqueness constraint permits only one verification receipt per task.

Human STOP and capability activity are not one-time admission checks. EAK re-checks them after executor return and serializes final receipt/task closure behind a SQLite `BEGIN IMMEDIATE` finalization gate. A STOP or capability revocation that wins before execution admission prevents the executor from starting. A STOP or revocation that wins before finalization prevents the task from being promoted to `CLOSED` or receiving a verification receipt. Receipt insertion, task closure, and the `RECEIPT_COMMITTED` event are atomic. If closure wins first, a later STOP applies to subsequent/in-flight work rather than retroactively invalidating an already committed receipt.

This v0.1 contract does not claim hard preemption of arbitrary Python executor code at the instruction level. Executors therefore remain limited to A0–A2 bounded/reversible work; stronger consequence-producing capabilities require a separately reviewed killable execution substrate before promotion.

This repository also does not authenticate the BANDO/TORI actor string accepted by Human STOP and sandbox approval APIs. Those interfaces are labels, not owner credentials. Production promotion requires a separately approved, tested authority-verification boundary; no caller-supplied actor label may be treated as proof of human approval.

## B Heard paid-intake sandbox

`bheard.paid_intake.sandbox` is intentionally A2, not A4. It does not charge a card or send a customer message. It consumes a Stripe-compatible test-mode webhook payload as evidence and verifies:

- bounded webhook and signature sizes before decode/parse;
- HMAC signature and timestamp tolerance;
- event type `checkout.session.completed`;
- `livemode == false`;
- `payment_status == paid`;
- pinned price `$19.00` and currency `usd`;
- bounded event/intake identifiers and intake fields;
- intake existence;
- immutable event-id/payload replay identity.

A verified sandbox event creates an immutable payment receipt and, in the same SQLite write transaction, ensures a single durable continuation task plus its provenance event. `bheard_payment_continuations` links the Stripe-compatible event id, immutable payment receipt, and EAK task. If the process stops after that commit but before scoring or execution, an identical retry resumes the same `TRIGGERED`/`SCORED` task. A retry of a `CLOSED`, `ABORTED`, or `QUARANTINED` task reports that terminal state without creating duplicates. A task found in `EXECUTING` or `VERIFYING` is reported as `RECOVERY_REQUIRED` rather than blindly replaying an arbitrary executor.

The fulfillment task writes one bounded local draft with descriptor-relative `O_EXCL`/`O_NOFOLLOW` creation, requires BANDO/TORI approval, and only then permits a sandbox delivery receipt. Verification reopens the exact draft name relative to the held directory descriptor and rejects symlinks, non-regular files, oversized content, or hash mismatch. The delivery step explicitly performs no external send.

Live Stripe checkout, real charging, refunds, outbound customer contact, publishing, spending, autonomous price changes, and A4 authority are outside v0.1.

## Remaining boundaries

The B Heard sandbox ingress, generated payment-task payload, generic `EmpireAutonomyKernel.trigger()` payload, and local draft artifact are explicitly bounded. Legacy payment-task recovery uses an indexed exact canonical-payload lookup instead of parsing task history while holding the payment writer transaction.

Actor-label authentication also remains unresolved and requires an explicitly approved authority-verification contract rather than silently treating caller-supplied strings as identity proof.

## Acceptance gate

This candidate is reviewable when repository CI passes on the exact branch head. It is not eligible for merge or live economic authority while actor authentication remains unresolved. Any later live integration requires a separately reviewed change and evidence that sandbox receipt, Human STOP, rollback, authorization and replay protections are reliable.
