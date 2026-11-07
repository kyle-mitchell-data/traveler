// Pure page logic: projection, flight paths, the server clock and the view model.
// No DOM here, so `node --test site/` covers it (site/model.test.js).

// --- map projection ---------------------------------------------------------------
// Equal Earth, scaled to a 1000-unit-wide map, 84°N to 58°S. Must match
// scripts/site_build.py, which draws the land and places the pins.

const A1 = 1.340264, A2 = -0.081106, A3 = 0.000893, A4 = 0.003796, M = Math.sqrt(3) / 2;
const RAD = Math.PI / 180;
export const WIDTH = 1000;
const TOP_LAT = 84;
const BOTTOM_LAT = -58;

function raw(lon, lat) {
  const t = Math.asin(M * Math.sin(lat * RAD));
  const t2 = t * t, t6 = t2 * t2 * t2;
  const x = (2 * Math.sqrt(3) * lon * RAD * Math.cos(t)) / (3 * (9 * A4 * t6 * t2 + 7 * A3 * t6 + 3 * A2 * t2 + A1));
  const y = t * (A1 + A2 * t2 + t6 * (A3 + A4 * t2));
  return [x, y];
}

const SCALE = WIDTH / (2 * raw(180, 0)[0]);
const TOP = raw(0, TOP_LAT)[1];

export function project(lon, lat) {
  // Routes over the poles run along the map's edge rather than off it.
  const [x, y] = raw(lon, Math.min(TOP_LAT - 1, Math.max(BOTTOM_LAT + 1, lat)));
  return [WIDTH / 2 + SCALE * x, SCALE * (TOP - y)];
}

// --- great circles -----------------------------------------------------------------

function toVec(lon, lat) {
  const p = lat * RAD, l = lon * RAD;
  return [Math.cos(p) * Math.cos(l), Math.cos(p) * Math.sin(l), Math.sin(p)];
}

function toLonLat([x, y, z]) {
  return [Math.atan2(y, x) / RAD, Math.atan2(z, Math.hypot(x, y)) / RAD];
}

/** Points along the great circle from a to b ({lon, lat}), evenly spaced, ends included. */
export function greatCircle(a, b, n = 96) {
  const va = toVec(a.lon, a.lat), vb = toVec(b.lon, b.lat);
  const dot = Math.min(1, Math.max(-1, va[0] * vb[0] + va[1] * vb[1] + va[2] * vb[2]));
  const omega = Math.acos(dot);
  if (omega < 1e-9) return [[a.lon, a.lat], [b.lon, b.lat]];
  const out = [];
  for (let i = 0; i <= n; i++) {
    const t = i / n;
    const s1 = Math.sin((1 - t) * omega) / Math.sin(omega), s2 = Math.sin(t * omega) / Math.sin(omega);
    out.push(toLonLat([s1 * va[0] + s2 * vb[0], s1 * va[1] + s2 * vb[1], s1 * va[2] + s2 * vb[2]]));
  }
  return out;
}

/** Great-circle distance in km. */
export function distanceKm(a, b) {
  const va = toVec(a.lon, a.lat), vb = toVec(b.lon, b.lat);
  const dot = Math.min(1, Math.max(-1, va[0] * vb[0] + va[1] * vb[1] + va[2] * vb[2]));
  return Math.acos(dot) * 6371;
}

/** SVG path data for a list of [lon, lat] points, split where it crosses the antimeridian. */
export function pathData(points) {
  const segs = [];
  let seg = [];
  for (let i = 0; i < points.length; i++) {
    const [lon, lat] = points[i];
    if (i > 0) {
      const [plon, plat] = points[i - 1];
      if (Math.abs(lon - plon) > 180) {
        // Cross at the map edge: finish this side, start again on the other.
        const edge = plon > 0 ? 180 : -180;
        const unwrapped = lon + (edge > 0 ? 360 : -360);
        const f = (edge - plon) / (unwrapped - plon);
        const clat = plat + (lat - plat) * f;
        seg.push(project(edge, clat));
        segs.push(seg);
        seg = [project(-edge, clat)];
      }
    }
    seg.push(project(lon, lat));
  }
  segs.push(seg);
  return segs
    .filter((s) => s.length > 1)
    .map((s) => 'M' + s.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join('L'))
    .join('');
}

