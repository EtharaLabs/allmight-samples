# Lotbook validation record

> **Database version.** The local runs recorded here used PostgreSQL 17, but the
> delivered images pin `postgresql-15=15.19-0+deb12u1` in both
> `environment/Dockerfile` and `tests/Dockerfile`. Only the oracle and nop runs on
> EC2 used the delivered images; the local results below have not been re-measured
> on 15.

How the task was validated locally (macOS; 1.0.0 on 2026-11-10, 2.0.0 on 2026-11-11) before any EC2 run: a fresh PostgreSQL database seeded
from `environment/services/db/init/*.sql` (identical to `tests/services/db/init/`), the kit mail sink, the oracle
in `solution/app` (or a mutant copy), and `tests/workflows.py` with `FK_BASELINE` from `tests/snapshot.py`.
W26 (health follows the database) was exercised locally through a TCP proxy between the app and PostgreSQL
that the stop/start hooks kill and restart; on EC2 the kit's `pg_ctl` hooks do the same.

## Oracle

2.0.0 after the second review round, fresh database, all 31 workflows, one run (`[wf]` lines from workflows.log): every workflow PASS, 0 breaches, no tracebacks in the app log.

```
[wf] W01 PASS 132.3s
[wf] W02 PASS 0.6s
[wf] W03 PASS 3.8s
[wf] W04 PASS 7.4s
[wf] W05 PASS 11.7s
[wf] W06 PASS 0.7s
[wf] W07 PASS 1.6s
[wf] W08 PASS 1.2s
[wf] W09 PASS 4.4s
[wf] W10 PASS 2.3s
[wf] W11 PASS 1.4s
[wf] W12 PASS 1.0s
[wf] W13 PASS 4.8s
[wf] W14 PASS 4.5s
[wf] W15 PASS 7.5s
[wf] W16 PASS 4.4s
[wf] W17 PASS 5.8s
[wf] W18 PASS 5.6s
[wf] W19 PASS 6.7s
[wf] W20 PASS 7.2s
[wf] W21 PASS 8.3s
[wf] W22 PASS 11.6s
[wf] W23 PASS 1.2s
[wf] W24 PASS 8.6s
[wf] W25 PASS 15.3s
[wf] W26 PASS 1.1s
[wf] W27 PASS 3.7s
[wf] W28 PASS 12.4s
[wf] W29 PASS 11.8s
[wf] W31 PASS 4.3s
[wf] W30 PASS 9.6s
```

Coverage observed in that run: W09 compared 13,491 realized rows (household 1, 2024), W10 15,067 (2025), W11 5,529
(household 2: 2,310 in 2024, 3,219 in 2025); W12 compared 5,607 open legacy lots on the lots pages (accounts 1-3:
2,705; 4, 5 and the IRA 8: 2,012; 6, 7 and the IRA 9: 890) plus both unrealized exports; the rule workflows compared
W13: 121 realized rows / 94 lots, W14: 190 / 171, W15: 100 / 92, W16: 145 / 57, W17: 79 / 104, W18: 65 / 142
(1 wash portion), W19: 102 / 130 (53 wash portions), W20: 107 / 126 (65 wash portions), W28: 34 spin-offs recorded
by form, 82 realized rows / 190 lots, W29: 30 mergers, 100 / 100, W30: 54 returns of capital, 66 / 93, W31: 26 / 64
with 25 portions disallowed for good.

## Planted defects

Each mutant is a copy of `solution/app` with exactly one change. Each was run against a fresh database on its
intended workflow and a few related ones.

