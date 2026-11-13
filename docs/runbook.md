# Runbook

Every command runs from the repo root with AWS credentials for the account
(`AWS_PROFILE`, default `traveler`). The Makefile wraps `python -m scripts.ops`; on a
machine without `make`, run the command it shows.

## Everyday

| Task | Command | What happens |
|---|---|---|
| Check | `make status` | Tower state (region, phase, next stop, release, hop counts) plus a live count of traveler copies across the allowlist. Two copies is normal mid-flight; outside a flight it prints a warning. |
| Wake | `make wake` (`REGION=eu-west-1` to choose) | Places the latest release in one region and routes the edge to it. It starts hopping after its first stay. |
| Sleep | `make sleep` (`NOW=1` skips the edge wait) | Routes the edge to the resting page, waits about 90 s for every edge location, then deletes every copy. |
| Ship code | merge to `main` | CI checks, then the release workflow applies Terraform, publishes the site and publishes the traveler package. The traveler adopts it at its next hop. |
| Ship code by hand | `make apply site release` | Same as the release workflow, from a laptop. |
| Preview facts | `make facts REGION=ap-northeast-1 ORIGIN=us-east-2` | Prints what a stay would say from the live sources. Writes nothing. |
| Destroy | `make destroy` | Sleeps, then `terraform destroy`. The Terraform state bucket is kept. |

## Switches

SSM parameters under `/traveler/`, read fresh on every check. `make set NAME=… VALUE=…`,
`make unset NAME=…`.

| Name | Effect |
|---|---|
| `kill=true` | At its next departure the traveler rests instead of hopping, and the watchdog will not resurrect it. |
| `pin_awake=true` | Never auto-sleep. Unset, it rests after `auto_sleep_hours` (12) awake. **Currently set: the demo runs always on.** |
| `stay_seconds` | Fixed stay length instead of a random 3–5 minutes (testing). |
| `hop_regions` | Comma-separated subset of the allowlist (testing). |
| `max_hops` | Rest after this many more hops (testing). |
| `adapters_off` | `all` or a list of fact sources to skip; computed and curated facts still post. |

## Alarms

Three CloudWatch alarms in the home region go to the `traveler-alerts` SNS topic, which
emails `alert_email`. The watchdog runs every 5 minutes and also resurrects or cleans up
on its own (decision D27), so most alarms clear without a person.

| Alarm | Means | Do |
|---|---|---|
| `traveler-no-heartbeat` | No tower update for 10 minutes. | `make status`. The watchdog should have resurrected it in another region; if the kill switch is on it will not. If it is stuck, `make sleep NOW=1` then `make wake`. |
| `traveler-aborts` | Three cancelled hops in a row. Each cancelled hop rolls back and the traveler stays put, so the site is still up. | Read the `HOP_ABORTED` events in the tower (`reason` holds the internal error) and the traveler's logs in the region it is in. A bad release: fix and merge, or republish the previous one. |
| `traveler-invariant` | More than two copies, or one copy where the tower says another region. | `make status`. The watchdog deletes stray copies older than 10 minutes on its next run. |

## Where to look

- **Tower** (DynamoDB `traveler-tower`, home region): `STATE/CURRENT`, `HOP/<hop_id>`
  records with phase timestamps, `EVENT` items (phases, statements, aborts).
- **Logs:** `/aws/lambda/traveler` in the region the traveler is in (3-day retention),
  `/aws/lambda/traveler-watchdog`, `/aws/lambda/traveler-route-writer` and
  `/aws/lambda/traveler-tower` in the home region.
- **Metrics:** namespace `Traveler`.
- **Cost:** `docs/cost-report.md`. About $8 a month always on.