/** Map position and heading (degrees, SVG rotation) at fraction t of a point list. */
export function along(points, t) {
  const f = Math.min(1, Math.max(0, t)) * (points.length - 1);
  const i = Math.min(points.length - 2, Math.floor(f));
  const k = f - i;
  const [lon1, lat1] = points[i], [lon2, lat2] = points[i + 1];
  let dlon = lon2 - lon1;
  if (dlon > 180) dlon -= 360;
  if (dlon < -180) dlon += 360;
  let lon = lon1 + dlon * k;
  if (lon > 180) lon -= 360;
  if (lon < -180) lon += 360;
  const [x, y] = project(lon, lat1 + (lat2 - lat1) * k);
  // Heading from a short step in lon/lat space, projected locally (no wrap jump).
  const [x2, y2] = project(lon + dlon * 0.01, lat1 + (lat2 - lat1) * (k + 0.01));
  const heading = Math.atan2(y2 - y, x2 - x) / RAD;
  return { x, y, heading };
}

// --- flight phases -----------------------------------------------------------------

// In order. `at` is where the plane sits once that phase is done: most of a flight
// is the edge settling (D21), so the plane waits at ROUTE_FLIPPED until the edge
// confirms. Progress only ever moves on a real phase event.
export const PHASES = [
  { name: 'DEPARTED', label: 'Left the old region', at: 0.04 },
  { name: 'FUNCTION_CREATED', label: 'New copy created', at: 0.12 },
  { name: 'FUNCTION_ACTIVE', label: 'New copy running', at: 0.2 },
  { name: 'URL_READY', label: 'Address ready', at: 0.27 },
  { name: 'HEALTH_OK', label: 'Health check passed', at: 0.34 },
  { name: 'ROUTE_FLIPPED', label: 'Edge routing switched', at: 0.42 },
  { name: 'ROUTE_CONFIRMED', label: 'Edge settled on the new region', at: 0.86 },
  { name: 'ARRIVED', label: 'Suitcase delivered', at: 0.95 },
  { name: 'LANDED', label: 'Old copy retired', at: 1 },
];
const PHASE_INDEX = Object.fromEntries(PHASES.map((p, i) => [p.name, i]));
const IN_FLIGHT = new Set(PHASES.map((p) => p.name));

export function progressFor(done) {
  let at = 0;
  for (const name of Object.keys(done)) if (name in PHASE_INDEX) at = Math.max(at, PHASES[PHASE_INDEX[name]].at);
  return at;
}

/** Move a displayed value toward its target; the plane glides instead of jumping. */
export function ease(current, target, dtMs, tauMs = 450) {
  if (target <= current) return target;
  const next = current + (target - current) * (1 - Math.exp(-dtMs / tauMs));
  return target - next < 0.0005 ? target : next;
}

// Plain-language reasons for a cancelled departure, by the phase that failed.
// The tower's raw reason is internal and never shown.
const ABORT_REASONS = {
  DEPARTED: 'it could not get ready to leave',
  FUNCTION_CREATED: 'the new copy could not be created',
  FUNCTION_ACTIVE: 'the new copy did not start',
  URL_READY: 'the new copy did not get an address',
  HEALTH_OK: 'the new copy failed its health check',
  ROUTE_FLIPPED: 'the edge routing could not be switched',
  ROUTE_CONFIRMED: 'the edge did not settle in time',
  ARRIVED: 'the new copy never checked in',
};

export function abortReason(phase) {
  return ABORT_REASONS[phase] || 'something went wrong on the way';
}

export function restReason(reason = '') {
  if (reason.startsWith('auto-sleep')) {
    const h = reason.match(/(\d+) h/);
    return h ? `a scheduled rest after ${h[1]} hours awake` : 'a scheduled rest';
  }
  if (reason === 'kill switch') return 'stopped by the operator';
  if (reason.startsWith('reached max_hops')) return 'the planned journey is complete';
  if (reason === 'nowhere else to go') return 'there was nowhere left to go';
  return '';
}

// --- server clock ------------------------------------------------------------------

/**
 * Offset between this browser's clock and the server's, from timed samples.
 * A ping that reports the server time gives offset = server + rtt/2 - received;
 * the sample with the smallest round trip wins. Without one, fall back to the
 * HTTP Date header (1-second resolution, so +0.5 s on average).
 */
export class ServerClock {
  constructor() {
    this.samples = [];
    this.dateOffset = null;
  }

