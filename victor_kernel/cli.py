from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .kernel import VictorKernel


def _kernel(db: str) -> VictorKernel:
    key = os.environ.get("VICTOR_KERNEL_SIGNING_KEY")
    if not key:
        raise SystemExit(
            "Set VICTOR_KERNEL_SIGNING_KEY to a secret of at least 16 bytes before using the kernel."
        )
    return VictorKernel(db, signing_key=key.encode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Victor transactional control-plane kernel")
    parser.add_argument("--db", default="victor_kernel.db")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("verify")
    sub.add_parser("recover")
    args = parser.parse_args()

    kernel = _kernel(args.db)
    try:
        if args.command == "init":
            print(json.dumps({"db": str(Path(args.db)), "schema": kernel.store.schema_version()}, indent=2))
        elif args.command == "verify":
            result = kernel.store.verify_integrity()
            print(json.dumps(result, indent=2))
            raise SystemExit(0 if result["ok"] else 2)
        elif args.command == "recover":
            print(json.dumps(kernel.recover(), indent=2))
    finally:
        kernel.close()


if __name__ == "__main__":
    main()
