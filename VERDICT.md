# VERDICT.md

## Scope

One task bundle, `145c5287-517b-5048-a526-4a66487b9554`.

| | |
|---|---|
| **task** | `ethara/ledgerline-review` v1.0.0, schema 1.4 |
| **shape** | python + postgresql, browser-graded web application, nine deliverables |
| **graded set** | 25 compiled criteria at weight 85 (`tests/test_output.py`), 8 judged at weight 30 (`tests/judge.toml`), weighted mean over 115 |
| **inputs** | 12 statement rows, 12 receipts, one `policy.json`; reference reconciliation is 11 matched, 1 unreceipted, 1 unposted, 3 violations |
| **rollouts** | 1, at `trajectories/opus 5/run_1` — eval `claude-code__claude-opus-5__adhoc`, trial `145c5287-517b-5048-a526-4a66487b__ZUHQ6q5`, 0 errors |
| **agent image** | `localhost:5001/ledgerline-env@sha256:e5931807291ac77e5991…`, `no-network`, uid 10001 |
| **verifier image** | `localhost:5001/ledgerline-tests@sha256:1f8fb568b74b1575048…`, `environment_mode = separate`, allowlist `api.anthropic.com` |

Findings carry an attribution tag: **[task]** the bundle as authored, **[pack]** what was packaged into it, **[harness]** the run/score/record pipeline, **[spec]** the instruction as written, **[agent]** the model under test.

## Executive summary

🟡 **HOLD:PILOT_REQUIRED.** The single rollout scores `reward 0.8174` — compiled dimension 64 of 85, judged dimension 30 of 30. The reconciliation itself is exactly right: every set, sort, total, violation-flag, snapshot and inputs-unmodified criterion passes, and all eight judged prose criteria pass with the judge's own reasoning recomputing the arithmetic. The entire 21-point shortfall is grading-side, not capability. Eighteen points are the six rendered-surface criteria, which scored zero because the verifier image cannot start the agent's application; three are a prose criterion that fails on a line wrap. Correct both and this rollout reads 1.0, which is the discrimination problem as much as it is the fairness one: with the grading path fixed, this run has no measured headroom left, and n=1 on one model supports no band at all. The disposition is capped by `solution/provenance.yaml`, which records the ancestry attestation as `attestation-unavailable` and all three screening roots as `unresolved` against an ENGRAM trust root this parent does not project. Those caps are external and owed, not defects in the bundle.

## Disposition

| | |
|---|---|
| **Disposition** | HOLD:PILOT_REQUIRED |
| **Basis** | `ancestry-attestation-unavailable`, `screening-unverified` — both ENGRAM-owned roots, unresolved in this parent |

## Findings

