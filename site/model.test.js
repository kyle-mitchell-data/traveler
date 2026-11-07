import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import * as M from './model.js';

const REGIONS = {
  'eu-west-1': { code: 'eu-west-1', city: 'Dublin', near: 'near Dublin, Ireland', lat: 53.35, lon: -6.26, tz: 'Europe/Dublin' },
  'ap-northeast-3': { code: 'ap-northeast-3', city: 'Osaka', near: 'near Osaka, Japan', lat: 34.69, lon: 135.5, tz: 'Asia/Tokyo' },
  'us-west-2': { code: 'us-west-2', city: 'Oregon', near: 'in Oregon, USA', lat: 45.84, lon: -119.7, tz: 'America/Los_Angeles' },
};

const iso = (ms) => new Date(ms).toISOString();
const T0 = Date.parse('2026-11-07T01:52:05.000Z');

function phase(seq, name, sec, hop = 'h-000268') {
  return { seq, type: 'PHASE', hop_id: hop, phase: name, origin: 'eu-west-1', destination: 'ap-northeast-3', region: 'eu-west-1', ts: iso(T0 + sec * 1000), elapsed_ms: sec * 1000 };
}

test('projection matches the pins the Python build placed', () => {
  // Regenerate with: python -m scripts.site_build, then read map-data from build/site/index.html
  const pins = JSON.parse(readFileSync(new URL('./model.test.pins.json', import.meta.url), 'utf8'));
  for (const p of pins) {
    const [x, y] = M.project(p.lon, p.lat);
    assert.ok(Math.abs(x - p.x) < 0.11 && Math.abs(y - p.y) < 0.11, `${p.code}: ${x},${y} vs ${p.x},${p.y}`);
  }
});

test('great circle ends at both cities and splits at the antimeridian', () => {
  const pts = M.greatCircle(REGIONS['us-west-2'], REGIONS['ap-northeast-3']);
  assert.deepEqual(pts[0].map((v) => +v.toFixed(3)), [-119.7, 45.84]);
  assert.deepEqual(pts.at(-1).map((v) => +v.toFixed(3)), [135.5, 34.69]);
  assert.equal((M.pathData(pts).match(/M/g) || []).length, 2);
  assert.equal((M.pathData(M.greatCircle(REGIONS['eu-west-1'], REGIONS['us-west-2'])).match(/M/g) || []).length, 1);
});

test('distance Dublin to Osaka is about 9,500 km', () => {
  const km = M.distanceKm(REGIONS['eu-west-1'], REGIONS['ap-northeast-3']);
  assert.ok(km > 9300 && km < 9700, String(km));
});

test('along() stays on the map and heads the right way', () => {
  const pts = M.greatCircle(REGIONS['eu-west-1'], REGIONS['ap-northeast-3']);
  for (let t = 0; t <= 1; t += 0.05) {
    const p = M.along(pts, t);
    assert.ok(p.x >= 0 && p.x <= M.WIDTH && p.y >= 0, JSON.stringify(p));
  }
  const start = M.along(pts, 0);
  assert.ok(Math.cos((start.heading * Math.PI) / 180) > 0, 'Dublin to Osaka starts heading east');
});

test('plane progress moves only on phase events and never backwards', () => {
  assert.equal(M.progressFor({}), 0);
  const seen = {};
  let last = 0;
  for (const p of M.PHASES) {
    seen[p.name] = { ts: '' };
    const now = M.progressFor(seen);
    assert.ok(now > last, p.name);
    last = now;
  }
  assert.equal(last, 1);
  // Out-of-order arrival still gives the furthest phase.
  assert.equal(M.progressFor({ ROUTE_FLIPPED: {}, DEPARTED: {} }), M.PHASES.find((p) => p.name === 'ROUTE_FLIPPED').at);
});

test('ease glides toward the target and snaps when close', () => {
  let v = 0;
  for (let i = 0; i < 10; i++) v = M.ease(v, 0.5, 100);
  assert.ok(v > 0.4 && v <= 0.5);
  for (let i = 0; i < 100; i++) v = M.ease(v, 0.5, 100);
  assert.equal(v, 0.5);
  assert.equal(M.ease(0.8, 0, 16), 0, 'a new flight starts from the beginning');
});

