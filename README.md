<p align="center"><img src="images/hero.svg" alt="allmight" width="720"></p>

# allmight: samples

<p align="center"><b>Five sample tasks from allmight, a Harbor benchmark that measures whether an autonomous agent can build a whole working web application from a written specification.</b></p>

<p align="center"><a href="#summary"><img src="https://img.shields.io/badge/tasks-5-845EF7" alt="tasks: 5"></a> <a href="#summary"><img src="https://img.shields.io/badge/harbor_schema-1.4-845EF7" alt="harbor schema: 1.4"></a> <a href="#trials"><img src="https://img.shields.io/badge/mean_reward-0.4680-845EF7" alt="mean reward: 0.4680"></a></p>

<p align="center"><sub><a href="#abstract">Abstract</a> | <a href="#summary">Summary</a> | <a href="#tasks">Tasks</a> | <a href="#trials">Trials</a> | <a href="#coverage">Coverage</a> | <a href="#dataset-structure">Dataset structure</a> | <a href="#reproduction">Reproduction</a> | <a href="#cite">Cite</a> | <a href="#contact">Contact</a></sub></p>

## Abstract

allmight is a benchmark of agentic tasks in [Harbor](https://github.com/laude-institute/harbor) format, built to measure whether an autonomous agent can deliver a working application rather than a patch. Each task hands the agent an empty workspace, a running environment, and a written specification of a business a real operator would recognize, and asks for the whole thing: the data model, the rules, the money, the roles, the screens, and the email. Nothing is scaffolded, and no reference implementation is reachable from inside the image.

Every task runs in a container whose services are already up — a PostgreSQL database seeded with an old system's records, an SMTP sink that accepts every message, and, where the task needs one, a Stripe-compatible card provider with a hosted payment page, signed webhooks and administrative fault injection. Grading is adversarial and continuous from 0 to 1: strict workflows drive the application through HTTP, the provider's APIs, the mail sink and a real Chromium, a reference engine re-derives every expected cent, slot, lot and hour independently, a breach battery probes tenancy, CSRF, XSS, mass assignment, webhook forgery, session handling and secret storage, and an LLM judge grades the experience from captured screenshots, page text and email.

This repository holds the five-task sample. Four are zero-to-one builds over an existing database — ticketing, clinic booking, warehouse fulfilment, payroll — each a day-long build with fourteen or so interacting feature areas; the fifth is a tighter, browser-graded reconciliation task whose answer is an exact maximum-cardinality matching under a stated tie-break. All five are rated `hard`. The hard part is never the framework: it is DST folds and gaps, half-cent allocation ties, webhooks that arrive late, twice, or out of order, and legacy rows stored as naive local time.

## Summary

| Property | Value |
| --- | --- |
| Tasks | 5 |
| Domain | Web application engineering (specification-driven, zero-to-one) |
| Business domains | Enterprise tooling (3), solo founder (2) |
| Stack | Python 3.12 standard library, PostgreSQL 16, no runtime dependencies added |
| Format | Harbor task, schema 1.4 |
| Platforms | linux/amd64, linux/arm64 (every image builds for both) |
| Difficulty | hard (5 of 5) |
| Specification size | 27,312 words across the five `instruction.md` files |
| Agent budget | 5 hours per task (2 hours for `145c5287`) |
| Verifier budget | 3 hours per task (30 minutes for `145c5287`) |
| Agent | Codex CLI 0.169.0 with `gpt-6.1-sol`, reasoning effort high |
| Judge | Claude Fable 5.1 (`claude-fable-5-1`) |
| Runs per task | 1 |
| Mean reward | 0.4680 |
| Mean agent cost | 0.53 USD |

## Tasks

| Task | Application | Business | Objective | Grading |
| --- | --- | --- | --- | --- |
| [`145c5287`](145c5287-517b-5048-a526-4a66487b9554/) | Ledgerline | Expense review | Reconcile a bank statement against its receipt corpus and policy under an exact maximum-cardinality matching with a stated tie-break, publish the result as JSON, as a PostgreSQL table dump and on a served review surface that cannot drift from either, and account in writing for the parse hazards and the contested posting convention. | 25 compiled criteria (85 weight) + 8 judged criteria (30 weight) |
| [`7cce0d38`](7cce0d38-d0ac-45f8-b277-db41d60db768/) | Seatwise | Reserved-seat ticketing | Build a multi-tenant ticketing platform for three promoters: seat maps, sales windows and presales in venue time zones, holds with TTLs, checkout confirmed only by signed webhooks, exact fee, tax and promo allocation, tiered refunds, cancellations, transfers with signed QR tokens, online and offline check-in, waiting lists, dashboards and exports. | 27 strict workflows (15 critical) + 10 judged criteria |
| [`8afcb304`](8afcb304-47f7-47d5-9a47-ac3ccadd0ca9/) | Harborview Physio | Clinic booking | Build a two-clinic physiotherapy booking application: an exact availability engine over effective-dated schedules, exceptions, closures, buffers and shared rooms across two time zones, card deposits through signed webhooks, elapsed-time refund bands, reschedules that carry the deposit, a chained waitlist, clinic-scoped staff roles, and a spreadsheet-safe export. | 27 strict workflows (17 critical) + 10 judged criteria |
| [`c5e57df8`](c5e57df8-355f-4931-8167-b8c9ace8a425/) | Pickpath | Warehouse fulfilment | Build a specialty-food distributor's order and warehouse application: tiered effective-dated prices, per-line tax and dimensional-weight freight, approval limits, FEFO allocation with shelf-life contracts, exact Held-Karp pick paths, scanned picks with short-pick re-allocation, version-checked cycle counts, invoices, returns with credit notes, and FIFO valuation over DST-ambiguous legacy times. | 26 strict workflows (17 critical) + 10 judged criteria |
| [`dd267972`](dd267972-4223-4439-afe5-095fe11a34f2/) | Shiftbook | Timekeeping and payroll | Build a restaurant group's timekeeping and payroll application: exact CA and FLSA overtime across three time zones and DST, a time-clock punch API, approvals along a reporting tree, finalize-once payroll runs with retroactive adjustments, emailed pay statements, and a spreadsheet-safe export. | 23 strict workflows (12 critical) + 10 judged criteria |

Each task is graded against a reference engine, not against its own output: the verifier re-derives the expected answer from the shipped corpus and from a model of every action it took, so a plausible-looking application that computes the wrong number still fails. In the four workflow tasks, `reward = 0.9 * functional + 0.1 * judge`, a workflow passes only if every one of its substeps passes, and any observed security breach or failed critical workflow caps every workflow at 0.4. In `145c5287`, the reward is the weighted mean of its two dimensions, so the compiled criteria carry 85 of 115 and the judged prose criteria 30.

## Trials

Each task was run once with Codex CLI 0.169.0 on `gpt-6.1-sol` at high reasoning effort and scored by the task's own verifier. Trials are stored in each task's `trials/gpt-6.1-sol/`.

| Task | Reward | Functional<sup>1</sup> | Judge | Cost (USD) | Agent time | Verifier time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| [`145c5287-517b-5048-a526-4a66487b9554`](145c5287-517b-5048-a526-4a66487b9554/trials/gpt-6.1-sol/) | 0.8435 | 0.7882 | 1.00 | 0.15 | 4m 2s | 5m 8s |
| [`7cce0d38-d0ac-45f8-b277-db41d60db768`](7cce0d38-d0ac-45f8-b277-db41d60db768/trials/gpt-6.1-sol/) | 0.2383 | 0.1981 | 0.60 | 0.77 | 24m 7s | 1h 52m |
| [`8afcb304-47f7-47d5-9a47-ac3ccadd0ca9`](8afcb304-47f7-47d5-9a47-ac3ccadd0ca9/trials/gpt-6.1-sol/) | 0.4170 | 0.3633 | 0.90 | 0.62 | 16m 3s | 20m 7s |
| [`c5e57df8-355f-4931-8167-b8c9ace8a425`](c5e57df8-355f-4931-8167-b8c9ace8a425/trials/gpt-6.1-sol/) | 0.4145 | 0.3495 | 1.00 | 0.61 | 17m 29s | 50m 17s |
| [`dd267972-4223-4439-afe5-095fe11a34f2`](dd267972-4223-4439-afe5-095fe11a34f2/trials/gpt-6.1-sol/) | 0.4269 | 0.3632 | 1.00 | 0.50 | 14m 32s | 11m 58s |
| **mean** | **0.4680** | **0.4125** | **0.90** | **0.53** | **15m 15s** | **40m 2s** |

<sup>1</sup> `functional` for the four workflow tasks; for `145c5287` the dimension is `test_output_score`, and its judged dimension is `judge_score`.

The judge scores stay high while the functional scores do not, and that gap is the point of the benchmark: the agent reliably produces an application that reads as finished and navigates cleanly, then loses the reward on exactness — a cent misallocated, a webhook applied twice, a slot offered inside a DST gap, a legacy row read in the wrong fold. Every agent run finished well inside its budget, so the scores are not a timeout artefact.

Each trial folder is Harbor's trial output:

| Path | Contents |
| --- | --- |
| `config.json`, `lock.json` | Trial configuration and resolved run settings |
| `result.json` | Agent, model, token usage, cost, timings, and verifier rewards |
| `trial.log` | Harbor's run log for the trial |
| `agent/` | Agent trajectory (`trajectory.json`), Codex session transcript (`sessions/`), and agent output (`codex.txt`) |
| `verifier/` | `reward.json`, `reward-details.json` (per-criterion scores and weights), workflow results, service logs, and the judge's captured evidence |
| `artifacts/` | Files the agent produced, collected from the paths declared in `task.toml` |

## Coverage

| Keyword | Tasks |
| --- | --- |
| authorization | 4 |
| availability | 1 |
| booking | 1 |
| browser-graded | 1 |
| concurrency | 4 |
| csrf | 4 |
| data-reconciliation | 1 |
| dst | 4 |
| email | 4 |
| fefo | 1 |
| held-karp | 1 |
| idempotency | 3 |
| llm-judge | 3 |
| multi-tenant | 1 |
| overtime | 1 |
| payments | 3 |
| payroll | 1 |
| physiotherapy | 1 |
| pick-path | 1 |
| postgresql | 5 |
| python | 1 |
| refunds | 2 |
| rewardkit | 3 |
| scheduling | 1 |
| stripe | 1 |
| stripe-compatible | 1 |
| ticketing | 1 |
| time-zones | 4 |
| waitlist | 1 |
| warehouse | 1 |
| web-application | 5 |
| webhooks | 3 |
| zero-to-one | 4 |

## Dataset structure

Each task is one Harbor task in a directory named by its uuid.

| Task | Keywords |
| --- | --- |
| [`145c5287-517b-5048-a526-4a66487b9554`](145c5287-517b-5048-a526-4a66487b9554/) | browser-graded, data-reconciliation, postgresql, python, web-application |
| [`7cce0d38-d0ac-45f8-b277-db41d60db768`](7cce0d38-d0ac-45f8-b277-db41d60db768/) | authorization, concurrency, csrf, dst, email, idempotency, llm-judge, multi-tenant, payments, postgresql, rewardkit, stripe-compatible, ticketing, time-zones, web-application, webhooks, zero-to-one |
| [`8afcb304-47f7-47d5-9a47-ac3ccadd0ca9`](8afcb304-47f7-47d5-9a47-ac3ccadd0ca9/) | authorization, availability, booking, concurrency, csrf, dst, email, payments, physiotherapy, postgresql, refunds, scheduling, time-zones, waitlist, web-application, webhooks, zero-to-one |
| [`c5e57df8-355f-4931-8167-b8c9ace8a425`](c5e57df8-355f-4931-8167-b8c9ace8a425/) | authorization, concurrency, csrf, dst, email, fefo, held-karp, idempotency, llm-judge, payments, pick-path, postgresql, refunds, rewardkit, stripe, time-zones, warehouse, web-application, webhooks, zero-to-one |
| [`dd267972-4223-4439-afe5-095fe11a34f2`](dd267972-4223-4439-afe5-095fe11a34f2/) | authorization, concurrency, csrf, dst, email, idempotency, llm-judge, overtime, payroll, postgresql, rewardkit, time-zones, web-application, zero-to-one |

```
<uuid>/
├── task.toml         runner manifest
├── instruction.md    specification the agent reads
├── README.md         task-level notes: layout, scoring, verifier requirements
├── environment/      sandbox image: empty /app, PostgreSQL with the old system's data,
│                     a mail sink, and a Stripe-compatible payments mock where the task needs one
├── tests/            verifier, hidden from the agent: strict workflows, the reference engine,
│                     Reward Kit criteria, and the judge config
├── solution/         reference solution (solve.sh installs it into /app)
└── trials/
    └── gpt-6.1-sol/  agent model: config.json, lock.json, result.json, trial.log,
                      agent/, verifier/, artifacts/
```

## Reproduction

Each task directory is a complete Harbor task: `task.toml` is the runner manifest, `instruction.md` the specification, `environment/` the sandbox image, `tests/` the verifier, and `solution/` the reference solution. The trials above were produced with:

```bash
harbor run -a codex -m openai/gpt-6.1-sol
```

The verifier needs `ANTHROPIC_API_KEY` with access to the judge model `claude-fable-5-1`; without it the evaluation is marked invalid rather than scored. The verifier's network is an allowlist of `api.anthropic.com` alone, and the key reaches only a local credential proxy, never the application under test. Every verifier writes a fail-closed zero before it starts, so a verifier, service or judge failure leaves that zero and records the reason instead of awarding partial credit.

The agent's own network is closed: `145c5287` runs with no network at all, and the four build tasks allow only `registry.npmjs.org`, `pypi.org` and `files.pythonhosted.org`. Each task states a grading horizon in `[metadata].valid_until`, after which its seeded dates and reference answers should not be trusted: 2027-11-01 for `c5e57df8`, 2027-11-11 for `dd267972`, 2028-11-09 for `145c5287`, 2029-09-30 for `7cce0d38`, and 2030-01-31 for `8afcb304`.

Delivery format and difficulty target were calibrated against Vibe Code Bench (arXiv:2603.04601). No instruction text, seed corpus, fixture, expected value, grading criterion or reference implementation is derived from that benchmark or from any other public source; all of it is original work authored for these tasks.

## Cite

```bibtex
@misc{allmight2026,
  title        = {allmight: benchmark tasks for building a web application from nothing},
  author       = {{Ethara.AI}},
  year         = {2026},
  howpublished = {\url{https://github.com/Ethara-Ai}}
}
```

## Contact

allmight is built by [Ethara.AI](https://github.com/Ethara-Ai). For questions about the benchmark or dataset, contact [research@ethara.ai](mailto:research@ethara.ai).

Copyright (c) 2026 Green Rider Technology LLP (Ethara AI). All rights reserved.
