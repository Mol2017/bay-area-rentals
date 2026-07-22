"""Local dev server: static files + a /api/refresh endpoint.

Usage::

    python3 scripts/serve.py [--port 8765] [--host 127.0.0.1]

A thin wrapper around stdlib ``http.server`` that serves the repo root (so
both ``/web/*`` and ``/data/*`` resolve) and additionally handles::

    POST /api/refresh
        Runs scripts/run_all_scrapers.py then scripts/merge.py and returns
        {"status": "ok", "units": N, "duration_s": ...}.

A single lock guards concurrent refreshes -- a second request while one is
running gets 409 rather than launching a duplicate scrape of 30 sites.

Unlike the reference project this deliberately does *not* delete the existing
raw files first: ``run_all_scrapers.py``'s defensive write needs the previous
run's data in place to protect against a source that fails or returns empty.
"""
from __future__ import annotations

import argparse
import http.server
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MERGED = REPO / "data" / "merged.json"

_refresh_lock = threading.Lock()


def run_refresh() -> dict:
    started = time.monotonic()

    scrape = subprocess.run(
        [sys.executable, "scripts/run_all_scrapers.py"],
        cwd=REPO, capture_output=True, text=True,
    )
    if scrape.returncode != 0:
        raise RuntimeError(f"scrape failed (rc={scrape.returncode}): {scrape.stderr[-1500:]}")

    merge = subprocess.run(
        [sys.executable, "scripts/merge.py"],
        cwd=REPO, capture_output=True, text=True,
    )
    if merge.returncode != 0:
        raise RuntimeError(f"merge failed (rc={merge.returncode}): {merge.stderr[-1500:]}")

    if not MERGED.exists():
        raise RuntimeError("merge ran but produced no merged.json")

    data = json.loads(MERGED.read_text())
    return {
        "status": "ok",
        "units": data.get("total_units", 0),
        "sources": len([s for s in data.get("sources", []) if s.get("unit_count")]),
        "duration_s": round(time.monotonic() - started, 1),
        "scrape_log": scrape.stdout[-2000:],
        "merge_log": merge.stdout[-500:],
    }


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(REPO), **kwargs)

    def end_headers(self):
        # Always fresh, so a refresh is visible on reload.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def do_POST(self):  # noqa: N802 (stdlib naming)
        if self.path != "/api/refresh":
            self.send_error(404, "no POST handler for this path")
            return
        if not _refresh_lock.acquire(blocking=False):
            self._send_json(409, {"status": "busy",
                                  "message": "another refresh is already running"})
            return
        try:
            print(f"[serve] refresh started by {self.client_address[0]}", flush=True)
            try:
                result = run_refresh()
            except Exception as e:  # noqa: BLE001
                print(f"[serve] refresh failed: {e}", flush=True)
                self._send_json(500, {"status": "error", "message": str(e)})
                return
            print(f"[serve] refresh ok: {result['units']} units in "
                  f"{result['duration_s']}s", flush=True)
            self._send_json(200, result)
        finally:
            _refresh_lock.release()

    def _send_json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), format % args))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    with http.server.ThreadingHTTPServer((args.host, args.port), Handler) as httpd:
        print(f"serving on http://{args.host}:{args.port}/web/", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nshutting down", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