test('server clock prefers the fastest ping and falls back to the Date header', () => {
  const c = new M.ServerClock();
  assert.equal(c.offset, 0);
  c.addDateHeader('Sat, 07 Nov 2026 01:52:05 GMT', T0 - 3000);
  assert.equal(c.offset, 3500);
  assert.equal(c.precise, false);
  c.addPing(T0 + 10_000, T0, T0 + 400); // rtt 400: offset 10_000 + 200 - 400
  c.addPing(T0 + 10_050, T0, T0 + 100); // rtt 100: offset 10_050 + 50 - 100
  assert.equal(c.offset, 10_000);
  assert.equal(c.precise, true);
  assert.equal(c.now(T0), T0 + 10_000);
});

test('event log dedupes by seq and keeps the newest', () => {
  const log = new M.EventLog(3);
  assert.equal(log.add([{ seq: 1 }, { seq: 2 }]), 2);
  assert.equal(log.add([{ seq: 2 }, { seq: 3 }, { seq: 4 }]), 2);
  assert.deepEqual(log.all().map((e) => e.seq), [2, 3, 4]);
});

test('clock and seconds formatting', () => {
  assert.equal(M.clock(272_000), '4:32');
  assert.equal(M.clock(1), '0:01');
  assert.equal(M.clock(-5), '0:00');
  assert.equal(M.seconds(109_194), '109.2 s');
});

test('resident view counts down to departure', () => {
  const state = { status: 'live', region: 'eu-west-1', hop_id: 'h-000267', phase: 'RESIDENT', arrived_at: iso(T0 - 200_000), departure_at: iso(T0 + 27_000) };
  const v = M.view({ state, regions: REGIONS, now: T0 });
  assert.equal(v.mode, 'resident');
  assert.equal(v.residence.near, 'near Dublin, Ireland');
  assert.equal(v.residence.leavesInMs, 27_000);
  assert.equal(v.residence.tz, 'Europe/Dublin');
});

test('packing shows the destination and a live countdown', () => {
  const state = { status: 'live', region: 'eu-west-1', hop_id: 'h-000267', phase: 'PACKING', next_region: 'ap-northeast-3', departure_at: iso(T0 + 20_000) };
  const events = [{ seq: 9, type: 'PACKING_STARTED', hop_id: 'h-000267', region: 'eu-west-1', destination: 'ap-northeast-3', ts: iso(T0 - 10_000) }];
  const v = M.view({ state, events, regions: REGIONS, now: T0 });
  assert.equal(v.mode, 'packing');
  assert.equal(v.feed[0].text, 'Packing our suitcase. Next stop: near Osaka, Japan.');
  assert.equal(v.feed[0].departAt, T0 + 20_000);
  // Once departed, the packing line stops counting.
  const after = M.view({ state: { ...state, phase: 'DEPARTED' }, events, regions: REGIONS, now: T0 + 21_000 });
  assert.equal(after.feed.find((i) => i.kind === 'packing').departAt, null);
});

test('flight view follows phase events and lingers while the edge settles', () => {
  const state = { status: 'live', region: 'eu-west-1', hop_id: 'h-000267', phase: 'ROUTE_FLIPPED', next_region: 'ap-northeast-3' };
  const events = [phase(10, 'DEPARTED', 0), phase(11, 'FUNCTION_CREATED', 2.3), phase(12, 'FUNCTION_ACTIVE', 4.5), phase(13, 'URL_READY', 6.4), phase(14, 'HEALTH_OK', 7.5), phase(15, 'ROUTE_FLIPPED', 10.6)];
  const v = M.view({ state, events, regions: REGIONS, now: T0 + 50_000 });
  assert.equal(v.mode, 'flight');
  assert.equal(v.flight.from, 'eu-west-1');
  assert.equal(v.flight.to, 'ap-northeast-3');
  assert.equal(v.flight.startedAt, T0);
  assert.equal(v.flight.progress, M.PHASES.find((p) => p.name === 'ROUTE_FLIPPED').at);
  assert.equal(v.feed[0].text, 'Took off from Dublin, bound for Osaka.');
  // Forty seconds later with no new event, the plane has not moved.
  assert.equal(M.view({ state, events, regions: REGIONS, now: T0 + 90_000 }).flight.progress, v.flight.progress);
});

test('a page opened mid-flight takes the early phases from the hop record', () => {
  const state = { status: 'live', region: 'eu-west-1', phase: 'ROUTE_CONFIRMED', next_region: 'ap-northeast-3' };
  const hops = [{ hop_id: 'h-000268', status: 'in_flight', from: 'eu-west-1', to: 'ap-northeast-3', started_at: iso(T0), phases: { DEPARTED: iso(T0), HEALTH_OK: iso(T0 + 7500) } }];
  const f = M.currentFlight(state, [phase(30, 'ROUTE_CONFIRMED', 101)], hops);
  assert.ok(f.done.DEPARTED && f.done.HEALTH_OK && f.done.ROUTE_CONFIRMED);
  assert.equal(f.startedAt, T0);
});

