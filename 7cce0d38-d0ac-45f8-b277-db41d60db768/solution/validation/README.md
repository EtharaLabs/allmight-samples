# Seatwise validation (local, 2026-11-10)

> **Database version.** The runs recorded here used Postgres 17, but the
> delivered images pin `postgresql-15=15.19-0+deb12u1` in both
> `environment/Dockerfile` and `tests/Dockerfile`. This record was therefore
> produced on a newer server than the task actually ships, and the results
> below have not been re-measured on 15.

Local runs use the shared Postgres 17 (database and role `seatwise` only), the kit's mail sink and payments
mock on this task's ports (payments admin over a loopback port, because the kit's admin socket uses Linux
`SO_PEERCRED`), and the oracle started from a fresh copy of `solution/app` with the verifier's TTLs
(`HOLD_TTL_SECONDS=120`, `WAITLIST_OFFER_TTL_SECONDS=120`, `ACCOUNT_LINK_TTL_SECONDS=20`). Every run starts
from a freshly loaded seed and takes the baseline snapshot before the app starts, exactly like
`tests/test.sh`. W27's database "stop/start" hooks kill and restart a local TCP proxy in front of the shared
Postgres (the shared server itself must not be stopped); the real `pg_ctl stop/start` happens only in the
verifier container on EC2.

## Oracle

All 27 workflows PASS with zero breaches on a fresh database (run listed at the end of this file). The money
engine check in W06 compares 116 orders line by line, 92 of them from `tests/reference/ties.json` (orders where
28-digit Decimal shares, float shares, round-half-even, or all-float arithmetic give a different breakdown than
the exact rules; 32 / 47 / 74 / 49 breaking cases respectively), plus 25 seat-order orders on a venue whose
sections were created out of order.

## Calendar horizon

`valid_until = "2029-09-30"` (task.toml [metadata]). Every date the grader needs in the future is either
derived at run time or lies far out:

- the DST cases of W03, W11 and W16 use the next real transitions of the venue's zone after the run starts
  (`harness.next_transition`, from zoneinfo), and every expected marker is computed from those dates;
- the old platform's upcoming events lie in 2029 (seed `solution/seed/generate.py`); the grader picks legacy
  events, buyers, holders and comp tickets relative to the run's own clock;
- event times the grader plans from its own clock never fall in the second pass of a fall-back's repeated hour
  (which a typed wall-clock time cannot name, R5.2): `harness.start_in` skips such minutes (at most an hour later),
  W11 creates each deadline event at whichever of its New York and Chicago venues names the instant exactly, and
  W04 uses America/Phoenix when Los Angeles is within 20 minutes of its repeated hour; simulated "now" values every
  minute through the 2027-11-07 fall-back (New York, Chicago, Los Angeles) confirm it (authoring script);
- simulating those picks month by month (authoring script) shows all of them hold through 2029-10-09; the first
  to run dry is W23's "Saguaro event more than 30 days away" pick. After `valid_until`, regenerate the seed with
  later upcoming dates.

## Planted defects

Each mutant is a copy of `solution/app` with exactly one realistic change (patches in the author's scratch
`mutants/snips/`), run with `--only <intended workflow>,W05` on a fresh database. W05 (the plain purchase
path) is the control: it passed against every mutant.

| Mutant | Lever | Change | Intended workflow | Result | Breach |
|---|---|---|---|---|---|
| m1_decimal_alloc | money allocation | largest remainder computed on 28-digit `Decimal` shares | W06 (critical) | FAIL: 6+ fixture orders differ by a cent (e.g. discount 1051 vs 1052) | — |
| m2_late_always_paid | webhook arriving after the state moved on | a completed payment for an expired order is always confirmed, even when its seat was resold | W08 (critical) | FAIL: late payment `paid`, want `unavailable` | X6 (seat sold twice) |
| m3_no_hold_lock | concurrency | the per-event lock around seat holds removed | W10 (critical) | FAIL: 5 of 8 racing buyers held the same seat | X6 |
| m4_cross_org_refund | tenancy / IDOR | organizer refund no longer checks that the ticket belongs to the URL's organization | W02 | FAIL: cross-organization refund took effect | X1 |
| m5_no_waitlist_offers | missing secondary feature | the worker never makes waiting-list offers | W17 | FAIL: no offer email to the entry that fits | — |
| m6_local_day_deadlines | temporal (DST) | refund deadlines computed as 7 and 2 local calendar days instead of 168 / 48 elapsed hours | W11 (critical) | FAIL: 100 % deadline `2027-03-14T01:00:00Z`, want `2027-03-14T00:00:00Z` (and the fall-back case) | — |
| m7_last_scan_wins | out-of-order offline events | the last scan received becomes the admission instead of the earliest | W16 (critical) | FAIL: admission 06:25 (later row), want 06:20 | — |
| m8_reset_link_outbox | cross-cutting secrets | the reset email is queued in the outbox table with its plain link (instead of sent directly) | W26 | FAIL: `sw_outbox.body` holds the plain reset link | X8 |

