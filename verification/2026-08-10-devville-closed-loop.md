# Dev-Ville closed-loop verification — 2026-08-10

## Purpose

Independently verify Victor's canonical unit/smoke path and the real cross-repository Dev-Ville organ integration through GitHub Actions.

## Verified run

- Workflow: `closed-loop`
- Workflow run: `#26` (`31427270378`)
- Result: **success**
- Victor PR head: `8097bc8fcff6719b89f3e434832d80dc0d0eb2be`
- Victor base: `3cdc7397695c622b022d8bcef291dcede8419bf1`
- Dev-Ville commit executed: `f10d8b7db50a341f427f90bae8f51e1369bae6aa`
- Runtime: CPython `3.12.13` on Ubuntu `24.04.4`

## Canonical test job

- Unit tests: **6/6 passed**
- Local command-center smoke loop: **verified**
- Local artifact SHA-256: `57cb2f127d5a87f0aa24b4f33eefe4ed6a208972b60c5e018b9f0cddd5e15ee9`
- Local event chain: **6 events, valid**

The unit suite verified:

1. local closed-loop completion + chain verification;
2. external Dev-Ville receipt verification;
3. missing-adapter failure + active-work cleanup;
4. hosted-model provider fail-closed policy;
5. single-active-work-order enforcement;
6. denial of unknown/unallowlisted capabilities.

## Real Victor → Dev-Ville execution

Directive:

> Create a minimal backend API with validation and tests

Result:

- Organ: `dev-ville`
- Capability: `devville.project.build`
- Work order: `wo-caf103d0a30f4c3bb58e69cd6f6b5327`
- Job: `job-09191e7b3cca41a093683a746a6fcde0`
- Outcome: `out-6c85d4a82f6e4da3b260d6092c13e3bf`
- Project status: **completed**
- Project progress: **100%**
- Tasks: **7/7 completed**
- Tickets: **7**
- Verified artifacts: **5**
- Total verified artifact bytes: **12,736**
- Receipt SHA-256: `aa5e1e395080d1461b153f7b01aa014e345c3fc40fd50025cd17300c6fac9b42`
- Canonical receipt hash: `c0c02cb8fa4d81607d280c90426ae73655c28d53babbbb00d785399c765bcbbc`
- Final event chain: **7 events, valid**

### Artifact digests

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `design_1786392314.py` | 2,384 | `d81fdec39d17bf321cf63ed8516a3298c31f163aa5c319fd9fdf21314fa3d696` |
| `test_design_1786392314.py` | 961 | `953b13bda4fd010d6af15b9a83d5bee9ca71d8fa11d4f28fb194dbadd9f63f68` |
| `backend_1786392314.py` | 8,016 | `0ba031e7469e46e55fc6570c6e6c09b9b477040288cac4a1ef3a7c227eb80a22` |
| `test_backend_1786392314.py` | 937 | `7d2ffad4d5332ff6670afbfa7166a3c4b6949000955f9073abf9b46ba71da758` |
| `backend_config_1786392314.json` | 438 | `e7d65e0d44df51fb18a1f78d7c2a297446ea475e799691d6a75a1bd503798778` |

## Conclusion

The minimum real cross-repository closed loop is **proven operational**:

```text
Bando/Victor directive
  -> canonical work order
  -> policy governance
  -> expiring capability lease
  -> Dev-Ville dispatch
  -> bounded project execution
  -> versioned receipt
  -> independent artifact/hash verification
  -> canonical outcome commit
  -> hash-chain verification
```

This proves the execution substrate. It does **not** yet prove autonomous revenue generation, production deployment, hostile-code isolation, or a native C/C++ Victor runtime. Those remain separate gates.
