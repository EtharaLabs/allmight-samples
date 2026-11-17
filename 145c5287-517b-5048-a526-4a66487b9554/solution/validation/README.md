# Local validation of ledgerline (2026-11-12)

> **Judge credentials.** The judged criteria were graded with model access
> supplied from the author's machine, not with a buyer's own
> `api.anthropic.com` key. The criteria, the `StructuredOutput` schema and the
> files each criterion reads are identical either way, but the verdicts below
> have not been re-measured under a buyer's key.

How the task was validated on the author's machine, and the defects planted to
show the criteria catch what they claim to.

## Setup

- PostgreSQL 16 inside the environment image (`postgresql-16=16.2-1ubuntu4`),
  database and role `ledgerline`, initialised at build time and started by
  `environment/start-postgres.sh`. No external services.
- The corpus is static, not generated: `environment/inputs/policy.json`,
  `statement.csv` and `receipts/rcp-0001.txt` through `rcp-0012.txt`, copied in
  read-only (`--chmod=0444`) as `/app/inputs`. Their fourteen sha256 digests are
  pinned in both `tests/fixtures.json` (`input_digests`) and
  `environment/assets.yaml`, and were re-checked against the shipped bytes.
- The reference solution is `solution/solve.sh`, which runs `solution/solver.py`
  and then dumps the `reconciliation` table with `psql`. It installs nothing:
  `python3` and `psql` are apt-pinned in the image.
- The nine deliverables land under `/app/out`, and `solver.py` also generates
  the served application (`/app/app.py` and `/app/start.sh`) rather than
  shipping it, so the screens and the JSON come from one source.
- Grading runs in a separate verifier image (`tests/Dockerfile`,
  `[verifier].environment_mode = "separate"`) with Playwright and Chromium
  pinned by per-architecture sha256. `tests/checks.py` drives the served screens
  through a real browser.
- Scoring is `tests/test_output.py` (25 compiled criteria, weight 85) plus
  `tests/judge.toml` (8 judged criteria, weight 30), combined by
  `tests/reward.toml`.

The eight judged criteria read the written deliverables with a model, reached
through `api.anthropic.com`, the only host in `[verifier].allowed_hosts`. They
were graded here with model access supplied from the author's machine rather
than with a buyer's credentials; each returned a well-formed
`{"score", "reasoning"}` object against the `StructuredOutput` schema, and all
eight returned `yes`. Each judged criterion opens the deliverable it grades by
itself, with `Read` under `cwd = "/app"` - the prompt names the file but carries
none of its text, so a judge that cannot reach the filesystem scores nothing.
Every compiled criterion runs the same way locally as in the verifier.

## Oracle: 25 of 25 compiled and 8 of 8 judged criteria pass (fresh container)

Weights sum to 115: 85 across the 25 compiled criteria and 30 across the 8
judged. Both dimensions aggregate as a weighted mean, so a full score needs
every criterion to pass.

### Compiled criteria

| Criterion | Weight | Result |
|---|---|---|
| `reconciliation_json_wellformed` | 3 | PASS |
| `matched_set_exact` | 5 | PASS |
| `matched_sorted_by_receipt_id` | 1 | PASS |
| `unreceipted_set_exact` | 3 | PASS |
| `unposted_set_exact` | 3 | PASS |
| `violation_flags_exact` | 3 | PASS |
| `totals_consistent` | 3 | PASS |
| `summary_matches_json` | 3 | PASS |
| `snapshot_table_shape` | 3 | PASS |
| `snapshot_agrees_with_reconciliation_json` | 5 | PASS |
| `inputs_unmodified` | 5 | PASS |
| `review_screen_structure` | 3 | PASS |
| `review_screen_rows_sorted` | 1 | PASS |
| `review_screen_columns_aligned` | 1 | PASS |
| `served_screen_agrees_with_reconciliation_json` | 5 | PASS |
| `detail_screens_complete` | 3 | PASS |
| `served_screens_request_no_external_origin` | 5 | PASS |
| `contested_account_structure` | 3 | PASS |
| `contested_account_names_an_alternative` | 3 | PASS |
| `policy_notes_covers_every_violation` | 5 | PASS |
| `policy_notes_names_the_governing_limit` | 3 | PASS |
| `parse_notes_covers_every_hazard` | 5 | PASS |
| `parse_notes_quotes_the_correct_value` | 3 | PASS |
| `convention_notes_carries_both_sections` | 3 | PASS |
| `convention_notes_names_every_row` | 5 | PASS |

The top-level `[[reward]]` in `tests/reward.toml` names 85 as this dimension's
weight.

Cheat checks: `cheat_inputs_unmodified`, the single id in
`tests/cheat-checks.json`, PASS - the shipped corpus is byte-identical after the
run.

### Judged criteria

