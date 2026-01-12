# victor_empire
empire builder

## Roadmap

The project direction lives in the [roadmap](./roadmap). It outlines Victor’s NZT architecture, the six core modules to ship first, and an end-to-end path from personal-grade MVP to production- and enterprise-grade deployments:

1. victor_nzt_mode_router.py
2. work_order.py
3. single_thread_enforcer.py
4. runway_ledger.py
5. inbox_capture.py
6. nzt_session.py (ties it together; CLI first)

Use the roadmap as the source of truth for planning and execution.

## Quickstart (personal-grade CLI)

```bash
python nzt_session.py --start --goal "Ship MVP" --next "Draft CLI" --inbox "idea: add web UI"
```

This creates a work order file, appends to the runway ledger, captures inbox items, and enforces a single active work order.
