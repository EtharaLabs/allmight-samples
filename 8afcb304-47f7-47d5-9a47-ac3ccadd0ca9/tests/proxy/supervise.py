#!/usr/bin/env python3
"""Run Reward Kit under a watch on the credential proxy (contract C7, K7).

Reward Kit scores a judge request that its model client abandons as a timeout as 0.0 and still
writes reward.json. The proxy answers every upstream failure or timeout with 502 or 503 before
that client timeout, which Reward Kit does not catch, so it aborts without a reward file. The one
case left is a proxy that itself stops answering. This supervisor closes it: it runs rewardkit as
a child, polls the proxy's GET /healthz every POLL seconds, and kills the child's whole process
group before it can write anything when the proxy fails to answer for STALL seconds or reports a
request in flight for longer than its own deadline plus MARGIN seconds. STALL and the deadline
plus MARGIN are both shorter than the judge timeout in tests/judge.toml, so the kill always lands
before the model client could time out.

Usage: supervise.py --health http://127.0.0.1:<port>/healthz -- rewardkit /tests --workspace /app
Exit status: rewardkit's own, or 75 when the supervisor killed it. Killing never touches
reward.json: Reward Kit writes it only after every judge call has returned.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

KILLED = 75


def _health(url: str, timeout: float) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except Exception:  # noqa: BLE001 - any failure to answer counts as a stall
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--health", required=True)
    parser.add_argument("--poll", type=float, default=2.0)
    parser.add_argument("--stall", type=float, default=20.0)
    parser.add_argument("--margin", type=float, default=15.0)
    parser.add_argument("--report", default="")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    child = subprocess.Popen(command, start_new_session=True)
    last_ok = time.monotonic()
    reason = ""
    while child.poll() is None:
        time.sleep(args.poll)
        health = _health(args.health, timeout=args.poll)
        now = time.monotonic()
        if health is not None:
            last_ok = now
            limit = float(health.get("deadline_s", 0)) + args.margin
            if float(health.get("oldest_inflight_s", 0)) > limit:
                reason = f"a proxied request stayed in flight beyond {limit:.0f}s"
        elif now - last_ok > args.stall:
            reason = f"the proxy did not answer its health check for {args.stall:.0f}s"
        if reason and child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            sys.stderr.write(f"supervise: killed rewardkit: {reason}\n")
            if args.report:
                with open(args.report, "w", encoding="utf-8") as handle:
                    json.dump({"killed": True, "reason": reason}, handle)
            return KILLED
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump({"killed": False, "returncode": child.returncode}, handle)
    return child.returncode


if __name__ == "__main__":
    raise SystemExit(main())