| Criterion | Weight | Result |
|---|---|---|
| `contested_pairing_matches_declared_matching` | 5 | PASS |
| `contested_reason_is_the_operative_rule` | 3 | PASS |
| `policy_verdict_follows_from_the_two_numbers` | 5 | PASS |
| `parse_notes_attributes_the_colon_mechanism` | 5 | PASS |
| `parse_notes_attributes_the_nonfield_mechanism` | 3 | PASS |
| `policy_magnitude_is_the_arithmetic_difference` | 1 | PASS |
| `convention_collapse_is_the_true_direction` | 5 | PASS |
| `convention_divergence_set_is_the_true_partition` | 3 | PASS |

Weights total 30, and the dimension aggregates as a weighted mean, so a full
score requires every one of the 8. Each criterion names the deliverable it reads
in `files` and is judged individually with `cwd = "/app"`.

The verdicts were also checked against the deliverables by hand, because a
judged criterion is only worth its weight if a wrong answer would fail it. The
policy arithmetic is exact in every section (RCP-0005 `8100 - 7500 = 600`,
RCP-0008 `2410`, RCP-0011 `2445`, and RCP-0001 `1800` short of its limit); the
two colon-bearing receipts are attributed to the colon rule with their vendor
strings quoted correctly; the contested sections name the rival receipt and the
rule that settles it; and the convention note states the collapse as twelve
eligible to one against a matching of eleven to one, partitioning all twelve
rows.

One property of the harness is worth recording: a judged criterion that cannot
be answered does not degrade to zero for its own dimension alone. Reward Kit
raises inside a task group (`RuntimeError: Reward 'judge' failed: ...`), which
ends the run before the compiled dimension scores at all, leaving
`tests/test.sh` to write its fail-closed `0`. A grader without judge credentials
therefore sees a flat zero rather than a partial score.

## No-op agent

With the environment up and no application, all 25 compiled and all 8 judged
criteria fail. The environment image ships the corpus to `/app/inputs` and
`start-postgres.sh` to `/usr/local/bin`, and nothing else: there is no
`/app/start.sh` to launch and no `/app/app.py` to serve, so `/app` holds only
the read-only corpus. Every structural criterion fails on the file that is not
there, the browser checks find nothing listening, and each judged criterion
reads an absent deliverable and answers no.

## Planted defects (each a copy of the reference solution with one realistic bug)

`mutants.py` holds the exact text replaced, the lever it exercises, and the one
criterion it must flip. Twenty-seven defects cover all 25 compiled criteria.

| Defect | Lever | Planted in | Target |
|---|---|---|---|
| `greedy-smallest-matching` | matching (cardinality) | `solver.py` | `matched_set_exact` |
| `tolerance-boundary-exclusive` | eligibility boundary | `solver.py` | `matched_set_exact` |
| `tie-break-nondeterministic` | matching (tie-break) | `solver.py` | `matched_set_exact` |
| `matched-not-sorted` | output ordering | `solver.py` | `matched_sorted_by_receipt_id` |
| `violation-at-limit` | policy comparison | `solver.py` | `violation_flags_exact` |
| `unreceipted-lists-every-row` | residual sets | `solver.py` | `unreceipted_set_exact` |
| `unposted-lists-every-receipt` | residual sets | `solver.py` | `unposted_set_exact` |
| `violation-count-inverted` | totals | `solver.py` | `totals_consistent` |
| `json-omits-totals` | deliverable shape | `solver.py` | `reconciliation_json_wellformed` |
| `summary-overstates-matched` | cross-file consistency | `solver.py` | `summary_matches_json` |
| `snapshot-flags-all-compliant` | table fidelity | `solver.py` | `snapshot_agrees_with_reconciliation_json` |
| `snapshot-dump-narrowed` | table shape | `solve.sh` | `snapshot_table_shape` |
| `corpus-byte-appended` | corpus immutability | `solver.py` | `inputs_unmodified` |
| `screen-hides-violations` | served/JSON divergence | `solver.py` | `served_screen_agrees_with_reconciliation_json` |
| `review-rows-reversed` | row ordering | `solver.py` | `review_screen_rows_sorted` |
| `table-layout-auto` | column alignment | `solver.py` | `review_screen_columns_aligned` |
| `stylesheet-from-another-origin` | self-containment | `solver.py` | `served_screens_request_no_external_origin` |
| `detail-screen-drops-category` | detail completeness | `solver.py` | `detail_screens_complete` |
| `contested-never-names-the-rival` | contested prose | `solver.py` | `contested_account_names_an_alternative` |
| `policy-note-skips-violations` | policy coverage | `solver.py` | `policy_notes_covers_every_violation` |
| `policy-note-omits-the-numbers` | policy prose | `solver.py` | `policy_notes_names_the_governing_limit` |
| `parse-note-skips-colon-receipts` | parse-hazard coverage | `solver.py` | `parse_notes_covers_every_hazard` |
| `parse-note-quotes-the-wrong-vendor` | quoted value | `solver.py` | `parse_notes_quotes_the_correct_value` |
| `convention-note-drops-divergence` | required sections | `solver.py` | `convention_notes_carries_both_sections` |
| `convention-note-names-no-rows` | row naming | `solver.py` | `convention_notes_names_every_row` |
| `review-screen-id-renamed` | screen structure | `solver.py` | `review_screen_structure` |
| `contested-writes-one-section` | contested structure | `solver.py` | `contested_account_structure` |

