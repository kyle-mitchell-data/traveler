# Decision log

Amendments to the build spec. Where these conflict with the spec, these win.

## D1. Budget: one $25/month budget

Replaces the spec's $10 and $20 alerts. Email alerts at $10, $20 and $25 actual
spend, plus a forecast alert at $25. Alerts go to the address in `terraform.tfvars`
(not committed).

## D2. Account stays on the AWS Free plan for now

The account is on the credit-based Free plan (expires 2027-04-28). Revisit before
phase 2 creates CloudFront, and again before the plan expires.

## D3. Lambda concurrency: build on the default quota, raise before launch

New accounts get 10 concurrent executions per region, and reserved concurrency can
only be set up to (unreserved − 100). So reserved concurrency is impossible today.

- Phases 1–5 run on the default quota. The account limit acts as the cost cap.
- Quota increases to 1,000 are requested per allowlisted region in the background.
- Reserved concurrency (10) is applied per region as each increase lands.
- Visitor handlers stay trivial (no network calls inside a request) so demo traffic
  uses 2–3 slots at most.

## D4. Async handoff cannot leave a ghost

Throttled async invokes are retried for up to 6 hours by default. A delayed P8
invoke could wake a traveler after the hop already rolled back. Every traveler
function is created with `MaximumEventAgeInSeconds=60` and `MaximumRetryAttempts=0`,
and arrival is idempotent per `hop_id`.

## D5. Nothing polls the key-value store API

Every KeyValueStore API call costs $0.001 (confirmed via the AWS Pricing API,
2026-10-28), reads included. Only the P6 route flip (and rollback, wake, sleep)
calls the store. The watchdog and `make status` read state from the tower table.

## D6. One cacheable tower feed

`/api/tower/status` and `/api/tower/events?since=` merge into `/api/tower/feed`,
which returns the state snapshot plus the last ~20 events. Every browser requests
the same URL, so the 1-second edge cache is shared across viewers. Clients drop
events they have already seen.

## D7. Polling cadence

Feed every 2 s, every 1 s during a flight. Ping every 5 s. All polling stops while
the tab is hidden. Target: about 2,500 CloudFront requests per viewer-hour.

## D8. Plain Lambda, not containers

Fargate and EC2 were considered for a single long-running process that serves
requests. Rejected for cost (public IPv4 + compute pushes always-on past budget),
per-region sprawl, slower flights, and losing the "carries itself" mechanic. A
container traveler may come back as an optional phase 7 variant.

## D9. Missing traveler permissions

Added to the traveler role beyond the spec's list: `GetFunctionUrlConfig`,
`PutFunctionEventInvokeConfig` (D4), `PutFunctionConcurrency` (D3), `TagResource`,
`s3:GetObject` on the release store, and the KeyValueStore read/update actions.

## D10. CloudFront default domain until a brand exists

Phase 2's "custom domain over HTTPS" gate is met with the `*.cloudfront.net`
domain until a name is chosen. ACM certificates for CloudFront live in us-east-1
regardless of the home region.

## D11. Per-region resources use provider v6 resource-level `region`

Instead of generating one provider alias per region, per-region resources (log
groups) use `for_each` over the allowlist with the `region` argument that the AWS
provider added in v6.

## D12. CI roles

Two GitHub OIDC roles, trusted only by `kyle-mitchell-data/traveler`:

- `traveler-ci-plan`: pull requests only. Read-only plus state-lock writes.
- `traveler-ci-deploy`: `main` branch only. Administrator access, with explicit
  denies on creating IAM users or access keys, so CI can never mint long-lived
  credentials. Scoping this down is a phase 6 hardening item.

## D13. Home region and allowlist

Home region us-east-2. The allowlist is all 17 regions enabled by default on the
account (no opt-in regions).
