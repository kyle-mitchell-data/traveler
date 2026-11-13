# Cost report

Actual AWS charges from Cost Explorer, before credits, for the first week of continuous
running (2026-11-06 to 2026-11-12, always on, about 245 hops a day across 17 regions).
Credits covered all of it; nothing was billed.

## Per day

| Day (UTC) | Gross | Note |
|---|---|---|
| 11-05 | $0.010 | build day, a few manual hops |
| 11-06 | $0.511 | 24-hour gate run starts 04:12 |
| 11-07 | $0.538 | front end launched 13:31 |
| 11-08 | $0.474 | |
| 11-09 | $0.480 | |
| 11-10 | $0.446 | route ETag fix deployed 17:15 (D5) |
| 11-11 | $0.264 | first full day after the fix |
| 11-12 | $0.264 | |

## Steady state, always on (11-11 and 11-12)

| Line | Per day | Per 30 days | Driver |
|---|---|---|---|
| CloudFront KeyValueStore API | $0.245 | $7.35 | One `UpdateKeys` per hop at $0.001 |
| DynamoDB (tower) | $0.013 | $0.38 | Phase events, statements, hop records |
| Lambda | $0.006 | $0.19 | Mostly inside the free tier; includes cross-region package copies |
| S3, Secrets Manager, SNS, CloudWatch | < $0.001 | < $0.03 | |
| **Total** | **$0.264** | **about $7.95** | |

CloudFront requests, edge functions and TLS cost nothing at this traffic. Cost Explorer
API calls ($0.01 each, $0.13 for the week) are excluded: they come from checking the
bill, not from running the site.

## Against the cost model

The model expected $5–20 a month always on, with the key-value store as the largest line
(about $10 at 320 hops a day). Actual: about $8 a month at 245 hops a day, the key-value
store 93% of it. Compute is effectively free.

Before the fix on 11-10 each hop made two KeyValueStore calls, because the route writer
runs as a cold container on almost every flip and lost its cached ETag. Daily calls went
from 455–461 to 245 once the ETag moved to the tower (D5 amendment).

Demo-only use (auto-sleep after 12 hours, woken by hand) scales with hours awake: about
$0.011 per awake hour.
