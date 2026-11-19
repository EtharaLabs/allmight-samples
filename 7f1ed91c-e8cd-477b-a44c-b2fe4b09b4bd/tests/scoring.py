"""Reward Kit criteria for tasks: one binary criterion per workflow, capped by breaches.

tests/workflows.py writes /logs/verifier/results.json. Each workflow criterion scores

    1.0 * cap   when every substep of that workflow passed, else 0.0,

where cap = BREACH_CAP (0.4) when any breach detector fired anywhere in the run, or when any workflow
marked `critical = true` in tests/workflows.toml failed and [scoring].critical_cap (0.4) is set;
otherwise 1.0. With reward = 0.9 * functional + 0.1 * judge (tests/reward.toml), one observed breach or
one failed critical workflow caps the reward at 0.9 * 0.4 + 0.1 = 0.46; the reward stays in [0, 1].

The functional/ directory registers its workflows from tests/workflows.toml; this module registers
nothing at the tests root.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from rewardkit import criterion

RESULTS = "/logs/verifier/results.json"
WORKFLOWS_TOML = Path(__file__).resolve().parent / "workflows.toml"
BREACH_CAP = 0.4


def _results() -> dict:
    try:
        with open(RESULTS, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _spec() -> dict:
    try:
        return tomllib.loads(WORKFLOWS_TOML.read_text())
    except (OSError, ValueError):
        return {}


def _passed(wf: dict) -> bool:
    return bool(wf.get("passed")) and bool(wf.get("steps")) and all(s.get("ok") for s in wf["steps"])


def breach_cap() -> float:
    """Multiplier for every workflow: BREACH_CAP on any observed breach; critical_cap on any failed critical workflow."""
    res = _results()
    cap = BREACH_CAP if res.get("breaches") else 1.0
    spec = _spec()
    critical_cap = float((spec.get("scoring") or {}).get("critical_cap", 1.0))
    if critical_cap < 1.0:
        flows = res.get("workflows", {})
        # A critical workflow that failed only because the verifier's overall time budget ran out (it could not
        # start, or its alarm was clamped to the time left) scores 0 itself but does not trigger the cap: the spec
        # sets no throughput, so slowness alone must not cap the credit a working application earned elsewhere.
        def _budget_only(wf: dict) -> bool:
            return "verifier time budget" in str(wf.get("error") or "")
        if any(w.get("critical") and not _passed(flows.get(w["id"]) or {}) and not _budget_only(flows.get(w["id"]) or {})
               for w in spec.get("workflow", [])):
            cap = min(cap, critical_cap)
    return cap


@criterion(shared=True)
def workflow_passed(workspace: Path, wid: str) -> float:
    wf = _results().get("workflows", {}).get(wid) or {}
    return (1.0 if _passed(wf) else 0.0) * breach_cap()


def register_functional() -> None:
    import rewardkit

    spec = _spec()
    for w in spec["workflow"]:
        rewardkit.workflow_passed(w["id"], name=w["id"], weight=float(w["weight"]))