The catalogue is verified structurally, not run as trials. For every entry the
replaced text occurs exactly once in its file, the named criterion exists in
`tests/checks.py`, and the patched source still parses - so each defect is a
real alternative implementation rather than a syntax error that would fail
everything indiscriminately.

That verification is a command, not a claim. `python3 mutants.py check` applies
each of the three conditions to every entry against the current bundle and
exits non-zero on the first that does not hold; `python3 mutants.py list`
prints the catalogue; and `python3 mutants.py apply <name> --into <dir>` writes
a mutated copy of `solution/` so one defect can be run as a trial. The check
reports `27 of 27 entries applicable` against this revision, which also makes
it a regression guard: any later edit that moves one of the 27 replaced strings
in `solver.py` or `solve.sh` turns the catalogue stale and the check fails.

Two notes on why the defects sit where they do:

- A defect inside `answer_from_inputs` moves the JSON and the served screens
  together, because `_app_module` copies that function into the generated
  `/app/app.py`. Only a defect confined to `render_site`, `STYLE` or the writers
  in `main` makes the two disagree, which is what
  `served_screen_agrees_with_reconciliation_json` measures.
- `snapshot_table_shape` counts fields per row against the fixture's six
  columns, so its defect narrows the `copy` projection in `solve.sh` instead of
  dropping a column from the DDL. Dropping the column would make `psql` reject
  the copy stream and fail every criterion, measuring nothing.

## Provenance

Nothing in this task is taken from a public dataset, repository or benchmark.
This section is the task's provenance record.

- **The corpus is original.** `environment/inputs/policy.json`,
  `statement.csv` and `receipts/rcp-0001.txt` through `rcp-0012.txt` were
  written for this task. The vendors, categories, amounts and dates are
  invented, and the parse hazards - the colon inside a `VENDOR` value, and the
  lines that are not field lines - were placed deliberately so that the
  reading rule stated in `instruction.md` is load-bearing rather than
  decorative. The fourteen sha256 digests are pinned in `tests/fixtures.json`
  (`input_digests`) and `environment/assets.yaml`, and the corpus is mounted
  read-only (`--chmod=0444`).
- **The expected answers are derived, not transcribed.** They come from this
  corpus by way of `solution/solver.py`, and are pinned as data in
  `tests/fixtures.json` under `expected`. Several compiled criteria also read
  `inputs/policy.json` and `inputs/statement.csv` directly and recompute their
  comparison from the shipped bytes rather than from anything the solution
  wrote (`tests/checks.py:406`, `:469`, `:578`, `:773`).
- **The benchmark citation is calibration only.**
  `[metadata].reference_benchmark` names Vibe Code Bench (arXiv 2603.04601).
  It records the delivery format and difficulty target this task was aimed at.
  No instruction text, corpus file, policy rule, expected answer, grading
  criterion or reference implementation derives from it. The same statement is
  recorded in `task.toml` as `reference_benchmark_relation`.
- **No third-party code is redistributed.** The bundle vendors nothing: there
  are no wheels, archives or `site-packages` trees in it. Everything the two
  images need is pinned and fetched at build time - the runtime packages from
  the Ubuntu snapshot archive by exact version, and the verifier's Playwright
  and Chromium by sha256. Because no upstream source or binary ships inside
  the task, no upstream licence text is carried here; the task's own code is
  marked `MIT` in both Dockerfiles.
- **No personal data.** Every name in the corpus is fictional, and the only
  real identifier anywhere in the task is the author address in
  `[task].author`.

## Build notes

Two downloads in the shipped Dockerfiles are not retried, and each cost a full
build when it failed upstream:

- `environment/Dockerfile` installs from the dated snapshot
  `snapshot.ubuntu.com/ubuntu/20261001T000000Z`, which returned `503` and `502`
  on 17 packages and ended one build with `exit code: 100`.
- `tests/Dockerfile` fetches the Chromium build with `curl -fsSL` and no
  `--retry`; one truncated transfer ended a build with `exit code: 18`
  (`CURLE_PARTIAL_FILE`).

`--retry 5 --retry-delay 5 --retry-all-errors` on the Chromium fetch and
`-o Acquire::Retries=5` on the apt layer would let both survive a flaky mirror.
Neither affects correctness.

## Not verified locally

- The judged verdicts under a buyer's own `api.anthropic.com` credentials. They
  were graded here through model access supplied from the author's machine; the
  criteria, the schema and the files read are identical either way.
- The 27 planted defects as trials: each costs a full verifier run.
- `linux/amd64`. Both images were built and run on arm64 here; the task declares
  both platforms and neither Dockerfile pins one.