test('landing and cancelled departures read plainly; raw reasons never show', () => {
  const events = [
    { seq: 20, type: 'LANDED', hop_id: 'h-1', region: 'ap-northeast-3', origin: 'eu-west-1', flight_ms: 109_194, ts: iso(T0) },
    { seq: 21, type: 'HOP_ABORTED', hop_id: 'h-2', region: 'ap-northeast-3', destination: 'eu-west-1', phase: 'ROUTE_FLIPPED', reason: "route writer refused: {'errorMessage': 'Pre-Condition failed', 'stackTrace': [...]}", ts: iso(T0 + 1) },
    { seq: 22, type: 'RESTING', hop_id: 'h-2', region: 'ap-northeast-3', reason: 'auto-sleep after 6 h awake', ts: iso(T0 + 2) },
    { seq: 23, type: 'INVARIANT_VIOLATION', region: 'us-east-2', reason: '2 copies', ts: iso(T0 + 3) },
  ];
  const feed = M.view({ state: { status: 'resting' }, events, regions: REGIONS, now: T0 }).feed;
  assert.deepEqual(
    feed.map((i) => i.text),
    [
      'Resting near Osaka, Japan: a scheduled rest after 6 hours awake.',
      'Departure cancelled, staying near Osaka, Japan: the edge routing could not be switched.',
      'Landed near Osaka, Japan after 109.2 s.',
    ],
  );
  assert.ok(!feed.some((i) => /stack|Pre-Condition|copies/.test(i.text)));
});

test('statements keep their source label and link, newest first', () => {
  const events = [
    { seq: 1, type: 'STATEMENT', text: 'First.', source_name: 'MET Norway', source_url: 'https://api.met.no/', ts: iso(T0) },
    { seq: 2, type: 'STATEMENT', text: 'Second.', source_name: 'Computed', source_url: '', ts: iso(T0 + 30_000) },
  ];
  const feed = M.view({ state: { status: 'live', phase: 'RESIDENT' }, events, regions: REGIONS, now: T0 }).feed;
  assert.deepEqual(feed.map((i) => [i.text, i.source, i.url]), [['Second.', 'Computed', ''], ['First.', 'MET Norway', 'https://api.met.no/']]);
});

test('stats from the hop sample, trail and the last journey', () => {
  const at = (min) => iso(T0 + min * 60_000);
  const hops = [
    { hop_id: 'h-5', status: 'landed', from: 'ap-northeast-3', to: 'eu-west-1', flight_ms: 100_000, started_at: at(10), landed_at: at(12) },
    { hop_id: 'h-4', status: 'aborted', from: 'ap-northeast-3', to: 'us-west-2', started_at: at(6) },
    { hop_id: 'h-3', status: 'landed', from: 'eu-west-1', to: 'ap-northeast-3', flight_ms: 120_000, started_at: at(2), landed_at: at(4) },
    { hop_id: 'h-2', status: 'landed', from: 'us-west-2', to: 'eu-west-1', flight_ms: 90_000, started_at: at(-100), landed_at: at(-98) },
  ];
  const st = M.stats({ hops_ok: 250, hops_aborted: 11 }, hops);
  assert.equal(st.hops, 250);
  assert.equal(st.aborted, 11);
  assert.equal(st.regions, 3);
  assert.equal(st.avgFlightMs, 103_333.33333333333);
  assert.equal(st.longestFlightMs, 120_000);
  assert.equal(st.sample, 4);
  assert.deepEqual(M.trail(hops).map((t) => t.hopId), ['h-5', 'h-3', 'h-2']);
  const j = M.lastJourney(hops);
  assert.equal(j.hops, 2, 'the hop 100 minutes earlier belongs to an earlier journey');
  assert.equal(j.endedIn, 'eu-west-1');
  // Tower-side stats, when the feed has them, win.
  assert.deepEqual(M.stats({}, hops, { hops: 1 }), { hops: 1 });
});