- ✅ **[task] The reconciliation is exactly correct, recomputed rather than accepted.** `matched_set_exact`, `unreceipted_set_exact`, `unposted_set_exact`, `violation_flags_exact`, `totals_consistent`, `matched_sorted_by_receipt_id`, `reconciliation_json_wellformed` all pass at full weight. The agent's `reconciliation.json` carries 11 matched pairs, `unreceipted: ["TXN-1011"]`, `unposted: ["RCP-0012"]` and 3 violations, which is the reference reconciliation. `snapshot_table_shape` and `snapshot_agrees_with_reconciliation_json` pass, so the PostgreSQL table and its dump agree with the JSON. `inputs_unmodified` passes at weight 5.
- ✅ **[harness] The judged dimension is live and discriminating, not rubber-stamping.** All 8 criteria return `yes` at 30 of 30, each with per-pair arithmetic in its `reasoning`: the judge hand-verified the eligibility graph to confirm the 11-pair maximum matching forces `RCP-0008 → TXN-1009`, recomputed all four policy magnitudes (8100 > 7500 by 600, 9910 by 2410, 9945 by 2445, 48200 under 50000 by 1800), and checked the colon-rule attribution against the receipt bytes. It also caught and bounded one real blemish — the run calls `RCP-0011` "better on both tie-breaks" where the date deltas tie at ±1 — and reasoned that it sits in the dispute framing rather than in the operative reason. That is a judge reading sentences, not identifiers.
- ✅ **[harness] The scoring model reproduces.** `tests/reward.toml` declares `weighted-mean` with `{test_output = 85, judge = 30}`; `(64 + 30) / 115 = 0.817391…` reproduces the emitted `0.8174` exactly, and the compiled dimension's own `0.7529` reproduces as `64/85`. Weights are stated rather than defaulted, which is what keeps the compiled share at the bound `0.7391304347826086` instead of the `0.5` that `_group_weights` would produce.
- ⚠️ **[pack] The verifier image cannot start the application it grades, and the loss is 18 of 85.** `verifier/app-start.log` is one line: `/opt/pw/bin/python3: No module named uvicorn`. The verifier apt-installs `python3-uvicorn=0.27.1-1` and `python3-starlette=0.31.1-1` into the system interpreter's `dist-packages` at `tests/Dockerfile:216`, then sets `PATH=/opt/toolkit/bin:/opt/pw/bin:/opt/claude:$PATH` at line 256, so a bare `python3` resolves to the Playwright virtualenv, whose `sys.prefix` excludes exactly those packages. The agent's `start.sh` runs `exec python3 -m uvicorn … app:app`, which is correct in the agent image — `environment/Dockerfile` installs the same two packages and prepends only the PostgreSQL bin directory, so nothing shadows the interpreter there. The deliverable therefore works where it was built and fails only where it is graded, and all six rendered-surface criteria score zero for a reason that is not the agent's: `review_screen_structure` 3, `review_screen_rows_sorted` 1, `review_screen_columns_aligned` 1, `served_screen_agrees_with_reconciliation_json` 5, `detail_screens_complete` 3, `served_screens_request_no_external_origin` 5. The browser half of the task was not measured.
- ⚠️ **[spec] The reference solution carries the workaround and the instruction does not disclose it.** `solution/solver.py:776-786` emits `exec /usr/bin/python3 "$(dirname "$0")/app.py"` under a comment that names the cause outright — *the grader's image puts a virtualenv earlier on PATH whose sys.prefix excludes /usr/lib/python3/dist-packages … a bare python3 there starts and then fails to import uvicorn*. So the asymmetry was known at authoring time. `instruction.md` tells the agent that `uvicorn` with `starlette` "is installed in the image already" and that `start.sh` must "run from a clean container in which only `/app` and the input tree exist"; it never says the grading container differs from the build container, and nothing agent-visible carries the hint. The only artifact encoding the fix sits in the private tree. Either the verifier must stop shadowing the interpreter that holds the runtime, or the instruction must name the interpreter the application has to exec.
- ⚠️ **[harness] `parse_notes_quotes_the_correct_value` fails on a line wrap, not on content.** The criterion requires each non-field line of a hazard receipt to appear in that receipt's section, tested as a verbatim substring of the section body joined by newlines (`tests/checks.py:641-661`). The run's `parse-notes.md` covers the right three receipts — `RCP-0003` and `RCP-0007` for the first-colon rule, `RCP-0004` for the ignored-lines rule — attributes each rule correctly, and quotes both of `RCP-0004`'s trailing lines. But it hard-wraps one of them as `Invoice\ngenerated automatically; retain for records.`, so the substring test misses it while every other string the checker looks for is present, `Thank you for your business.` and both full vendor values included. The deliverable is specified as English prose with no line-length rule and explicitly says "no length is prescribed", so a prose criterion must not be decidable by where a line breaks. Normalise whitespace before the containment test. Weight 3.
- ⚠️ **[harness] One rollout, one model, no paired reading, so nothing here bands.** `result.json` records `n_total_trials: 1` and `pass_at_k: {}`. `trajectories/` holds only `opus 5/run_1`; there is no second lane. `task.toml` declares `discrimination_weight = 0.5` and a measured `null_floor = 0.0435`, and the floor is sound — `provenance.yaml` records that the no-op control banks `inputs_unmodified` at 5 of 115 and is rejected by the other 32 graded items — but a single observation supports neither a difficulty band nor a determinism claim. With the two grading defects above corrected this rollout reads 115 of 115, so the measurement that exists is also the measurement with no headroom in it.
- ⚠️ **[task] `snapshot.sql` staleness is specified but not independently measured here.** The instruction warns that a correct table with a stale dump "scores as if the table were absent", and `snapshot_agrees_with_reconciliation_json` passes on this rollout. That is the agreeing case only. The disagreeing case is asserted by the deliverable text and by the reference solver's ordering comment, not demonstrated by a control in this bundle's recorded output.

## Not measured this cycle

Recorded because a scored run is not a complete one.

1. **Contamination screening.** `provenance.yaml` carries `exclusion_list_root`, `freeze_date_table_root` and `neardup_index_root` all at `state: unresolved`, with `near_duplicate_screening` likewise unresolved for want of a calibrated threshold and `freeze_holdout` `not-applicable` for a synthetic original. `screening_measured_at` and `screening_expires_at` stay null by design rather than stamping an unresolved root. FORGE authors none of these roots.
2. **Attestation.** Both `ancestry_attestation` and `event_attestation` read `state: attestation-unavailable`; no trust root is projected in this parent to resolve a signature against.
3. **The rendered surface.** Zero of the three render obligations in `instruction.md` — font-weight 700/400 legibility, `document.fonts.check` at each graded element's own computed style, and column left-edge alignment to one device-independent pixel at 1280×800 — were exercised, because the application never answered. The criteria are compiled and the browser is pinned in the image; they have simply never run green.
4. **Canary liveness.** The four `FORGE-CANARY-SLOT-*` tokens are planted across `solution/TRUTH.md`, `solve.sh` and `provenance.yaml`, and the private boundary holds on inspection, but no positive-control read was performed to show the boundary check can fire.

## How to reproduce

```
cd "trajectories/opus 5/run_1/145c5287-517b-5048-a526-4a66487b__ZUHQ6q5/verifier"
cat reward.json                    # 0.8174
cat app-start.log                  # No module named uvicorn
python3 -c "import json;d=json.load(open('reward-details.json'))['reward'];
print([(c['name'],c['weight'],c['detail']['score']) for c in d['components']])"
```

`(64 + 30) / 115` is the emitted reward. The six zeros are the rendered-surface criteria and the seventh is `parse_notes_quotes_the_correct_value`; every other graded item passes. The interpreter shadow reproduces from `tests/Dockerfile:216` against `tests/Dockerfile:256` without running anything.

## Flag legend

✅ pass, recomputed from committed bytes. ⚠️ defect, stale claim, or coverage gap. 🟡 HOLD. Attribution: **[task]** authored bundle, **[pack]** packaging, **[harness]** run/score/record pipeline, **[spec]** instruction as written, **[agent]** model under test.
