"""Workflow runner for verifiers.

A *workflow* is a pre-authored path through the deployed application (the Vibe Code Bench unit).
It is a function of a Ctx; each `with ctx.step("..."):` block is one substep. A workflow passes
only when every one of its substeps passes (strict gating), and a failing substep stops the
workflow: later substeps are recorded as not reached. Breach detectors are recorded with
`ctx.breach(code, detail)` from anywhere; scoring caps functional credit when any breach fired.

Results go to one JSON document:
{
  "workflows": {id: {"title", "weight", "passed", "steps": [{"name","ok","detail","seconds"}], "error"}},
  "breaches": {code: [detail, ...]},
  "surfaces": {name: bool},        # surfaces that answered at least once (prohibition rule)
  "observations": {...},           # free-form evidence for audit
}
Nothing here scores. tests/scoring.py turns the document into Reward Kit criteria.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable


class StepFailed(AssertionError):
    pass


class InfraError(RuntimeError):
    """A failure of the verifier or its services, never of the submission. Aborts the run."""


class WorkflowTimeout(BaseException):
    pass


@dataclass
class Workflow:
    wid: str
    title: str
    weight: float
    fn: Callable
    timeout: int
    order: int
    critical: bool = False


REGISTRY: list[Workflow] = []


def workflow(wid: str, title: str, weight: float = 1.0, timeout: int = 900, critical: bool = False):
    """critical=True marks a core business-correctness workflow (scoring may cap the run when one fails)."""
    def deco(fn):
        REGISTRY.append(Workflow(wid, title, float(weight), fn, timeout, len(REGISTRY), critical))
        return fn
    return deco


@dataclass
class Results:
    workflows: dict = field(default_factory=dict)
    breaches: dict = field(default_factory=dict)
    surfaces: dict = field(default_factory=dict)
    observations: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"workflows": self.workflows, "breaches": self.breaches, "surfaces": self.surfaces,
                "observations": self.observations}


class Ctx:
    def __init__(self, results: Results, wf: Workflow, shared: dict):
        self.results, self.wf, self.shared = results, wf, shared
        self.record = {"title": wf.title, "weight": wf.weight, "critical": wf.critical, "passed": False, "steps": [],
                       "error": None}
        results.workflows[wf.wid] = self.record

    @contextmanager
    def step(self, name: str):
        t0 = time.monotonic()
        entry = {"name": name, "ok": False, "detail": "", "seconds": 0.0}
        self.record["steps"].append(entry)
        try:
            yield entry
            entry["ok"] = True
        except StepFailed as e:
            entry["detail"] = str(e)[:2000]
            raise
        except (InfraError, WorkflowTimeout):
            raise
        except Exception as e:  # an application misbehaviour surfaced as an exception
            entry["detail"] = f"{type(e).__name__}: {e}"[:2000]
            raise StepFailed(entry["detail"]) from e
        finally:
            entry["seconds"] = round(time.monotonic() - t0, 3)

    def check(self, cond, msg: str = "check failed"):
        if not cond:
            raise StepFailed(msg)
        return cond

    def eq(self, got, want, msg: str = ""):
        if got != want:
            raise StepFailed(f"{msg} expected {want!r}, got {got!r}"[:2000])
        return got

    def breach(self, code: str, detail: str) -> None:
        self.results.breaches.setdefault(code, []).append(f"[{self.wf.wid}] {detail}"[:1000])

    def surface(self, name: str, answered: bool = True) -> None:
        if answered:
            self.results.surfaces[name] = True

    def observe(self, key: str, value) -> None:
        self.results.observations.setdefault(self.wf.wid, {})[key] = value


def _alarm(signum, frame):
    raise WorkflowTimeout()


def run_all(out_path: str, shared: dict, only: set[str] | None = None) -> Results:
    results = Results()
    signal.signal(signal.SIGALRM, _alarm)
    # FK_BUDGET_SECONDS (set by test.sh from the verifier timeout, minus time kept for scoring) bounds the
    # whole run: each workflow's alarm is clamped to what is left, and workflows that cannot start are
    # recorded as failed, so a slow application keeps every workflow it finished instead of the verifier
    # being killed and the run scoring the fail-closed zero.
    budget = float(os.environ.get("FK_BUDGET_SECONDS") or 0)
    run_start = time.monotonic()
    for wf in sorted(REGISTRY, key=lambda w: w.order):
        if only and wf.wid not in only:
            continue
        ctx = Ctx(results, wf, shared)
        t0 = time.monotonic()
        print(f"[wf] {wf.wid} {wf.title}", flush=True)
        left = budget - (t0 - run_start) if budget else None
        if left is not None and left < 30:
            ctx.record["error"] = "verifier time budget exhausted before this workflow could start"
            ctx.record["seconds"] = 0.0
            print(f"[wf] {wf.wid} FAIL 0s {ctx.record['error']}", flush=True)
            _write(out_path, results)
            continue
        limit = wf.timeout if left is None else max(1, min(wf.timeout, int(left)))
        signal.alarm(limit)
        try:
            wf.fn(ctx)
            ctx.record["passed"] = all(s["ok"] for s in ctx.record["steps"]) and bool(ctx.record["steps"])
        except StepFailed as e:
            ctx.record["error"] = str(e)[:2000]
        except WorkflowTimeout:
            ctx.record["error"] = f"workflow exceeded {limit}s" + (" (verifier time budget)" if limit < wf.timeout else "")
        except InfraError:
            signal.alarm(0)
            _write(out_path, results)
            raise
        except Exception as e:  # noqa: BLE001 - verifier bug or app-induced crash outside a step
            ctx.record["error"] = f"{type(e).__name__}: {e}"[:2000]
            ctx.record["traceback"] = traceback.format_exc()[-4000:]
        finally:
            signal.alarm(0)
        ctx.record["seconds"] = round(time.monotonic() - t0, 1)
        print(f"[wf] {wf.wid} {'PASS' if ctx.record['passed'] else 'FAIL'} {ctx.record['seconds']}s "
              f"{ctx.record['error'] or ''}"[:400], flush=True)
        _write(out_path, results)
    _write(out_path, results)
    return results


def _write(path: str, results: Results) -> None:
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(results.to_json(), fh, indent=1, default=str)
    import os
    os.replace(path + ".tmp", path)


def main_guard(fn) -> None:
    try:
        fn()
    except InfraError as e:
        print(f"verifier infrastructure failure: {e}", file=sys.stderr)
        sys.exit(3)