test('move state: one copy at rest, two while moving, one again after landing', () => {
  const at = M.moveState({ here: 'eu-west-1', next: '', regions: REGIONS });
  assert.equal(at.copies, 1);
  assert.equal(at.old.status, 'serving');
  assert.equal(at.new.status, 'none');
  assert.match(at.edge.text, /Dublin/);

  const done = {};
  const flight = { from: 'eu-west-1', to: 'ap-northeast-3', done };
  const states = [];
  for (const [i, p] of M.PHASES.entries()) {
    done[p.name] = { ts: iso(T0 + i * 10_000) };
    states.push(M.moveState({ flight, regions: REGIONS, now: T0 + i * 10_000 + 5000 }));
  }
  assert.deepEqual(states.map((s) => s.copies), [1, 2, 2, 2, 2, 2, 2, 2, 1]);
  assert.equal(states.at(-1).old.status, 'gone');
  assert.equal(states.at(-1).new.status, 'serving');
  // While the edge settles it reports progress toward the usual 90 s.
  const switching = states[M.PHASES.findIndex((p) => p.name === 'ROUTE_FLIPPED')];
  assert.equal(switching.edge.status, 'switching');
  assert.ok(switching.edge.settle.fraction > 0 && switching.edge.settle.fraction < 0.1);
  assert.match(states.at(-1).edge.text, /Osaka/);
});

test('step explanations name the cities', () => {
  const build = M.PHASES.find((p) => p.name === 'FUNCTION_CREATED');
  assert.equal(M.explain(build, 'eu-west-1', 'ap-northeast-3', REGIONS), 'Copies its own code into a brand-new Lambda function in Osaka.');
  const last = M.PHASES.at(-1);
  assert.match(M.explain(last, 'eu-west-1', 'ap-northeast-3', REGIONS), /behind in Dublin/);
  assert.match(M.explain(last, '', '', {}), /the old region/);
});

test('last move and journey rows come from hop records', () => {
  const phases = { DEPARTED: iso(T0), HEALTH_OK: iso(T0 + 6000), ROUTE_FLIPPED: iso(T0 + 9000), ROUTE_CONFIRMED: iso(T0 + 100_000), LANDED: iso(T0 + 109_000) };
  const hops = [
    { hop_id: 'h-9', status: 'in_flight', from: 'ap-northeast-3', to: 'us-west-2', started_at: iso(T0 + 400_000), phases: { DEPARTED: iso(T0 + 400_000) } },
    { hop_id: 'h-8', status: 'aborted', from: 'eu-west-1', to: 'us-west-2', abort_phase: 'HEALTH_OK', started_at: iso(T0 - 300_000), aborted_at: iso(T0 - 290_000), phases: {} },
    { hop_id: 'h-7', status: 'landed', from: 'eu-west-1', to: 'ap-northeast-3', started_at: iso(T0), landed_at: iso(T0 + 109_000), flight_ms: 109_000, phases },
  ];
  const last = M.lastMove(hops, 'ap-northeast-3');
  assert.equal(last.hopId, 'h-7');
  assert.equal(last.times.HEALTH_OK, 6000);
  assert.equal(last.landedAt, T0 + 109_000);
  assert.equal(M.lastMove(hops, 'us-west-2'), null);

  const rows = M.journeyRows(hops);
  assert.deepEqual(rows.map((r) => r.outcome), ['in flight', 'cancelled', 'landed']);
  assert.equal(rows[1].abortPhase, 'HEALTH_OK');
  assert.equal(rows[2].buildMs, 6000);
  assert.equal(rows[2].edgeMs, 91_000);
  assert.equal(rows[2].totalMs, 109_000);
});

test('durations read naturally', () => {
  assert.equal(M.duration(38_000), '38 s');
  assert.equal(M.duration(4 * 60_000 + 5000), '4 min');
  assert.equal(M.duration((3 * 60 + 12) * 60_000), '3 h 12 min');
  assert.equal(M.duration(53 * 3_600_000), '2 days 5 h');
});

test('latency samples find the moment the answering region changed', () => {
  const l = new M.Latency(60_000);
  l.add(T0, 120, 'eu-west-1');
  l.add(T0 + 5000, 118, 'eu-west-1');
  l.add(T0 + 6000, 260, 'ap-northeast-3');
  l.add(T0 + 7000, 250, 'ap-northeast-3');
  l.add(T0 + 8000, 270, 'ap-northeast-3');
  assert.deepEqual(l.switches(), [{ t: T0 + 6000, from: 'eu-west-1', to: 'ap-northeast-3' }]);
  assert.deepEqual(l.medians().map((m) => [m.region, m.ms]), [['eu-west-1', 120], ['ap-northeast-3', 260]]);
  l.add(T0 + 70_000, 255, 'ap-northeast-3');
  assert.deepEqual(l.samples.map((x) => x.t), [T0 + 70_000], 'samples older than the window fall out');
});