  addPing(serverMs, sentMs, receivedMs) {
    const rtt = receivedMs - sentMs;
    if (!(serverMs > 0) || rtt < 0) return;
    this.samples.push({ rtt, offset: serverMs + rtt / 2 - receivedMs });
    if (this.samples.length > 8) this.samples.shift();
  }

  addDateHeader(header, receivedMs) {
    const t = Date.parse(header || '');
    if (Number.isNaN(t)) return;
    const offset = t + 500 - receivedMs;
    // Cached responses carry an older Date; the largest offset is the freshest.
    this.dateOffset = this.dateOffset === null ? offset : Math.max(this.dateOffset, offset);
  }

  get offset() {
    if (this.samples.length) return this.samples.reduce((a, b) => (b.rtt < a.rtt ? b : a)).offset;
    return this.dateOffset ?? 0;
  }

  get precise() {
    return this.samples.length > 0;
  }

  now(local = Date.now()) {
    return local + this.offset;
  }
}

// --- events ------------------------------------------------------------------------

/** Every event seen since the page opened, by sequence number (the feed holds only ~20). */
export class EventLog {
  constructor(limit = 300) {
    this.limit = limit;
    this.bySeq = new Map();
  }

  add(events = []) {
    let fresh = 0;
    for (const e of events) {
      if (typeof e.seq !== 'number' || this.bySeq.has(e.seq)) continue;
      this.bySeq.set(e.seq, e);
      fresh++;
    }
    if (this.bySeq.size > this.limit) {
      const drop = [...this.bySeq.keys()].sort((a, b) => a - b).slice(0, this.bySeq.size - this.limit);
      for (const k of drop) this.bySeq.delete(k);
    }
    return fresh;
  }

  all() {
    return [...this.bySeq.values()].sort((a, b) => a.seq - b.seq);
  }
}

// --- formatting --------------------------------------------------------------------

