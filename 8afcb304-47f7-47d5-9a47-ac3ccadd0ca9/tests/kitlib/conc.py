"""Run callables at the same moment: every thread waits on one barrier, then fires."""

from __future__ import annotations

import threading
from typing import Callable


def simultaneously(fns: list[Callable], timeout: float = 120.0) -> list:
    barrier = threading.Barrier(len(fns))
    out: list = [None] * len(fns)

    def run(i, fn):
        try:
            barrier.wait(timeout=30)
            out[i] = ("ok", fn())
        except Exception as e:  # noqa: BLE001
            out[i] = ("error", e)

    threads = [threading.Thread(target=run, args=(i, f), daemon=True) for i, f in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    return out