Hardening round (checks added through the reference model, no new workflows):

| Mutant | Lever (audit item) | Change | Intended workflow | Result | Breach |
|---|---|---|---|---|---|
| m9_offer_closed_on_cancel | A1: R19 offer status after a cancellation | cancelling marks open offers `closed` instead of `expired` | W13 (critical) | FAIL: `data-offer-status 'closed' is not one of open|accepted|expired|declined` | — |
| m10_window_before_reasons | B2: R13.3 refusal order | `outside_window` checked before `cancelled` / `refunded` / `transferred` | W16 (critical) | FAIL: batch 4 rows at S − 8 h answer `outside_window`, want `refunded` / `transferred` | — |
| m11_topup_ignores_pending | B5: R10.6 top-up counts pending refunds | cancellation top-up subtracts only completed refunds | W13 (critical) | FAIL: the over-refund is refused by the provider, so the ticket gets `[2475]`, want `[2475, 2475]` | — |
| m12_late_ignores_holds | B6: R8.4 / R6.1 availability | a late payment is confirmed when its seat is merely held (not sold) | W08 (critical) | FAIL: late payment `paid`, want `unavailable` | X6 |
| m13_lines_by_seat_id | B8: R4.3 / R9.3 seat order | order lines sorted by seat id instead of seat order | W06 (critical) | FAIL: `ALP-A-1` tax 200 want 201 (sections created ZED, MID, ALP) | — |

All 13 mutants were re-run against the hardened workflows: each fails its intended workflow and W05 passes.

Because W06, W08, W10, W11 and W16 are `critical = true` (with `[scoring] critical_cap = 0.4`), mutants
m1, m2, m3, m6 and m7 would also cap every workflow at 0.4; m2, m3, m4 and m8 also cap through their breach.

## No-op agent

