// The page: polls the tower and the traveler (D7) and draws what model.js derives.
import * as M from './model.js';

const DATA = JSON.parse(document.getElementById('map-data').textContent);
const REGIONS = Object.fromEntries(DATA.regions.map((r) => [r.code, r]));
const SVG = 'http://www.w3.org/2000/svg';
const $ = (id) => document.getElementById(id);

const serverClock = new M.ServerClock();
const log = new M.EventLog();
let state = {};
let hops = [];
let towerStats = null;
let feedOk = null; // null until the first answer
let current = null; // last view
let shown = 0; // plane progress on screen, eases toward the real one
let lastFrame = performance.now();

// --- tiny DOM helpers -------------------------------------------------------------

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('data-')) el.setAttribute(k, v);
    else el[k] = v;
  }
  el.append(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
  return el;
}

function s(tag, attrs = {}) {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

const timeFmt = new Intl.DateTimeFormat([], { hour: '2-digit', minute: '2-digit' });
const zoneFmts = {};
function localTime(ms, tz) {
  zoneFmts[tz] ||= new Intl.DateTimeFormat([], { hour: '2-digit', minute: '2-digit', weekday: 'short', timeZone: tz });
  return zoneFmts[tz].format(ms);
}

// --- fetching ---------------------------------------------------------------------

async function getJson(url) {
  const sent = Date.now();
  const t0 = performance.now();
  const res = await fetch(url, { cache: 'no-store' });
  const rtt = performance.now() - t0;
  const received = Date.now();
  const body = await res.json().catch(() => ({}));
  return { res, body, rtt, sent, received };
}

async function pollFeed() {
  try {
    const { res, body, received } = await getJson('/api/tower/feed');
    serverClock.addDateHeader(res.headers.get('date'), received);
    if (!res.ok) throw new Error(String(res.status));
    feedOk = true;
    state = body.state || {};
    towerStats = body.stats || null;
    const events = body.events || [];
    const fresh = events.filter((e) => !log.bySeq.has(e.seq));
    log.add(events);
    if (!hops.length || fresh.some((e) => ['LANDED', 'HOP_ABORTED', 'ARRIVAL_STARTED', 'RESTING', 'WOKE'].includes(e.type))) {
      pollHops();
    }
  } catch {
    feedOk = false;
  }
  render();
}

async function pollHops() {
  try {
    const { res, body } = await getJson('/api/tower/hops?limit=100');
    if (res.ok) hops = body.hops || [];
    render();
  } catch {
    /* keep the last good trail */
  }
}

async function pollPing() {
  const el = $('latency');
  try {
    const { res, body, rtt, sent, received } = await getJson('/api/traveler/ping');
    if (!res.ok) {
      el.hidden = true;
      return;
    }
    if (body.now) serverClock.addPing(Date.parse(body.now), sent, received);
    else serverClock.addDateHeader(res.headers.get('date'), received);
    const region = res.headers.get('x-traveler-region') || body.region || '';
    el.textContent = `You are ${Math.round(rtt)} ms away${region ? ` from ${region}` : ''}`;
    el.hidden = false;
  } catch {
    el.hidden = true;
  }
}

// --- render: status, panel, feed, stats -------------------------------------------

const STATUS_TEXT = { resident: 'Live', packing: 'Packing', flight: 'In flight', resting: 'Resting', unknown: 'Connecting…' };

function render() {
  const now = serverClock.now();
  const v = M.view({ state, events: log.all(), hops, regions: REGIONS, now });
  if (feedOk === false && !state.status) v.mode = 'unknown';
  const pill = $('status');
  pill.textContent = feedOk === false ? 'Reconnecting…' : STATUS_TEXT[v.mode];
  pill.className = `pill ${v.mode === 'resident' ? 'live' : v.mode}`;
  renderPanel(v);
  renderFeed(v.feed);
  renderStats();
  renderMap(v);
  renderSummary(v, now);
  $('release').textContent = state.release ? `Release ${state.release}` : '';
  current = v;
}

let panelKey = '';
function renderPanel(v) {
  const f = v.flight;
  const key = [v.mode, state.hop_id, state.next_region, f?.hopId, f && Object.keys(f.done).join(','), hops.length && v.mode === 'resting'].join('|');
  if (key === panelKey) return;
  panelKey = key;
  const panel = $('panel');
  panel.className = `card panel ${v.mode}`;
  panel.setAttribute('aria-busy', 'false');
  const r = v.residence;
  let body;
  if (v.mode === 'flight') body = flightPanel(f);
  else if (v.mode === 'resting') body = restingPanel(r);
  else if (v.mode === 'unknown') {
    body = [h('p', { class: 'kicker' }, 'Connecting'), h('p', { class: 'place' }, 'Looking for the traveler…'), h('p', { class: 'hint' }, "Can't reach the control tower yet. Retrying every few seconds.")];
  } else {
    const stay = r.arrivedAt && r.departAt ? r.departAt - r.arrivedAt : null;
    body = [
      h('p', { class: 'kicker' }, v.mode === 'packing' ? 'Packing to leave' : 'Now staying'),
      h('p', { class: 'place' }, r.near),
      h('p', { class: 'code' }, r.region),
      h('p', { class: 'big-clock', 'data-tick': 'countdown', 'data-at': r.departAt || '' }, '–'),
      h('p', { class: 'big-clock-label' }, v.mode === 'packing' ? `until departure for ${M.regionLabel(REGIONS, r.nextRegion)}` : 'until the next departure'),
      stay ? h('div', { class: 'bar', 'aria-hidden': 'true' }, h('span', { 'data-tick': 'bar', 'data-from': r.arrivedAt, 'data-to': r.departAt })) : null,
      h(
        'dl',
        { class: 'facts' },
        h('dt', {}, 'Arrived'),
        h('dd', {}, r.arrivedAt ? timeFmt.format(r.arrivedAt) + ' your time' : '–'),
        h('dt', {}, 'Local time there'),
        h('dd', { 'data-tick': 'localtime', 'data-tz': r.tz }, '–'),
        r.nextRegion ? h('dt', {}, 'Next stop') : null,
        r.nextRegion ? h('dd', {}, M.regionLabel(REGIONS, r.nextRegion)) : null,
      ),
    ];
  }
  panel.replaceChildren(...[body].flat().filter(Boolean));
  tick();
}

function flightPanel(f) {
  const from = REGIONS[f.from], to = REGIONS[f.to];
  const km = from && to ? Math.round(M.distanceKm(from, to)) : null;
  const doneCount = M.PHASES.filter((p) => f.done[p.name]).length;
  const next = M.PHASES[doneCount];
  const items = M.PHASES.map((p, i) => {
    const d = f.done[p.name];
    const at = d && f.startedAt ? '+' + M.seconds(Date.parse(d.ts) - f.startedAt) : '';
    const cls = d ? 'done' : i === doneCount ? 'current' : '';
    return h('li', { class: cls }, h('span', { class: 'mark', 'aria-hidden': 'true' }), h('span', {}, p.label, d ? h('span', { class: 'sr-only' }, ', done') : null), h('time', {}, at));
  });
  let hint = null;
  if (f.landed) hint = `Landed ${M.regionLabel(REGIONS, f.to)}.`;
  else if (next?.name === 'ROUTE_CONFIRMED') hint = 'Waiting for edge locations around the world to pick up the new route. This is most of every flight, usually about 90 seconds. The old copy keeps serving until then.';
  return [
    h('p', { class: 'kicker' }, f.landed ? 'Landed' : 'In flight'),
    h('p', { class: 'place' }, `${from?.city || f.from} → ${to?.city || f.to}`),
    h('p', { class: 'code' }, `${f.from} → ${f.to}${km ? ` · ${km.toLocaleString()} km` : ''}`),
    h('p', { class: 'big-clock', 'data-tick': f.landed ? '' : 'elapsed', 'data-from': f.startedAt || '' }, f.landed && f.done.LANDED && f.startedAt ? M.seconds(Date.parse(f.done.LANDED.ts) - f.startedAt) : '–'),
    h('p', { class: 'big-clock-label' }, 'flight time'),
    h('ol', { class: 'checklist', 'aria-label': 'Flight steps' }, items),
    hint ? h('p', { class: 'hint' }, hint) : null,
  ];
}

function restingPanel(r) {
  const j = M.lastJourney(hops);
  return [
    h('p', { class: 'kicker' }, 'Resting'),
    h('p', { class: 'place' }, 'The traveler is asleep'),
    h('p', { class: 'code' }, r.region ? `Last seen ${r.near}` : ''),
    j
      ? h(
          'p',
          { class: 'hint' },
          `Its last journey: ${j.hops} ${j.hops === 1 ? 'hop' : 'hops'} across ${j.regions} regions, ending ${M.regionLabel(REGIONS, j.endedIn)} at ${timeFmt.format(j.endedAt)} your time.`,
        )
      : null,
    h('p', { class: 'hint' }, 'It wakes up on its own schedule. This page will notice.'),
  ];
}

let feedSeqs = '';
let feedSeen = null;
function renderFeed(items) {
  const top = items.slice(0, 40);
  const seqs = top.map((i) => i.seq + (i.departAt ? 'c' : '')).join(',');
  if (seqs === feedSeqs) return;
  feedSeqs = seqs;
  const ol = $('feed');
  if (!top.length) {
    ol.replaceChildren(h('li', { class: 'empty' }, 'Statements appear here about every 30 seconds.'));
    return;
  }
  ol.replaceChildren(
    ...top.map((i) => {
      const cls = [i.kind === 'statement' ? '' : 'event', i.kind, feedSeen && !feedSeen.has(i.seq) ? 'new' : ''].filter(Boolean).join(' ');
      return h(
        'li',
        { class: cls },
        h('time', { dateTime: new Date(i.ts).toISOString() }, timeFmt.format(i.ts)),
        h('p', { class: 'text' }, i.text),
        i.source ? h('span', { class: 'source' }, 'Source: ', i.url ? h('a', { href: i.url, rel: 'noopener', target: '_blank' }, i.source) : i.source) : null,
        i.departAt ? h('span', { class: 'countdown', 'data-tick': 'countdown-line', 'data-at': i.departAt }, '') : null,
      );
    }),
  );
  feedSeen = new Set(top.map((i) => i.seq));
  tick();
}

function renderStats() {
  const st = M.stats(state, hops, towerStats);
  $('st-hops').textContent = st.hops?.toLocaleString() ?? '–';
  $('st-regions').textContent = st.regions ? String(st.regions) : '–';
  $('st-avg').textContent = st.avgFlightMs ? M.seconds(st.avgFlightMs) : '–';
  $('st-long').textContent = st.longestFlightMs ? M.seconds(st.longestFlightMs) : '–';
  $('st-aborted').textContent = st.aborted?.toLocaleString() ?? '–';
  const note = $('st-note');
  note.hidden = !st.sample;
  note.textContent = st.sample ? `Regions and flight times cover the last ${st.sample} hops.` : '';
}

let lastSummary = '';
function renderSummary(v, now) {
  let text;
  const r = v.residence;
  if (v.mode === 'flight') {
    const next = M.PHASES.find((p) => !v.flight.done[p.name]);
    text = `The traveler is in flight from ${M.regionLabel(REGIONS, v.flight.from)} to ${M.regionLabel(REGIONS, v.flight.to)}. ${next ? `Current step: ${next.label}.` : 'Landed.'}`;
  } else if (v.mode === 'resting') {
    text = 'The traveler is resting.';
  } else if (v.mode === 'unknown') {
    text = 'Finding the traveler.';
  } else {
    const mins = r.departAt ? Math.max(0, Math.round((r.departAt - now) / 60000)) : null;
    const when = mins === null ? '' : mins < 1 ? ' Departure in under a minute.' : ` Next departure in about ${mins} ${mins === 1 ? 'minute' : 'minutes'}.`;
    const dest = r.nextRegion ? ` Next stop: ${M.regionLabel(REGIONS, r.nextRegion)}.` : '';
    text = `The traveler is ${r.near} (${r.region}). Status: ${v.mode === 'packing' ? 'packing' : 'staying'}.${when}${dest}`;
  }
  if (text !== lastSummary) $('map-summary').textContent = lastSummary = text;
}

// --- map --------------------------------------------------------------------------

const map = $('map');
let unit = 1; // map units per rendered pixel, so marks stay readable on a phone
let mapKey = '';
let routePoints = null;

function measure() {
  const w = map.getBoundingClientRect().width || DATA.width;
  unit = Math.max(1, (DATA.width / w) * 0.75);
  mapKey = '';
}

function renderMap(v) {
  const here = v.mode === 'flight' ? (v.flight.landed ? v.flight.to : v.flight.from) : v.residence.region;
  const next = v.mode === 'flight' ? (v.flight.landed ? '' : v.flight.to) : v.residence.nextRegion;
  const tr = M.trail(hops);
  const key = [here, next, unit, tr.map((t) => t.hopId).join(','), v.mode].join('|');
  if (key === mapKey) return;
  mapKey = key;

  const visited = new Set(tr.flatMap((t) => [t.from, t.to]));
  const trailG = $('trail');
  trailG.replaceChildren(
    ...tr
      .filter((t) => REGIONS[t.from] && REGIONS[t.to])
      .map((t, i) => s('path', { class: 'trail-arc', d: M.pathData(M.greatCircle(REGIONS[t.from], REGIONS[t.to], 48)), opacity: String(0.6 - i * 0.06), 'stroke-width': String(1.4 * unit) })),
  );

  const pins = $('pins');
  const marks = [];
  for (const r of DATA.regions) {
    const cls = r.code === here ? 'pin here' : visited.has(r.code) ? 'pin visited' : 'pin';
    const c = s('circle', { class: cls, cx: r.x, cy: r.y, r: String((r.code === here ? 4.5 : 3) * unit) });
    const title = s('title');
    title.textContent = `${r.code} · ${r.near}`;
    c.append(title);
    marks.push(c);
  }
  const label = (code, text) => {
    const r = REGIONS[code];
    if (!r) return;
    const right = r.x > DATA.width - 140;
    const t = s('text', { class: 'pin-label', x: String(r.x + (right ? -9 : 9) * unit), y: String(r.y - 7 * unit), 'text-anchor': right ? 'end' : 'start' });
    t.style.fontSize = `${13 * unit}px`;
    t.style.strokeWidth = `${4 * unit}px`;
    t.textContent = text;
    marks.push(t);
  };
  if (REGIONS[here] && v.mode !== 'resting') {
    const p = REGIONS[here];
    marks.unshift(s('circle', { class: 'pin-pulse', cx: p.x, cy: p.y, r: String(4.5 * unit) }));
  }
  if (next && REGIONS[next]) {
    marks.push(s('circle', { class: 'pin next', cx: REGIONS[next].x, cy: REGIONS[next].y, r: String(6 * unit), 'stroke-width': String(2 * unit) }));
    label(next, REGIONS[next].city);
  }
  if (REGIONS[here]) label(here, REGIONS[here].city);
  pins.replaceChildren(...marks);

  const route = $('route');
  routePoints = null;
  route.replaceChildren();
  if (next && REGIONS[here] && REGIONS[next]) {
    const from = v.mode === 'flight' ? v.flight.from : here;
    routePoints = M.greatCircle(REGIONS[from], REGIONS[next]);
    route.append(
      s('path', { class: 'route-arc', d: M.pathData(routePoints), 'stroke-width': String(2 * unit), 'stroke-dasharray': `${5 * unit} ${6 * unit}` }),
      s('path', { class: 'route-done', id: 'route-done', d: '', 'stroke-width': String(2.5 * unit) }),
    );
  }
  if (v.mode !== 'flight') shown = 0;
}

function drawPlane(dt) {
  const plane = $('plane');
  const f = current?.mode === 'flight' ? current.flight : null;
  if (!f || !routePoints || f.landed) {
    plane.style.display = 'none';
    return;
  }
  shown = M.ease(shown, f.progress, dt);
  const { x, y, heading } = M.along(routePoints, shown);
  plane.setAttribute('transform', `translate(${x.toFixed(1)} ${y.toFixed(1)}) rotate(${heading.toFixed(1)}) scale(${unit.toFixed(2)})`);
  plane.style.display = '';
  const done = $('route-done');
  if (done) {
    const n = Math.max(2, Math.round(shown * (routePoints.length - 1)) + 1);
    done.setAttribute('d', M.pathData(routePoints.slice(0, n)));
  }
}

// --- per-frame ticking (clocks, countdowns, the plane) -----------------------------

function tick() {
  const now = serverClock.now();
  for (const el of document.querySelectorAll('[data-tick]')) {
    const kind = el.dataset.tick;
    let text = null;
    if (kind === 'countdown' || kind === 'countdown-line') {
      const at = Number(el.dataset.at);
      if (!at) text = '–';
      else {
        const left = at - now;
        text = left > 0 ? M.clock(left) : 'Leaving now';
        if (kind === 'countdown-line') text = left > 0 ? `Leaving in ${M.clock(left)}` : 'Leaving now';
      }
    } else if (kind === 'localtime') {
      text = localTime(now, el.dataset.tz);
    } else if (kind === 'elapsed') {
      const from = Number(el.dataset.from);
      text = from ? M.seconds(Math.max(0, now - from)) : '–';
    } else if (kind === 'bar') {
      const a = Number(el.dataset.from), b = Number(el.dataset.to);
      el.style.width = `${Math.min(100, Math.max(0, ((now - a) / (b - a)) * 100)).toFixed(2)}%`;
    }
    if (text !== null && el.textContent !== text) el.textContent = text;
  }
}

function frame(t) {
  const dt = t - lastFrame;
  lastFrame = t;
  if (!document.hidden) {
    tick();
    drawPlane(dt);
  }
  requestAnimationFrame(frame);
}

// --- schedule (D7: 1 s in flight, 2 s otherwise, nothing while hidden) ------------

let feedTimer = 0;
async function feedLoop() {
  clearTimeout(feedTimer);
  if (document.hidden) return;
  await pollFeed();
  const urgent = current?.mode === 'flight' || (current?.mode === 'packing' && current.residence.leavesInMs < 5000);
  feedTimer = setTimeout(feedLoop, urgent ? 1000 : 2000);
}

let pingTimer = 0;
async function pingLoop() {
  clearTimeout(pingTimer);
  if (document.hidden) return;
  await pollPing();
  pingTimer = setTimeout(pingLoop, 5000);
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    feedLoop();
    pingLoop();
  }
});
window.addEventListener('resize', () => {
  measure();
  if (current) renderMap(current);
});

measure();
feedLoop();
pingLoop();
pollHops();
requestAnimationFrame(frame);
