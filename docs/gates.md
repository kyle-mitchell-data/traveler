# Phase gates

Evidence for each phase's acceptance criteria. Dates are UTC.

## Phase 1: Bootstrap

| Criterion | Result |
|---|---|
| `terraform plan` clean from a fresh clone | Pass (2026-11-05): fresh `gh repo clone`, init, plan → "No changes" |
| A test budget alert reaches Kyle's email | **Open**: `traveler-alert-test` ($0.01, credits excluded) waits on billing data |
| No long-lived AWS keys anywhere | Pass (2026-11-05): bootstrap IAM user and key deleted; account has 0 IAM users; local access is IAM Identity Center only |

## Phase 2: One region, routed — passed

| Criterion | Result |
|---|---|
| Custom domain serves the page over HTTPS | Pass on the CloudFront default domain (D10) |
| `/api/traveler/whereami` returns the right region through the edge | Pass for us-east-2, ap-southeast-2, eu-central-1, eu-west-2 |
| `make sleep` produces the resting response; `make wake` brings it back | Pass; after D21, sleep caused zero 403s across 234 edge probes |

Also verified early: direct calls to a traveler Function URL return 403 (a phase 6 criterion).

## Phase 3: Handoff between two regions — passed

| Criterion | Result |
|---|---|
| 20 consecutive hops, zero non-200 from the synthetic prober | Pass (2026-11-06 01:14–02:10): 20/20 landed us-east-2 ↔ us-east-1, flights 103–107 s, about 2,770 probes, 0 failures during hops. Non-200s only in the wake window before the first departure and the deliberate rest after the last landing. |
| Never more than 2 traveler functions, checked continuously | Pass: copies counted every 5 s; maximum 2 |
| Forced failure at P2 rolls back cleanly, 1 function left | Pass: h-000036, CreateFunction denied (`iam:PassRole` on a role the traveler does not own) |
| Forced failure at P5 | Pass: h-000037, health check saw the wrong release |
| Forced failure at P7 | Pass: h-000038, "edge did not settle in time", rolled back, next hop landed |
| Missing ARRIVED | Pass (2026-11-06): h-000041, "no ARRIVED within 60 s", routed back, new copy deleted, 1 function left |

Found and fixed during the gate: stale-ETag route writes fail with `ValidationException`,
not `ConflictException` (commit 7fd5bc2). Five real P6 aborts before the fix all rolled
back cleanly and raised the 3-in-a-row alarm event.

Also verified early: a release published mid-run was adopted at the next hop (a phase 4
criterion).

## Phase 4: Autonomous travel — in progress

| Criterion | Result |
|---|---|
| Runs 24 hours unattended with all invariants holding | **Running** since 2026-11-06 ~04:12 UTC: 17-region allowlist, 3–5 min stays, `pin_awake=true`. Evidence: watchdog metrics (`Copies`, `InvariantViolations`, `HeartbeatAgeSeconds`) every 5 min, hop records, alarms. |
| Watchdog resurrects a hand-deleted traveler within 10 minutes | Pass (2026-11-06): deleted in eu-west-2 at 04:03:42; traveler noticed in 10 s and stopped; watchdog resurrected in eu-north-1 after 331 s. About 60 s of 403s while the edge moved off the deleted copy. |
| A new release published by CI is running within 2 hops | Pending CI (needs the `workflow` token scope and repo secrets). Manual `make release` adoption already verified in phase 3. |

Also verified: intercontinental hops us-east-2 → ap-northeast-1 (111.0 s) → eu-central-1
(111.6 s), zero failed probes in flight.
