# Decision log

Amendments to the build spec. Where these conflict with the spec, these win.

## D1. Budget: one $25/month budget

Replaces the spec's $10 and $20 alerts. Email alerts at $10, $20 and $25 actual
spend, plus a forecast alert at $25. Alerts go to the address in `terraform.tfvars`
(not committed).

## D2. Account: Paid plan, advanced features, no region lock

The account was created with "Sign up for AWS (new)", which pins accounts to one
Region through an organization SCP. On 2026-11-05 it was upgraded to the Paid plan
(credit carried over), advanced features were activated, and the replacement
`AdvancedModeRegionRestrictionSecurityControlPolicy` was detached by hand in the
management account. All 17 default Regions are usable. The AWS Budgets spend-limit
SCPs were left in place as an extra backstop.

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

## D14. Recursive loop detection is set to Allow on every copy

Lambda stops chains of about 16 invocations that pass trace headers, including
Lambda-to-Lambda async invokes. Each new traveler copy calls
`PutFunctionRecursionConfig RecursiveLoop=Allow` at P2, before anything invokes it.
The watchdog and copy count remain the guard against runaway replication.

## D15. CloudFront on pay-as-you-go, no WAF

The free flat-rate plan excludes KeyValueStore (it starts at Pro, $15/month), so
the distribution uses pay-as-you-go, whose free tier (1 TB, 10M requests, 2M
function invocations a month) covers demo traffic at $0. WAF on pay-as-you-go is
$5 per web ACL plus $1 per rule even when idle, so it is skipped. Floods are capped
by Lambda concurrency, edge caching and the budget alerts.

## D16. Route flips may run in the home region

KeyValueStore writes use SigV4A, which boto3 handles through `awscrt`. If the Lambda
runtime does not bundle it, the traveler asks a small home-region function to
perform the flip, so the multi-MB library never travels with the traveler. To be
tested in phase 2.

## D17. Each copy grants CloudFront access to itself

With Origin Access Control on IAM-auth Function URLs, every new copy needs
`lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` granted to
`cloudfront.amazonaws.com` with the distribution as `SourceArn`. Done at P4 with
`AddPermission`.

## D18. At most 10 custom metrics, none split by Region

The first 10 CloudWatch metrics and 10 alarms are free. A metric split by Region
becomes 17 metrics, so Region goes in the log line, not in a metric dimension.

## D19. Hop summaries live under one partition

The spec's `HOP#<hop_id> / SUMMARY` layout cannot list recent hops without a scan.
Hop summaries use `pk=HOP, sk=<hop_id>` instead, so `/api/tower/hops` is a single
newest-first query. Hop IDs are zero-padded (`h-000412`), so they sort correctly.
The shape of each summary is unchanged.

## D20. `make wake` shares code with the handoff

`traveler/placement.py` (create, health check, remove) and `traveler/routing.py`
(the route flip) are used both by the operator CLI (`scripts/ops.py`) and, from
phase 3, by the traveler itself. Wake is therefore an exercise of P2–P6 before any
hop exists.

## D21. KeyValueStore propagation is about 60 seconds, not "seconds"

Measured 2026-11-06 from one edge location (MCI), flipping only the `status` key
with no function changes: first edge server sees the change after 23–31 s, servers
in the same location disagree (responses alternate) for about 30 s more, and the
edge is stable after about 60 s. Both directions behaved the same.

Accepted 2026-11-06:

- Flight cap is 180 s, replacing the spec's 120 s.
- P7 requires both: at least `EDGE_PROPAGATION_S` (90 s) since the P6 flip, and a
  run of consecutive responses from the new region. Only then is ARRIVED allowed
  to retire the old copy.
- `make sleep` waits `EDGE_PROPAGATION_S` between the flip and deleting copies
  (`NOW=1` skips the wait for emergencies). Verified: 234 edge responses during a
  sleep, all 200 or 503, zero 403s.

Background and consequences:

