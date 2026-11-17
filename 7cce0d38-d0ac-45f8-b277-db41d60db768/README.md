# ethara/seatwise-tickets

A zero-to-one web-application task: the agent builds a reserved-seat ticketing platform (venues and seat maps in venue time zones, holds, exact fees, tax and promo allocation, refunds, transfers, online and offline check-in, waiting lists) over an existing database from an old platform, from the specification in `instruction.md`.

## Layout
- `instruction.md`: the specification the agent receives.
- `environment/`: the agent's container (empty `/app`, PostgreSQL with the existing data, a mail sink and a payments provider mock).
- `solution/`: the reference application (`solve.sh` installs it into `/app`) and `validation/` notes.
- `tests/`: the verifier, run in a separate container: `test.sh`, the strict workflows (`workflows.py` and `wf_a.py`–`wf_d.py`, with `harness.py` and `kitlib/`), the verifier's own copies of the services, a reference model, and the Reward Kit criteria and judge config.

## Scoring
reward = 0.9 x functional + 0.1 x judge. Functional is the weighted share of 27 strict workflows (a workflow passes only if every step passes). Any observed security breach, or any failed critical workflow (15 are critical), caps every workflow at 0.4. A critical workflow that fails only because the verifier's time budget ran out scores 0 without triggering the cap. `test.sh` writes a fail-closed zero first; a verifier, service or judge failure leaves that zero and records why in `/logs/verifier/evaluation-invalid.json`.

## Verifier requirements
- `ANTHROPIC_API_KEY` must be set for the verifier (`[verifier.env]` passes it through). It must have access to the judge model `claude-fable-5-1`. Without it the evaluation is marked invalid.
- The verifier's network is an allowlist of `api.anthropic.com` only; the key reaches only a local credential proxy, never the application under test.
- `[verifier].timeout_sec` is 10800; the workflows share a time budget so a slow application keeps the credit it earned.
- Date horizon: the verifier and seed data are valid for grading until 2029-09-30 (`[metadata].valid_until`).

## Author
shivansh.rai <shivansh.rai@ethara.ai>
