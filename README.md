# The Traveler

A website whose server never stays put. Every three to five minutes its backend, a single
AWS Lambda function, builds a copy of itself in another AWS region, moves visitors' traffic
there, and deletes the old copy. Nobody deploys it; the running code carries itself
around the world.

**Live:** https://d2w15vlwpzsfqy.cloudfront.net. The page is both the demo and the
explanation: a map of where it is and where it goes next, the nine steps of each move as
they happen, a latency chart from your own browser that jumps when the answering region
changes, and a feed of facts it looks up at each stop.

## How a move works

```
browser ──▶ CloudFront ──▶ edge function reads `current_origin` from a KeyValueStore
                │                     │
                ├─▶ S3 (static page)  └─▶ Lambda Function URL in the live region
                └─▶ tower read API ◀── DynamoDB "control tower" (state, hops, events)
```

1. **Leaves.** At the end of a 3–5 minute stay the live copy picks a destination from a
   17-region allowlist.
2. **Builds a copy.** It fetches its own package and creates the function in the new region.
3. **Starts it**, **gives it a Function URL** (reachable only through CloudFront) and
   **checks its health and release**.
4. **Switches the route.** One `UpdateKeys` call points the edge at the new copy.
5. **Waits for the world.** Edge locations converge in about 60 s; the old copy keeps
   answering for at least 90 s and until ten probes in a row reach the new copy, so no
   request is dropped.
6. **Hands over.** The new copy claims the hop with a conditional write, the old copy
   exits, and **the new copy deletes the old one**.

A move takes about 110 s, almost all of it step 5. If any step fails the move is
cancelled, the route is restored if it changed, and the traveler stays put. Outside a
move exactly one copy exists; during one, two. A watchdog in the home region checks every
5 minutes, deletes strays and resurrects a traveler that stops.

## Results

- **Reliability.** 20 forced back-to-back hops with a prober hitting the site every
  second: about 2,770 probes, zero failed. A 24-hour unattended run: 244 of 244 hops
  landed. Since launch on 2026-11-07, every hop has landed. Each forced failure (create,
  health, edge settle, missing arrival) rolled back cleanly.
- **Cost.** About $8 a month always on, 93% of it routing-key updates at $0.001 each;
  compute is effectively free. See [docs/cost-report.md](docs/cost-report.md).
- **Honest flight.** The plane on the map advances only on real phase events, and the
  countdown uses the server's clock: across 600 hops, departures landed within
  ±1 s of the countdown (median 0.05 s early).

## Repo

| Path | What |
|---|---|
| `traveler/` | The traveling function: lifecycle and handoff, placement, routing, tower client, fact engine (`facts/`) |
| `route_writer/` | Home-region function that writes the KeyValueStore (needs SigV4A, which the traveler package leaves out) |
| `watchdog/`, `traveler/watchdog.py` | Five-minute invariant checks, stray cleanup, resurrection |
| `tower/` | Cached read API behind `/api/tower/*` |
| `edge/` | CloudFront Function that routes `/api/traveler/*` to the live region |
| `site/` | Static front end, no framework, no third-party requests |
| `infra/terraform/` | Everything in AWS, including IAM with a permissions boundary and GitHub OIDC roles |
| `scripts/ops.py` | Operator CLI: release, wake, sleep, status, facts |
| `docs/` | [decisions](docs/decisions.md), [phase gates](docs/gates.md), [runbook](docs/runbook.md), [cost report](docs/cost-report.md) |

## Deploy your own

Needs an AWS account you control (admin credentials, default regions only), Terraform
1.10+, Python 3.11+, Node 20+ for the tests, and a GitHub repo for CI.

```bash
make venv                   # local tooling
make bootstrap init         # Terraform state bucket, git hooks, terraform init
cp infra/terraform/terraform.tfvars.example infra/terraform/terraform.tfvars
#   set alert_email, github_repo and github_oidc_subject_prefix
make deploy                 # apply, publish the site, publish a release, wake it up
```

`terraform output` prints the site URL and two CI role ARNs. For merges to `main` to
deploy, add three repository secrets: `AWS_PLAN_ROLE_ARN`, `AWS_DEPLOY_ROLE_ARN` and
`ALERT_EMAIL`. They are secrets rather than variables so the account ID and address stay
masked in public Actions logs. `make status` shows where it is; `make sleep` stops it;
`make destroy` removes everything. More in the [runbook](docs/runbook.md).

The allowlist in `infra/terraform/variables.tf` must contain only regions enabled on the
account. New accounts get 10 concurrent Lambda executions per region, which is enough.

## Not built

- An archive of every hop to S3 and all-time totals; the page's flight stats cover the
  last 100 hops.
- A stats page (slowest region to start, most-visited region, source reliability).
- A WAF rule (left off to keep the edge free, decision D15) and per-hop tracing.

## License

MIT
