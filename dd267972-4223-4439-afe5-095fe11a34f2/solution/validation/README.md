# Validation notes

What was checked, how, and what is still open. Every number here was measured on a real
Harbor run; nothing is asserted from reading alone. Where something was not executed, it
says so.

## Setup

- Harbor 0.25.0 (`uvx --python 3.13 --from harbor==0.25.0 harbor`), engine `docker`,
  one attempt per trial.
- Native `linux/amd64` host. The environment and verifier images build and the
  `pg_isready` health check passes.
- The judge model is reached through a local Anthropic-compatible endpoint, so
  `judge` scores below are real model output rather than a stub.

## Baseline

| Run | Workflows | functional | judge | reward | n_errors | wall |
|---|---|---|---|---|---|---|
| oracle | 23 passed, 0 failed | 1.0 | 0.7 | 0.97 | 0 | 926 s |
| nop | — | 0.0 | 0.0 | 0.000 | 0 | ~80 s |

`reward = 0.9 * functional + 0.1 * judge`, so with `functional = 1.0` the reference
solution's reward is bounded by the judge's subjective score and **1.0 is not reachable**.
The meaningful signal is `functional = 1.0` with `nop = 0`: the reference solution satisfies
every strict workflow, and an untouched environment satisfies none.

## The grader alone cannot catch a wrong pay rule

The reference pay engine in `tests/reference/payroll.py` and the solution's engine in
`solution/app/shiftbook/engine.py` are the same code — they differ only in the opening
docstring. Grading the solution with the reference engine therefore cannot detect a rule
that is wrong in both at once.

This was tested rather than assumed. `UNIT_S` was changed from `360` to `300` in **both**
engines identically — the shape of a genuine shared bug, and a direct violation of
`instruction.md:131`, which requires every boundary to be a multiple of six minutes:

| Run | Workflows | functional | judge | reward |
|---|---|---|---|---|
| baseline | 23 passed, 0 failed | 1.0 | 0.7 | 0.97 |
| 5-minute rounding units | **23 passed, 0 failed** | **1.0** | **0.7** | **0.97** |

The two runs are indistinguishable. `tests/workflows.py` contains no numeric cent
comparisons, and `tests/reference/ties.json` stores only inputs — each entry carries
`['punches', 'rates']` and no expected amounts — so at verification time the expected pay
could only come from the engine under test.

## What closes it

`tests/reference/payroll.py` now defines `selftest()`, called from `tests/test.sh` before
any workflow runs. A failure marks the evaluation invalid.

Its expected values are **transcribed from the six worked examples in `instruction.md`
lines 164-179** — the specification's own numbers, not the engine's output. That
independence is what lets it catch a rule that is wrong on both sides.

Seven cases are pinned:

| Case | What it fixes in place |
|---|---|
| R5.2 (`instruction.md:131`) | six-minute rounding of a punch instant |
| Example 1 | CA daily OT and DT on a 13-hour shift with a 30-minute break |
| Example 2 | FLSA weekly OT at 40 hours |
| Example 3 | weighted regular rate across two jobs at different rates |
| Example 4 | seventh-day OT with seven meal premiums |
| Example 5 | six-minute rounding end to end, including a meal premium |
| Example 6 | DST fall-back splitting one shift across two workweeks |

Examples 3, 4 and 6 passed on their first run, which independently confirms the engine
implements the weighted regular rate, the seventh-day rule and the fall-back split
correctly — those were not adjusted to fit.

A seventh case covers the rounding tie, which none of the author's six examples reach
because they all use round rates and whole or tenth hours. It is derived by hand from the
instruction: 30 h `COOK` at 2400 plus 15 h `SERVER` at 1653 gives straight-time pay of
96795 over 45.0 hours, so the regular rate is exactly 2151 and the OT premium is
5 × 2151/2 = **5377.5 cents, an exact half cent**. `instruction.md:155` states that half a
cent rounds up, so the pinned answer is 5378 and a gross of 102173.

## Planted defects, and whether the self-test catches them

Each defect was applied to the engine and the self-test run against it.

| Defect | Rule broken | Caught |
|---|---|---|
| `half_up` tie `2*r >= d` → `2*r > d` | `instruction.md:155`, half a cent rounds up | yes — `tie OT premium: 5377.5 cents must round UP to 5378` |
| `UNIT_S` 360 → 300 | `instruction.md:131`, six-minute units | yes — `08:02:59 must round down to 08:00` |
| OT premium `rr/2` → `rr` | `instruction.md:144`, OT earns half the regular rate | yes — example 1 OT premium 9600 vs 4800 |
| DT premium `rr` → `rr/2` | `instruction.md:144`, DT earns the whole regular rate | yes — example 1 DT premium 600 vs 1200 |
| none (control) | — | passes cleanly, no false positive |

The first of these is the one the 23 workflows provably miss, and the one the hand-derived
tie case exists to cover.

The wiring was tested as well as the function: with a mutated engine in place, the
`tests/test.sh` block fails the run through `invalid` rather than passing silently.

## Not verified

- **The defects above were run against the self-test, not through a full Harbor trial.**
  The `UNIT_S` case was also run as a full trial, which is how the table in the previous
  section was produced; the other three were not.
- **`tests/reference/ties.json` still pins no expected amounts.** Its entries hold inputs
  only. `tests/reference/gen_ties.py` states the plans are built so that "a 28-digit Decimal
  and a float rate-first computation would round the wrong way, so they discriminate exact
  arithmetic" — the scenarios were designed to discriminate, but no value was recorded, so
  nothing stored would catch the engine rounding them wrongly. The self-test now covers the
  tie direction; pinning these would add breadth beyond the one derived case.
- **No mutation catalogue is shipped.** Sibling tasks `145c5287` and `8afcb304` ship a
  `mutants.py` enumerating defect-to-criterion mappings. The four defects above are recorded
  here in prose instead; a catalogue asserting mappings that were never executed would be
  worse than this.
- The judge's 0.7 was not investigated. It is a subjective score over ten experience
  criteria and does not indicate a functional defect.
