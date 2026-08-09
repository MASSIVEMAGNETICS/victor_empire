from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from victor_runtime import VictorKernel


HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Victor Command Center</title>
<style>
body{font-family:system-ui,sans-serif;background:#0b0d10;color:#e8eef2;max-width:1000px;margin:0 auto;padding:24px}
h1{margin-bottom:4px}.sub{color:#94a3b8;margin-top:0}.panel{background:#131820;border:1px solid #273244;border-radius:14px;padding:18px;margin:16px 0}
textarea{width:100%;min-height:100px;background:#090c10;color:#fff;border:1px solid #334155;border-radius:10px;padding:12px;box-sizing:border-box}
button{background:#fff;color:#000;border:0;border-radius:10px;padding:11px 16px;font-weight:700;margin-top:10px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#090c10;padding:14px;border-radius:10px}
</style>
</head>
<body>
<h1>Victor Command Center</h1>
<p class="sub">Canonical state • governed execution • verification • continuity</p>
<div class="panel">
  <h2>Command</h2>
  <textarea id="cmd" placeholder="Type an objective. Example: Produce today's highest-leverage execution receipt."></textarea>
  <button onclick="runCommand()">Execute closed loop</button>
</div>
<div class="panel">
  <h2>System state</h2>
  <pre id="status">loading...</pre>
</div>
<div class="panel">
  <h2>Last result</h2>
  <pre id="result">none</pre>
</div>
<script>
async function refresh(){
  const r=await fetch('/api/status'); const j=await r.json();
  document.getElementById('status').textContent=JSON.stringify(j,null,2);
}
async function runCommand(){
  const command=document.getElementById('cmd').value.trim();
  if(!command)return;
  const r=await fetch('/api/command',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({command})});
  const j=await r.json();
  document.getElementById('result').textContent=JSON.stringify(j,null,2);
  await refresh();
}
refresh();
</script>
</body>
</html>
"""


def make_handler(kernel: VictorKernel):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/":
                body = HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "text/html; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/status":
                self._json(200, kernel.status())
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/api/command":
                self._json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("content-length", "0"))
                raw = self.rfile.read(length)
                data = json.loads(raw.decode("utf-8"))
                command = str(data.get("command", "")).strip()
                if not command:
                    raise ValueError("command is required")
                if command.lower() == "status":
                    result = kernel.status()
                elif command.lower() in {"verify", "verify chain", "verify-chain"}:
                    ok, count, error = kernel.events.verify()
                    result = {"ok": ok, "events": count, "error": error}
                else:
                    result = kernel.run_closed_loop(command)
                self._json(200, result)
            except Exception as exc:
                self._json(400, {"error": type(exc).__name__, "message": str(exc)})

        def log_message(self, fmt: str, *args) -> None:
            return

    return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Victor Command Center")
    parser.add_argument("--data-dir", default=".victor")
    parser.add_argument("--workspace", default="artifacts")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the local command center")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)

    run = sub.add_parser("run", help="Execute one closed loop")
    run.add_argument("goal")

    sub.add_parser("status", help="Show canonical runtime state")
    sub.add_parser("verify-chain", help="Verify the hash-chained event ledger")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    kernel = VictorKernel(data_dir=Path(args.data_dir), workspace=Path(args.workspace))

    if args.command == "serve":
        server = ThreadingHTTPServer((args.host, args.port), make_handler(kernel))
        print(f"Victor Command Center: http://{args.host}:{args.port}")
        server.serve_forever()
    elif args.command == "run":
        print(json.dumps(kernel.run_closed_loop(args.goal), indent=2))
    elif args.command == "status":
        print(json.dumps(kernel.status(), indent=2))
    elif args.command == "verify-chain":
        ok, count, error = kernel.events.verify()
        print(json.dumps({"ok": ok, "events": count, "error": error}, indent=2))


if __name__ == "__main__":
    main()