- P7 cannot use "3 responses in a row". It needs a long consecutive run (or a fixed
  floor of about 90 s after P6) before ARRIVED is allowed to delete the old copy.
- The old copy must stay alive and serving until then. That keeps G3 (no failed
  requests) achievable: stale edges reach the old copy, which still answers 200.
- Flights will take roughly 90–100 s, mostly edge propagation. That is the honest
  handoff time (G2). The 120 s flight cap is too tight; propose 180 s, which still
  fits the 600 s invocation timeout (300 s stay + 30 s packing + 180 s flight).
- `make sleep` must flip, wait for propagation, then delete. Deleting immediately
  causes about 60 s of 403s from edges that still route to the deleted copy.

## D22. Traveler round trip is slower than the network

Through CloudFront, `ping` to eu-central-1 takes about 0.4 s and to ap-southeast-2
about 0.8–1.2 s, roughly 3x the network round trip. CloudFront appears to open a
new TLS connection to the Function URL for most requests. Investigate in phase 6
before the "You are N ms away" figure is shown to visitors.

## D23. Traveler timeout is 900 seconds

The worst case for one invocation is now: arrival (up to 60 s waiting for the old
copy's exit) + a 300 s stay + a 180 s flight cap + a 90 s rollback wait. That exceeds
the spec's 600 s, so the traveler uses Lambda's maximum of 900 s, and each stay is
shortened if needed to leave `RESERVE_S` (300 s) for a flight that rolls back.

## D24. Route flips go through `traveler-route-writer` (D16 resolved)

Verified 2026-11-05 with a throwaway function: the python3.13 Lambda runtime has no
`awscrt`, and a KeyValueStore call fails with `MissingDependencyException`. The
route writer (home region, 256 MB, awscrt bundled) is the only non-operator writer,
and it accepts only the four routing keys with validated values. The traveler
package stays about 10 KB.

## D25. One hop, one winner

The hop record moves from `in_flight` to either `arrived` (written by the new copy)
or `aborted` (written by the old copy), by conditional write. Whoever loses stands
down: a new copy that arrives late does nothing and is deleted by the rollback; an
old copy that times out just as the new one arrives carries on as a normal handoff.

## D26. Forced failures are real and one-shot

The phase 3 gate requires forced failures at P2, P5, P7 and a missing ARRIVED. The
operator sets `/traveler/inject` to a phase name; the next hop then fails for real
at that phase (a role that does not exist, a wrong release, an edge that never
settles, a lost suitcase) and the traveler deletes the switch as it fires. The
traveler's only SSM write permission is deleting that one parameter.

## D27. Watchdog rules

Every 5 minutes, from the home region, reading the tower and Lambda (never the KVS):

- A copy outside the tower's region and older than 10 minutes is a stray: deleted
  and reported. Younger copies are mid-flight and left alone.
- While resting, copies are removed only once the tower has been quiet for 10
  minutes, so `make sleep`'s 90 s edge wait is never cut short.
- Dead means: live, but no tower update for 10 minutes, or the tower's region has
  no copy and nothing has updated for 2 minutes (the 2 minutes covers a landing).
  The watchdog resurrects in another region from the latest release, then waits
  for the edge before deleting any old copy, because a copy whose loop died can
  still serve visitors. The kill switch blocks resurrection.
- A traveler that finds its own function deleted stops heartbeating, so a hand
  deletion is resurrected within about 7 minutes.

Metrics `HeartbeatAgeSeconds`, `Copies`, `ConsecutiveAborts` and
`InvariantViolations` (no dimensions) drive three alarms to the `traveler-alerts`
topic. The watchdog holds a tower lock so runs never overlap.

## D28. Auto-sleep

At each departure the traveler rests if it has been awake (`awake_since`, set by
wake and kept by resurrection) for `auto_sleep_hours` (12) unless `pin_awake` is
true. The 24-hour gate run uses `pin_awake=true`.