export function clock(ms) {
  const s = Math.max(0, Math.ceil(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

export function seconds(ms) {
  return `${(ms / 1000).toFixed(1)} s`;
}

export function regionLabel(regions, code) {
  const r = regions[code];
  return r ? r.near : code ? `in ${code}` : 'somewhere';
}

function cityOf(regions, code) {
  return regions[code]?.city || code;
}

// --- view model --------------------------------------------------------------------

/** The current flight, from PHASE events and (if the page opened mid-flight) the hop record. */
export function currentFlight(state, events, hops = []) {
  if (!IN_FLIGHT.has(state.phase || '')) return null;
  const phaseEvents = events.filter((e) => e.type === 'PHASE');
  const last = phaseEvents[phaseEvents.length - 1];
  const hopId = last && last.destination === state.next_region ? last.hop_id : hops[0]?.status === 'in_flight' ? hops[0].hop_id : null;
  if (!hopId) return null;
  const done = {};
  const record = hops.find((h) => h.hop_id === hopId);
  for (const [name, ts] of Object.entries(record?.phases || {})) done[name] = { ts };
  for (const e of phaseEvents) if (e.hop_id === hopId) done[e.phase] = { ts: e.ts, elapsed_ms: e.elapsed_ms };
  const startedAt = Date.parse(done.DEPARTED?.ts || record?.started_at || '') || null;
  return {
    hopId,
    from: last?.hop_id === hopId ? last.origin : record?.from || state.region,
    to: last?.hop_id === hopId ? last.destination : record?.to || state.next_region,
    done,
    startedAt,
    progress: progressFor(done),
    landed: Boolean(done.LANDED),
  };
}

/**
 * What the page shows: mode (resident | packing | flight | resting | unknown), the
 * residence or flight details, and the feed (newest first).
 */
export function view({ state = {}, events = [], hops = [], regions = {}, now = Date.now() }) {
  const status = state.status || 'unknown';
  const flight = status === 'live' ? currentFlight(state, events, hops) : null;
  let mode = 'unknown';
  if (status === 'resting') mode = 'resting';
  else if (status === 'live') mode = flight ? 'flight' : state.phase === 'PACKING' ? 'packing' : 'resident';

  const departAt = Date.parse(state.departure_at || '') || null;
  const residence = {
    region: state.region || '',
    near: regionLabel(regions, state.region),
    arrivedAt: Date.parse(state.arrived_at || '') || null,
    departAt,
    leavesInMs: departAt ? departAt - now : null,
    nextRegion: state.next_region || '',
    tz: regions[state.region]?.tz || 'UTC',
  };
  return { mode, status, residence, flight, feed: feedItems(events, state, regions, now) };
}

export function feedItems(events, state, regions, now) {
  const items = [];
  const departAt = Date.parse(state.departure_at || '') || null;
  for (const e of events) {
    const ts = Date.parse(e.ts);
    const base = { seq: e.seq, ts, hopId: e.hop_id };
    switch (e.type) {
      case 'STATEMENT':
        items.push({ ...base, kind: 'statement', text: e.text, source: e.source_name, url: e.source_url, category: e.category });
        break;
      case 'PACKING_STARTED': {
        const live = state.phase === 'PACKING' && state.hop_id === e.hop_id && departAt && departAt > now;
        items.push({
          ...base,
          kind: 'packing',
          text: `Packing our suitcase. Next stop: ${regionLabel(regions, e.destination)}.`,
          departAt: live ? departAt : null,
        });
        break;
      }
      case 'PHASE':
        if (e.phase === 'DEPARTED') {
          items.push({ ...base, kind: 'flight', text: `Took off from ${cityOf(regions, e.origin)}, bound for ${cityOf(regions, e.destination)}.` });
        }
        break;
      case 'LANDED':
        items.push({
          ...base,
          kind: 'landed',
          text: `Landed ${regionLabel(regions, e.region)} after ${seconds(e.flight_ms || 0)}.`,
        });
        break;
      case 'HOP_ABORTED':
        items.push({
          ...base,
          kind: 'aborted',
          text: `Departure cancelled, staying ${regionLabel(regions, e.region)}: ${abortReason(e.phase)}.`,
        });
        break;
      case 'RESTING': {
        const why = restReason(e.reason);
        items.push({ ...base, kind: 'resting', text: `Resting${e.region ? ' ' + regionLabel(regions, e.region) : ''}${why ? `: ${why}` : ''}.` });
        break;
      }
      case 'WOKE':
        items.push({ ...base, kind: 'woke', text: `Woke up ${regionLabel(regions, e.region)}.` });
        break;
      case 'RESURRECTED':
        items.push({ ...base, kind: 'woke', text: `Back on its feet ${regionLabel(regions, e.region)}; the watchdog stepped in.` });
        break;
      default:
        break;
    }
  }
  return items.reverse();
}

// --- stats, trail and the last journey ------------------------------------------------

/** Landed hops, newest first, as {from, to} pairs for the map trail. */
export function trail(hops, n = 8) {
  return hops.filter((h) => h.status === 'landed' && h.from && h.to).slice(0, n).map((h) => ({ from: h.from, to: h.to, hopId: h.hop_id }));
}

export function stats(state = {}, hops = [], towerStats = null) {
  if (towerStats) return towerStats;
  const landed = hops.filter((h) => h.status === 'landed' && h.flight_ms);
  const flights = landed.map((h) => h.flight_ms);
  const visited = new Set(hops.filter((h) => h.status === 'landed').flatMap((h) => [h.from, h.to]).filter(Boolean));
  return {
    hops: state.hops_ok ?? landed.length,
    aborted: state.hops_aborted ?? hops.filter((h) => h.status === 'aborted').length,
    regions: visited.size,
    avgFlightMs: flights.length ? flights.reduce((a, b) => a + b, 0) / flights.length : null,
    longestFlightMs: flights.length ? Math.max(...flights) : null,
    sample: hops.length,
  };
}

/** The most recent run of landed hops with no gap over `gapMs`: the last journey. */
export function lastJourney(hops, gapMs = 20 * 60 * 1000) {
  const landed = hops.filter((h) => h.status === 'landed' && h.landed_at);
  if (!landed.length) return null;
  const run = [landed[0]];
  for (let i = 1; i < landed.length; i++) {
    const gap = Date.parse(run[run.length - 1].started_at || run[run.length - 1].landed_at) - Date.parse(landed[i].landed_at);
    if (gap > gapMs) break;
    run.push(landed[i]);
  }
  const regions = new Set(run.flatMap((h) => [h.from, h.to]));
  return {
    hops: run.length,
    regions: regions.size,
    endedIn: run[0].to,
    endedAt: Date.parse(run[0].landed_at),
    startedAt: Date.parse(run[run.length - 1].started_at || run[run.length - 1].landed_at),
  };
}