An empty workspace (no `start.sh`): the app never answers `/healthz`; all 27 workflows FAIL at their first
substep and no breach is recorded, so functional = 0 and the reward is 0 (the judge sees only "CHECKPOINT NOT
REACHED" evidence).

## Not verified locally

- The real PostgreSQL stop/start of W27 (`pg_ctl` in the verifier container), the Unix-socket payments admin
  surface, and the Docker images: these need the EC2 run (oracle = 1.0, nop = 0.0).
- The judge (fable-5.1) was not run locally; the evidence directories under `FK_UX_DIR` are produced by W05
  and W24 and were inspected by hand.

## Final local run

Fresh database from the regenerated seed, verifier overrides as shipped (HOLD_TTL_SECONDS=120,
WAITLIST_OFFER_TTL_SECONDS=120, ACCOUNT_LINK_TTL_SECONDS=20), 2026-11-11 16:18-16:55 IST (36 min 56 s; the
verifier's timeout is 10800 s, of which test.sh gives the workflows 8100 s minus start-up):

```
[wf] W01 PASS 59.0s
[wf] W02 PASS 17.7s
[wf] W03 PASS 1.1s
[wf] W04 PASS 222.5s
[wf] W05 PASS 3.9s
[wf] W06 PASS 23.1s
[wf] W07 PASS 125.2s
[wf] W08 PASS 620.2s
[wf] W09 PASS 14.1s
[wf] W10 PASS 4.1s
[wf] W11 PASS 598.3s
[wf] W12 PASS 30.8s
[wf] W13 PASS 15.5s
[wf] W14 PASS 2.8s
[wf] W15 PASS 6.6s
[wf] W16 PASS 4.4s
[wf] W17 PASS 138.2s
[wf] W18 PASS 253.5s
[wf] W19 PASS 7.5s
[wf] W20 PASS 18.3s
[wf] W21 PASS 2.0s
[wf] W22 PASS 2.3s
[wf] W23 PASS 2.4s
[wf] W24 PASS 6.1s
[wf] W25 PASS 27.4s
[wf] W26 PASS 3.0s
[wf] W27 PASS 0.4s
breaches: {}
```

## Executed mutation trial (2026-11-12, native amd64)

The "Planted defects" section above records defects by inspection. This section records one
applied to the shipped code and run as a full Harbor trial, because the money engine in
`tests/reference/money.py` is byte-identical to `solution/app/seatwise/money.py` and a grader
that computes expected values with the solution's own code cannot, in principle, catch a rule
that is wrong in both.

`SERVICE_FEE_PER_MILLE` was changed from `75` to `70` in **both** copies identically — a 7.0%
service fee instead of the 7.5% the instruction specifies.

| Run | Outcome | Workflows reached | reward |
|---|---|---|---|
| baseline | all workflows passed | 27 passed, 0 failed | 0.99 |
| fee 75 → 70 | **refused to grade**, exit 3 | 5 reached, 0 failed | 0.0 |

The mutant run stopped at W06 with:

```
verifier infrastructure failure: verifier fixture: worked check of the seat-order tie
```

The check that fired is at `tests/wf_b.py:127-129`:

```python
q0 = H.model.quote_for(eid, ["ALP-A-1", "ZED-A-1"])
if {l.key: (l.tax, l.total) for l in q0.lines} != {"ALP-A-1": (201, 2659), "ZED-A-1": (200, 2658)}:
    raise InfraError("verifier fixture: worked check of the seat-order tie")
```

Those cents are hard-coded in the workflow file, not derived from the money engine, so they
survive a change to the engine and catch it. The grader fails closed — it declines to emit a
score rather than passing a wrong rule.

So the shared-engine concern is real in structure but mitigated in practice, at least for the
fee rate: independent ground truth does exist, and it provably fires.

### Not verified

- Only the service-fee rate was mutated **as a full Harbor trial**. The other rules were
  covered separately by the self-test battery below, which runs outside a trial.
- The `wf_b.py` fixture pins one scenario (the seat-order tie). Its own breadth is still
  unmeasured; the self-test below is what gives the remaining rules coverage.

## Reference money self-test, wired into the verifier (2026-11-12)

`tests/reference/money.py` already ended with a `__main__` block reproducing the four worked
examples of `instruction.md` R9 (lines 228-240) and the R10.3 refund (line 279), from values
the author wrote down independently of the engine. Nothing invoked it: `tests/test.sh`
referenced it zero times, and no other file ran it as a module.

It is now run by `tests/test.sh` before the services start, and a failure marks the
evaluation invalid:

```sh
if ( cd "$TESTS" && python3 -m reference.money ) >"$LOGS/reference-selftest.txt" 2>&1; then
	echo "verifier: reference money self-test passed"
else
	invalid "the reference money self-test failed; see reference-selftest.txt"
fi
```

This matters because `tests/reference/money.py` is byte-identical to
`solution/app/seatwise/money.py` (7289 bytes), so grading the solution with those rules
cannot catch a rule that is wrong in both. The worked examples can.

### Defects the existing block catches

Each was injected into `tests/reference/money.py` in a scratch copy and run through
`python3 -m reference.money`, the same invocation `tests/test.sh` now uses.

| Injected defect | Result |
|---|---|
| `TAX_PER_100K` 8875 → 8750 (wrong order tax rate) | caught — `AssertionError` on example 1 |
| `SERVICE_FEE_PER_MILLE` 75 → 70 | caught — `AssertionError` on example 1 |
| `half_up` tie `(2n+d)` → `(2n+d-1)` (half-down) | caught — `AssertionError` |
| allocation tie-break `(-rem, i)` → `(-rem, -i)` (spare cent to the last seat, not the first) | caught — `AssertionError` on example 3 |
| allocation distributes no spare cents (floors only) | caught — `AssertionError` |
| fixed code not capped at the remaining subtotal | caught — `ValueError` from `half_up` on example 4, exit 1 |
| pristine control | passes, exit 0 — no false positive |

**Six of six.** The allocation tie-break case is caught specifically by example 3, where
`BALC-A-3` and `BALC-A-4` have equal remainders and the spare cent must go to the earlier
seat in seat order (R4.3) — totals 2890 and 2889 differ by exactly that cent.

The wiring was verified as well as the block: with `TAX_PER_100K` mutated, the `tests/test.sh`
invocation exits 1 and takes the `invalid` branch; unmodified it exits 0 and the run proceeds.
The vendor CLI was re-run afterwards and reports zero new findings.

### Not verified

- The six defects were run against the self-test, not as full Harbor trials. Only the
  service-fee mutation was run as a complete trial (above).
- `tests/reference/ties.json` still pins no expected amounts — its entries carry
  `['codes', 'fee', 'orders', 'prices']`. `gen_ties.py` uses `approx_quote` and
  `bankers_quote` to *select* cases where a Decimal(28) or float allocation, or
  round-half-even, would differ, so the discriminating inputs exist; their answers were
  never recorded.
- No mutation catalogue is shipped. The trial and battery above are recorded instead,
  because a catalogue asserting defect-to-workflow mappings that were never executed would
  be weaker evidence than runs that were.