| Mutant | Lever | Change | Intended workflow | Result |
|---|---|---|---|---|
| m1-wash-window | wash sale (R7.2) | `WINDOW_DAYS = 29`: replacements must be within 29 days, not 30 | W19 | W19, W20, W09 FAIL (realized rows and lots differ, e.g. a 30-day replacement not matched; household 1's 2024 summary). No breach. |
| m2-decimal-share | FX/rounding (R5.2, R5.4) | share rule computed per share first with 28-digit `Decimal` | W13 | W13, W14, W10 FAIL (half-cent ties from `ties.json` and the legacy tie lots round down). No breach. |
| m3-legacy-refs | import idempotency (R4.5) | the natural key ignores `legacy_transactions.ref` | W07 | W07 FAIL (a statement repeating the old tool's last rows records 45 rows instead of 45 duplicates); W08 PASS. No breach. |
| m4-viewer-writes | security / roles (R1.2) | the change guard accepts the `viewer` role | W05 | W05 FAIL with breach X1 (a viewer created an account, a security, a method change, a transaction and, in 2.0.0, a corporate action); W06 PASS. |
| m5-adjustment-netting | retroactivity (R8.4) | adjustments not netted against adjustments already frozen into later locked years | W21 | W21 FAIL (2026 summary repeats corrections already carried by 2025); W22 PASS. No breach. |
| m6-reset-token-at-rest | secrets at rest (R11.6) | the reset email, with its plain token, is queued in the stored outbox table | W27 | W27 FAIL with breach X8 (`lb_outbox.body` holds a reset token); W01 PASS. |

All six were rebuilt from the 2.0.0 oracle and re-run on the 2.0.0 seed with the same results (m2 now fails W10 on
household 1's 2025 ORCH summary row, one cent). Run with `tools/run_mutants.sh` from the author's scratch directory (each run: fresh database, then
`workflows.py --only <ids>` against the mutant). The unrelated workflow in each run passing shows the defects
stay isolated to the rule cluster they break.

## Approximate-arithmetic coverage

`tests/reference/gen_ties.py` emulates two common approximations of R5 by patching the reference engine's two
rounding points: binary floating point, and 28-digit `Decimal` with per-share values first and
`units_per_usd` rates inverted once. It keeps only scenarios where an approximation and the exact rules
disagree on a realized row, an open lot or a dividend:

| Category | Scenarios | float wrong | Decimal wrong | Planted in |
|---|---|---|---|---|
| fx | 40 | 19 | 27 | W13 (0-30), W18 (30-40) |
| share | 40 | 16 | 27 | W14 (0-20), W15 (20-40) |
| split | 24 | 8 | 16 | W16 |
| transfer | 20 | 7 | 14 | W17 |
| wash | 40 | 13 | 28 | W19 (0-20), W20 (20-40) |
| dividend | 17 | 10 | 9 | W18 |

The legacy data was generated with tie lots in HIFO periods (`solution/seed/generate.py`). Under the same two
approximations the 2023-2025 reported realized rows of the two legacy households (2.0.0 seed, IRA rows excluded)
differ in 213 (float, 2023-2025) and 9 (Decimal, 2025) of 33,579 rows for household 1 and 20 (float, 2023-2025) and 5
(Decimal, 2023) of 7,015 rows for household 2. (1.0.0 seed: 73 / 75 of 38,196 and 49 / 44 of 8,986.) The Decimal
cases come from the HIFO tie lots; the generator's other tie planter (`plant_share_ties`, half-cent proceeds shares)
finds no reachable case in this data and plants none, so the dense Decimal coverage is `ties.json` (W13-W20).

## QC and hardening pass (2026-11-10)

Grader tolerance and robustness (no instruction.md change):

- Wash-sale marker (W12 and every lots comparison): a replacement lot whose disallowed part added 0 cents to its
  basis may be marked either way (`tests/harness.py` `wash_marker` / `tolerate_either`; the engine keeps
  `Lot.wash_cents`). The legacy data has exactly one such open lot (TIDL, 0.0167 shares in account 2; household 2
  has none). An oracle copy that marks these lots false (the other defensible reading) passes W12, W19 and W20.
- Reset-link TTL in the verifier is 120 s (`tests/verifier-overrides.env`); only W01's expiry step waits it out.
- Workflow alarms: W13-W20 make 206-283 requests each (counted on the oracle); their alarm is 2400 s, at least
  3x the time at 2.5 s per request. The kit's FK_BUDGET_SECONDS bounds the total.
- Session-cookie checks (W02 attributes, W27 at-rest needles) apply to the cookie(s) whose removal signs a
  client out; `signed_in()` follows redirects and judges the final page.
- Redirects: any 3xx is accepted where the spec names no code; a same-account transfer may be refused with any
  4xx or 3xx as long as nothing changed; specific-lot quantities that do not add up are no longer probed (the
  spec does not say what happens).
- Concurrent uploads (W07): the losing upload may be refused; each row must be listed exactly once.
- Header injection (W21): judged on the SMTP envelope recipients, not the parsed To header.
- Error lines are read inside the rejected element and compared as a set.
- Calendar: W21's "current year cannot be locked" probe uses the current UTC year (it was fixed to 2026). Every
  other date in the tests and the seed is in the past, so no `valid_until` horizon remains.
- Judge evidence is captured before the strict checks of its step, and best effort in `finally` (W15, W21, W24),
  so one defect does not also cost the judged criteria.

Hardening checks added inside existing critical workflows (each passes on the oracle and fails a one-change
oracle copy; `tools/make_mutants2.py` in the author's scratch):

| Check (rule) | Workflow | Mutant | Result |
|---|---|---|---|
| A year that had no data before the household's first lock is frozen empty with every earlier year (R8.3): a late 2022 buy and sale leave 2022's summary.csv and realized.csv unchanged and land as 2026 adjustments | W21 | d1-live-unfrozen-year | FAIL: "2022 realized.csv changed after it was locked" |
| Inside one file a repeated row is a duplicate, and a reference repeated with other content rejects the file (R4.5) | W07 | d2-no-in-file-dedupe | FAIL: "recorded=3 duplicates=0" |
| A hand-entered buy or dividend dated before the first CAD rate is refused with 422 (R3.4, R5.1) | W13 | d3-no-rate-check | FAIL: "expected 422, got 500" |

The first check also corrected the oracle and the reference model, which had shown live realized rows for such a
year.

## 2.0.0: corporate actions and retirement accounts (R13)

New rules: instruction.md R13.1-R13.11 with worked examples 9-12 (spin-offs with cash in lieu, cash-and-stock and
cash-only mergers, returns of capital, IRA accounts, recording actions by form); R3.1 (account type), R6.1 (a date's
corporate actions after its splits, before its buys), R9.1 (rows of actions in realized.csv) and R10.1 (the
`corporate_actions` table) were extended. `tests/reference/examples.py` reproduces examples 9-12 with the reference
engine.

Seed (`solution/seed/generate.py`, both init copies identical): 23 legacy corporate action rows (5 spin-offs of 3
parents, the AURS one on the date of its 10-for-1 split; 3 cash-and-stock mergers, two of them the EUR DLTW merger in
each household; 1 cash-only merger; 14 returns of capital, including two HLIO distributions that exceed the basis of
many lots) and two IRA accounts (8 in household 1, 9 in household 2). With
them the legacy households have 35,750 and 8,509 lot portions (cash in lieu 521 / 170, merger rows 1,063 / 101,
return-of-capital rows 305 / 167), 2,171 / 1,494 IRA portions that must not be reported, and 117 open lots with
basis 0. No legacy corporate action involves a security within 30 days of a loss sale of it (R13.8), checked by the
author's analysis script. W09-W12 compare all of this.

Fixtures (`tests/reference/gen_ca.py` -> `tests/reference/ca_ties.json`): each scenario is kept only when the exact
rules and at least one emulated wrong reading disagree on a realized row, an open lot or a year figure.

| Category | Scenarios | Wrong readings that change a cent (scenarios) | Why the cases are hard | Planted in |
|---|---|---|---|---|
| spinoff | 34 (19 USD, 7 CAD, 8 EUR; 16 with a split on the same date) | after_split 16, per_share 12, cil_once 9, decimal_alloc 6, whole_first 2 | USD lot bases chosen so that basis x a is an exact half cent (a has an even denominator, the basis an odd multiple of half of it): rounding the parent's share instead, computing per share first or in 28-digit Decimal lands on the other cent; a fractional share takes f/n of the allocated basis by the share rule, not the remainder; CAD/EUR cash in lieu must be rounded to the cent before conversion; on a split date the spin-off counts post-split shares | W28 |
| merger | 30 (20 USD, 7 EUR, 3 CAD; 1 cash-only, 5 stock-only) | loss_recognized 26, full_gain 16, after_trades 9, value_once 2 | lots on both sides of G = 0 and with G above the cash; a buy of the target on the merger date stays a lot of the target; cash and stock value converted per lot after rounding in the currency | W29 |
| roc | 26 (54 distributions) | after_trades 17, negative_basis 16, account_total 8 | second and third distributions exceed the remaining basis (basis 0 plus a gain row, by each lot's own holding period); several lots in one account where per-lot conversion differs from converting the account total and splitting it; a buy on the distribution date is not reduced | W30 |
| ira | 26 | ira_taxable 26, ira_dividends 13, ira_sale_washed 10 | taxable loss sales replaced 0-30 days before or after in the IRA (and sometimes in a taxable account too): the IRA part is disallowed for good with nothing added to the IRA lot; IRA loss sales bought back within 30 days in a taxable account are never matched; IRA dividends are not reported | W31 |

New workflows (weights and the existing W01-W27 weights unchanged; reward formula unchanged):

| Id | Title | Weight | Critical |
|---|---|---|---|
| W28 | Spin-offs: basis allocation, cash in lieu, carried holding periods, same-day splits, refusals | 5 | yes |
| W29 | Cash-and-stock mergers: recognized gain capped at the cash, unrecognized losses, carried lots, cash-only sales | 5 | yes |
| W31 | Retirement accounts: an IRA account by form, no reporting, losses disallowed for good, transfers refused | 4 | yes |
| W30 | Returns of capital: basis reduced per lot, excess as a gain, never below zero, same-day buys untouched | 4 | no |

Each records its actions through the corporate-actions form (R13.11) after importing the rows dated up to each
action, then compares realized.csv, summary.csv, lots pages and unrealized.csv with the reference model, and checks
the R13.11 refusals (another household's security, the same security twice, another currency, too many decimals, a
non-positive amount, a merger that would leave a later sale short, a cash-only merger naming an acquirer) and that a
viewer or advisor cannot record one. W04, W05 and W06 also attempt a corporate action as the advisor, the viewer, the
preparer and without the session token. Totals: 31 workflows, weight 112, 18 critical.

Time budget: on the oracle W28-W31 make 424, 357, 316 and 120 requests (counted with the author's
`tools/count_requests.py`); their alarm is 3600 s, at least 3x the time at 2.5 s per request.

Planted defects for R13 (each a one-change copy of `solution/app`, fresh database per run; `tools/make_mutants3.py`,
`make_mutants3b.py`, `make_mutants3c.py` in the author's scratch):

| Mutant | Rule | Change | Target | Result |
|---|---|---|---|---|
| v1-parent-rounded | R13.2 | the parent's share is rounded and the new lot gets the rest (basis - rhu(basis x (1 - a))) | W28 | W28 FAIL (5 of 56 2025 rows); W09 PASS |
| v2-per-share-alloc | R13.2 | the allocation rounded per share, then times the quantity | W28 | W28 FAIL (2 of 56 2025 rows) |
| v3-convert-once | R13.1 | converted amounts not rounded to the cent in their currency first | W28, W29 | W28 FAIL (6 of 25 2024 rows), W29 FAIL |
| v4-merger-loss | R13.5 | a loss recognized when stock is received | W29 | W29 FAIL (15 of 19 2024 rows); W10 FAIL (2025 DLTW and ORCH summary) |
| v5-merger-uncapped | R13.5 | the whole realized gain recognized, not capped at the cash | W29 | W29 FAIL |
| v6-roc-negative | R13.7 | the basis may go below 0 (no excess row) | W30 | W30 and W09 FAIL (the app answers 500: a negative basis cannot be rounded) |
| v6b-roc-excess-dropped | R13.7 | the basis stops at 0 but the excess is not reported | W30 | W30 FAIL (13 of 15 2024 rows missing); W09 FAIL (2024 HLIO summary) |
| v15-roc-account-total | R13.7 | the distribution converted once per account and spread over its lots | W30 | W30 FAIL; W09 FAIL (2024 BRKW and HLIO summary) |
| v8-actions-after-trades | R13.8, R6.1 | corporate actions after the day's trades | W29, W30 | both FAIL (a same-day buy merged away or reduced) |
| v9-spinoff-before-split | R13.8 | a spin-off before the same day's split | W28 | W28 FAIL (an import the rules accept is refused as short); W09 FAIL (2024 AURQ summary) |
| v13-spinoff-new-holding | R13.4 | spun-off lots held from the action date | W28 | W28 FAIL; W09 PASS |
| v14-cil-short | R13.3 | cash in lieu held from the action date (always short-term) | W28 | W28 FAIL (21 of 25 2024 rows); W09 FAIL |
| v7-ira-taxable | R13.9 | the IRA treated as a taxable account | W31 | W31 FAIL; W10 FAIL |
| v10-ira-basis-added | R13.10 | the disallowed loss added to IRA replacement shares | W31 | W31 FAIL (22 of 38 IRA lots); W10 PASS |
| v11-ira-sale-washed | R13.9 | a loss sale in the IRA matched by the wash-sale rule | W31 | W31 FAIL; W10 FAIL |
| v12-ira-dividends | R13.9 | IRA dividends reported | W31 | W31 FAIL (2024 summary dividends); W10 FAIL |
| v16-ca-no-csrf | R11, R13.11 | the corporate-actions form skips the session token check | W06 | W06 FAIL with breach X2 |
| v17-ca-any-role | R1.2, R13.11 | any role that can see the household may record an action | W04, W05 | W04 FAIL with breach X1 (advisor), W05 FAIL with breach X1 (viewer) |

v11 and v12 first passed W31 and failed only the legacy W10; the IRA fixtures then gained IRA loss sales bought back
within 30 days in a taxable account and IRA dividends (the `ira_sale_washed` and `ira_dividends` readings), after
which both fail W31 (results above are from that version; the other categories' fixtures did not change). The
role and CSRF checks for corporate actions compare the household's listed actions before and after the attempts, so
an earlier workflow's breach is not attributed to them (m4 passes W06 again).

The earlier hardening mutants (d1-d3) and the tolerance copy h1 were rebuilt from the 2.0.0 oracle and re-run: d1
fails W21, d2 fails W07, d3 fails W13; h1 passes W12, W19 and W20.

## 2.0.0 review fixes (2026-11-11)

After the first EC2 round (oracle 1.0, nop 0.0) an independent review re-implemented R13 and matched the reference
engine on all 116 fixture scenarios. Changes:

- Grader: every account the harness creates sends `account_type` (empty for taxable, R3.1), also in W06's
  mass-assignment create; R13.11 refusals take the accepted statuses as a parameter, and a new security of another
  household may be refused with 403 (R1.3) or 422; the account-page transaction list ignores corporate-action kinds as
  it ignores transfers; W31 runs before W30 (critical first); IRA shares that served as replacement shares may carry
  either wash marker; W31 reads the first non-empty `data-account-type` of each account.
- The other-household probe in W28 named household 2's lowest security id, which is in EUR, so a 422 for "another
  currency" also passed it. It now names a USD security of household 2. Mutant v18 (the form accepts a security of
  another household) passed W28 before this change and fails it after.
- Spec: R6 intro (lots cross securities only through corporate actions), R9.2 (IRA rows and dividends do not count),
  R13.2 (each part divided off under R7.3 is its own lot), R13.5 and example 10 (the row's quantity), R13.11
  (neither an acquirer nor an acquirer value for a cash-only merger). No rule changed meaning; the oracle and the
  reference engine are unchanged.
- The W28 step title no longer claims IRA lots (no corporate-action fixture puts a lot in an IRA; the legacy IRA
  accounts hold securities with spin-offs, mergers and returns of capital and are compared by W09-W12).
- Seed: the spun-off symbol MPLX was renamed MPLW (it sorts in the same place, so the regenerated `002_records.sql`
  differs from the previous one only in those two securities rows). HLIO and QNTM are kept.

Mutants re-run on a fresh database each (the oracle app is unchanged, so the copies are the same): v1, v2, v9, v13,
v14 fail W28; v3 fails W28 and W29; v4, v5 fail W29; v8 fails W29 and W30; v6, v6b, v15 fail W30; v7, v10, v11, v12
fail W31; v16 fails W06 (X2); v17 fails W04 and W05 (X1); v18-ca-other-household fails W28 ("a new security of
another household: expected 403 or 422, got 303"); v19-method-idor (the method-change form checks the user's own
household instead of the account's) fails W03 with breach X1. No mutant had targeted W03 before.

## Second review round (2026-11-11)

Wording only in the spec (R10.1, R13.1: a recorded corporate action may live in the app's own tables; R13.8: lots
created by an action are never replacement shares in their own right) and in task.toml's breach list. Grader: every
create-account post sends `account_type` (empty for taxable). Judge evidence no longer depends on the first import
step: W21's `finally` captures `lock_before` and `report_year` with Elena's session when its browser never opened,
W24's `finally` opens a browser if needed and captures `advisor_view` from `/accounts/1`, and `lots_wash` (J03)
falls back to the legacy HIFO account 2 (366 wash-adjusted lots) when W19's book is missing. Checked with an oracle
copy whose imports all fail (W19, W21, W24 fail at their first step): `lock_before`, `report_year`, `lots_wash` and
`advisor_view` were all captured.
