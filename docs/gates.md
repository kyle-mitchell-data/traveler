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

## Phase 4: Autonomous travel — passed

| Criterion | Result |
|---|---|
| Runs 24 hours unattended with all invariants holding | **Pass.** 2026-11-06 04:12 → 2026-11-07 04:12 UTC, 17-region allowlist, 3–5 min stays, `pin_awake=true`. 244 hops started, 244 landed, 0 aborted; all 17 regions visited; 289–416 s between departures. Watchdog, 288 samples each: `Copies` 1–2, `InvariantViolations` 0, `ConsecutiveAborts` 0, `HeartbeatAgeSeconds` max 89 s. All three alarms OK for the whole window. Four releases adopted mid-run; about 15.9 h ran phase 4 code and the last 8.1 h ran the phase 5 release `r202611062005-8d9404c`. The 11 aborts on the counter all predate the run (phase 2/3 testing, 2026-11-06 00:56–03:25). |
| Watchdog resurrects a hand-deleted traveler within 10 minutes | Pass (2026-11-06): deleted in eu-west-2 at 04:03:42; traveler noticed in 10 s and stopped; watchdog resurrected in eu-north-1 after 331 s. About 60 s of 403s while the edge moved off the deleted copy. |
| A new release published by CI is running within 2 hops | **Pass.** Push `a82fc2f` → ci green → release workflow applied infra and published `r202611061815-a82fc2f` at 18:15:26 UTC; adopted at the very next hop (h-000191, ap-northeast-3 → eu-west-2, landed, 115 s flight), mid-way through the 24 h run with no aborts. First attempt failed at OIDC: the repo issues immutable (ID-pinned) subjects, now trusted alongside the name form. |

Also verified: intercontinental hops us-east-2 → ap-northeast-1 (111.0 s) → eu-central-1
(111.6 s), zero failed probes in flight.

## Phase 5: Fact engine — passed

Deployed as `r202611062005-8d9404c` at 20:05 UTC on 2026-11-06 and ran the last 8.1 h of
the phase 4 run: 777 `STATEMENT` events in that window, `AdapterFailures` 0, and a check of
47 stays found 6–10 statements each with no rule broken. Local
evidence (`tests/test_facts.py`, recorded API responses for Tokyo, all 17 profiles):

| Criterion | Result |
|---|---|
| Each stay posts stay ÷ 30 statements; slot 1 is a right-now fact | Local pass: `slots_for` table; the simulated stay loop posts 3 statements for a 95 s stay on every hop and wake; slot 1 is time or weather and names the place |
| No consecutive same-category; no repeats within 3 visits | Local pass: 4 online visits to Tokyo and 6 offline visits to every region; online never repeats; offline is clean for 3 visits, then releases the oldest visit first (D29) |
| With every network adapter off, a full set from computed and evergreen facts | Local pass: 10 of 10 statements, every region, every visit |
| Selection is reproducible for a given hop ID | Local pass: same hop, same statements; different hops differ |

Also checked live from the laptop (`make facts`): all six network adapters answered for
London, Tokyo, Northern Virginia, Frankfurt and Northern California. After deploy: confirm
in AWS from the tower's `STATEMENT` events and the hop records' `facts` summaries.

