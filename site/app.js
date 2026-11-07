// The page: polls the tower and the traveler (D7) and draws what model.js derives.
import * as M from './model.js';

const DATA = JSON.parse(document.getElementById('map-data').textContent);
const REGIONS = Object.fromEntries(DATA.regions.map((r) => [r.code, r]));
const SVG = 'http://www.w3.org/2000/svg';
const $ = (id) => document.getElementById(id);

const serverClock = new M.ServerClock();
const log = new M.EventLog();
const latency = new M.Latency();
let state = {};
let hops = [];
let towerStats = null;
let feedOk = null; // null until the first answer
let answeredBy = ''; // region that answered this browser's last ping
let current = null; // last view
let shown = 0; // plane progress on screen, eases toward the real one
let lastFrame = performance.now();

// --- tiny DOM helpers -----------------------------------------------------------------

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('data-') || k.startsWith('aria-')) el.setAttribute(k, v);
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
const secFmt = new Intl.DateTimeFormat([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const zoneFmts = {};
function localTime(ms, tz) {
  zoneFmts[tz] ||= new Intl.DateTimeFormat([], { hour: '2-digit', minute: '2-digit', weekday: 'short', timeZone: tz });
  return zoneFmts[tz].format(ms);
}
const city = (code) => REGIONS[code]?.city || code || '';
const near = (code) => M.regionLabel(REGIONS, code);
const tickEl = (kind, attrs = {}, tag = 'strong') => h(tag, { 'data-tick': kind, ...attrs }, '–');

// --- fetching ------------------------------------------------------------------------------

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
    const marks = ['LANDED', 'HOP_ABORTED', 'ARRIVAL_STARTED', 'RESTING', 'WOKE', 'PHASE'];
    if (!hops.length || fresh.some((e) => marks.includes(e.type))) pollHops();
  } catch {
    feedOk = false;
  }
  render();
}

let hopsBusy = false;
async function pollHops() {
  if (hopsBusy) return;
  hopsBusy = true;
  try {
    const { res, body } = await getJson('/api/tower/hops?limit=100');
    if (res.ok) hops = body.hops || [];
    render();
  } catch {
    /* keep the last good log */
  } finally {
    hopsBusy = false;
  }
}

async function pollPing() {
  try {
    const { res, body, rtt, sent, received } = await getJson('/api/traveler/ping');
    if (!res.ok) {
      answeredBy = '';
      return;
    }
    if (body.now) serverClock.addPing(Date.parse(body.now), sent, received);
    else serverClock.addDateHeader(res.headers.get('date'), received);
    answeredBy = res.headers.get('x-traveler-region') || body.region || '';
    latency.add(Date.now(), Math.round(rtt), answeredBy);
  } catch {
    answeredBy = '';
  }
  renderProof();
  if (current) renderMove(current);
}

// --- render ------------------------------------------------------------------------------------

const STATUS_TEXT = { resident: 'Live', packing: 'Packing', flight: 'Moving', resting: 'Resting', unknown: 'Connecting…' };

function render() {
  const now = serverClock.now();
  const v = M.view({ state, events: log.all(), hops, regions: REGIONS, now });
  if (feedOk === false && !state.status) v.mode = 'unknown';
  v.last = M.lastMove(hops, v.mode === 'flight' ? v.flight.from : state.region);
  current = v;
  const pill = $('status');
  pill.textContent = feedOk === false ? 'Reconnecting…' : STATUS_TEXT[v.mode];
  pill.className = `pill ${v.mode === 'resident' ? 'live' : v.mode}`;
  const hopId = v.mode === 'flight' ? v.flight.hopId : state.hop_id;
  $('hop').textContent = hopId ? `hop ${Number(hopId.slice(2)).toLocaleString()}` : '';
  renderHero(v);
  renderMove(v);
  renderFeed(v.feed);
  renderLog();
  renderMap(v);
  renderSummary(v, now);
  $('release').textContent = state.release ? `Release ${state.release}` : '';
}

// --- hero: the live sentence ----------------------------------------------------------------------

let heroKey = '';
function renderHero(v) {
  const f = v.flight;
  const r = v.residence;
  const step = f ? M.PHASES.findIndex((p) => !f.done[p.name]) : -1;
  const key = [v.mode, r.region, r.nextRegion, f?.hopId, step, f?.landed, v.last?.hopId, state.hops_ok, state.awake_since].join('|');
  if (key === heroKey) return;
  heroKey = key;
  const em = (t) => h('em', {}, t);
  let title, lede, facts = [];

  if (v.mode === 'flight' && f.landed) {
    title = ['This website just moved to ', em(city(f.to)), '.'];
    lede = [`The copy ${near(f.from)} has been deleted. Nothing is left there.`];
  } else if (v.mode === 'flight') {
    const p = M.PHASES[Math.max(0, step)];
    title = ['This website is moving from ', em(city(f.from)), ' to ', em(city(f.to)), ', right now.'];
    lede = [h('strong', {}, `Step ${step + 1} of ${M.PHASES.length}, ${p.label.toLowerCase()}: `), M.explain(p, f.from, f.to, REGIONS)];
    facts = [
      h('li', {}, 'Moving for ', tickEl('elapsed', { 'data-from': f.startedAt || '' }, 'b')),
      REGIONS[f.from] && REGIONS[f.to] ? h('li', {}, h('b', {}, Math.round(M.distanceKm(REGIONS[f.from], REGIONS[f.to])).toLocaleString()), ' km') : null,
    ];
  } else if (v.mode === 'resident' || v.mode === 'packing') {
    if (v.mode === 'packing') {
      title = ['This website is packing up to leave ', em(city(r.region)), '.'];
      lede = ['In ', tickEl('countdown', { 'data-at': r.departAt || '' }), ` it builds a copy of itself ${near(r.nextRegion)}, switches your traffic over, and deletes the copy here.`];
    } else {
      title = ['Right now, this website is running ', em(r.near), '.'];
      lede = ['In ', tickEl('countdown', { 'data-at': r.departAt || '' }), ' it will build a copy of itself in another part of the world, send your traffic there, and delete itself here. No one presses a button.'];
    }
    facts = [
      h('li', {}, 'Here for ', tickEl('since', { 'data-from': r.arrivedAt || '' }, 'b')),
      v.last?.landedAt ? h('li', {}, 'Previous copy ', h('b', {}, near(v.last.from)), ' deleted ', tickEl('since', { 'data-from': v.last.landedAt }, 'b'), ' ago') : null,
      state.hops_ok ? h('li', {}, h('b', {}, state.hops_ok.toLocaleString()), ' moves so far') : null,
    ];
  } else if (v.mode === 'resting') {
    title = ['This website is resting.'];
    lede = [`It stopped moving and deleted itself, so nothing is running anywhere. It was last ${r.region ? near(r.region) : 'seen moving'}. It wakes up on its own schedule, and this page will notice.`];
  } else {
    title = ["Finding the website's server…"];
    lede = ["Can't reach the control tower yet. Retrying every few seconds."];
  }
  $('headline').replaceChildren(...title);
  $('lede').replaceChildren(...lede);
  $('hero-facts').replaceChildren(...facts.filter(Boolean));
  tick();
}

// --- the move: three boxes and the steps -----------------------------------------------------------

let stepsKey = '';
function renderMove(v) {
  const f = v.mode === 'flight' ? v.flight : null;
  const r = v.residence;
  const m = M.moveState({ flight: f, here: v.mode === 'resting' ? '' : r.region, next: r.nextRegion, regions: REGIONS, now: serverClock.now() });

  const box = (id, part, kicker) => {
    const node = $(`node-${id}`);
    node.className = `node copy ${part.status}`;
    $(`${id}-kicker`).textContent = kicker;
    $(`${id}-title`).textContent = part.region ? `${city(part.region)} · ${part.region}` : '–';
    $(`${id}-text`).textContent = part.text;
    $(`${id}-answered`).hidden = !(answeredBy && part.region === answeredBy && part.status !== 'none' && part.status !== 'gone');
  };
  box('old', m.old, v.mode === 'resting' ? 'Nothing running' : f ? 'Old copy' : 'Running now');
  box('new', m.new, f ? 'New copy' : 'Next copy');

  $('node-edge').className = `node edge ${m.edge.status}`;
  $('edge-text').textContent = m.edge.text;
  $('edge-settle').hidden = !m.edge.settle;
  if (m.edge.settle) $('edge-settle').dataset.since = m.edge.settle.since;

  const copies = $('copies');
  copies.textContent = String(m.copies);
  copies.classList.toggle('two', m.copies > 1);
  copies.nextElementSibling.textContent = m.copies === 1 ? 'copy running' : 'copies running';

  const sub = $('move-sub');
  if (f && !f.landed) sub.replaceChildren(h('strong', {}, 'Live'), ` · step ${Math.min(M.PHASES.length, Object.keys(f.done).length + 1)} of ${M.PHASES.length}`);
  else if (v.mode === 'resident' || v.mode === 'packing') sub.replaceChildren('Next move in ', tickEl('countdown', { 'data-at': r.departAt || '' }));
  else if (v.mode === 'resting') sub.replaceChildren('Not moving while it rests');
  else sub.replaceChildren();

  // Steps: live during a flight; between flights, the last move's timings.
  const last = v.last;
  const key = [v.mode, f?.hopId, f && Object.keys(f.done).join(','), last?.hopId].join('|');
  if (key === stepsKey) return;
  stepsKey = key;
  let title, from, to, times, live = false;
  if (f) {
    title = f.landed ? `This move: ${city(f.from)} → ${city(f.to)}, done` : `This move: ${city(f.from)} → ${city(f.to)}`;
    ({ from, to } = f);
    times = M.stepTimes(f.done, f.startedAt);
    live = true;
  } else if (last) {
    title = `Last move: ${city(last.from)} → ${city(last.to)}${last.flightMs ? `, ${M.seconds(last.flightMs)}` : ''}`;
    ({ from, to, times } = last);
  } else {
    title = 'Every move takes these nine steps';
    times = {};
  }
  $('steps-title').textContent = title;
  const firstOpen = M.PHASES.findIndex((p) => !(p.name in times));
  $('steps').replaceChildren(
    ...M.PHASES.map((p, i) => {
      const done = p.name in times;
      const cls = live ? (done ? 'done' : i === firstOpen ? 'current' : '') : done ? 'past' : '';
      return h(
        'li',
        { class: cls },
        h('span', { class: 'mark', 'aria-hidden': 'true' }),
        h('span', { class: 'label' }, p.label, done ? h('span', { class: 'sr-only' }, live ? ', done' : '') : null),
        h('time', {}, done ? `${M.seconds(times[p.name])}` : ''),
        h('span', { class: 'why' }, M.explain(p, from, to, REGIONS)),
      );
    }),
  );
}

// --- proof: who answered, and the latency chart ---------------------------------------------------

const CHART_SPAN_MS = 5 * 60 * 1000;
let hoverT = null;

function renderProof() {
  const last = latency.samples.at(-1);
  $('answer-region').textContent = answeredBy ? `${city(answeredBy)} · ${answeredBy}` : 'nobody yet';
  $('answer-ms').textContent = last && answeredBy ? `${last.ms} ms round trip` : '';
  const meds = latency.medians();
  $('medians').replaceChildren(...(meds.length > 1 ? meds.map((m) => h('li', {}, h('b', {}, city(m.region)), ` ${m.ms} ms`)) : []));
  drawChart();
}

function drawChart() {
  const svg = $('chart-svg');
  const W = svg.clientWidth || 300, H = 150, L = 36, R = 8, T = 18, B = 18;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const end = Date.now(), start = end - CHART_SPAN_MS;
  const pts = latency.samples.filter((p) => p.t >= start);
  const kids = [];
  if (pts.length < 2) {
    kids.push(s('text', { class: 'empty-text', x: String(L), y: String(H / 2) }));
    kids[0].textContent = 'Measuring… a point every few seconds.';
    svg.replaceChildren(...kids);
    return;
  }
  const maxMs = Math.max(...pts.map((p) => p.ms));
  const step = [50, 100, 200, 250, 500, 1000, 2000].find((v) => v * 2 >= maxMs) || 5000;
  const top = step * 2;
  const x = (t) => L + ((t - start) / CHART_SPAN_MS) * (W - L - R);
  const y = (ms) => T + (1 - Math.min(ms, top) / top) * (H - T - B);
  for (const v of [0, step, top]) {
    kids.push(s('line', { class: 'grid', x1: String(L), x2: String(W - R), y1: String(y(v)), y2: String(y(v)) }));
    const label = s('text', { class: 'axis', x: String(L - 6), y: String(y(v) + 4), 'text-anchor': 'end' });
    label.textContent = v === 0 ? '0' : `${v} ms`;
    kids.push(label);
  }
  const ago = s('text', { class: 'axis', x: String(L), y: String(H - 3) });
  ago.textContent = '5 min ago';
  const nowLabel = s('text', { class: 'axis', x: String(W - R), y: String(H - 3), 'text-anchor': 'end' });
  nowLabel.textContent = 'now';
  kids.push(ago, nowLabel);
  for (const sw of latency.switches().filter((w) => w.t >= start)) {
    const sx = x(sw.t);
    kids.push(s('line', { class: 'switch', x1: String(sx), x2: String(sx), y1: String(T - 4), y2: String(H - B) }));
    const right = sx > W - 90;
    const t = s('text', { class: 'switch-label', x: String(sx + (right ? -4 : 4)), y: String(T - 6), 'text-anchor': right ? 'end' : 'start' });
    t.textContent = `moved to ${city(sw.to)}`;
    kids.push(t);
  }
  kids.push(s('path', { class: 'line', d: 'M' + pts.map((p) => `${x(p.t).toFixed(1)},${y(p.ms).toFixed(1)}`).join('L') }));
  if (hoverT !== null) {
    const p = pts.reduce((a, b) => (Math.abs(b.t - hoverT) < Math.abs(a.t - hoverT) ? b : a));
    kids.push(s('line', { class: 'cross', x1: String(x(p.t)), x2: String(x(p.t)), y1: String(T), y2: String(H - B) }));
    kids.push(s('circle', { class: 'dot', cx: String(x(p.t)), cy: String(y(p.ms)), r: '4' }));
    const tip = $('chart-tip');
    tip.textContent = `${secFmt.format(p.t)} · ${p.ms} ms · ${city(p.region)}`;
    tip.style.left = `${Math.min(W - 70, Math.max(70, x(p.t)))}px`;
    tip.hidden = false;
  } else {
    $('chart-tip').hidden = true;
  }
  svg.replaceChildren(...kids);
}

$('chart-svg').addEventListener('pointermove', (e) => {
  const svg = $('chart-svg');
  const rect = svg.getBoundingClientRect();
  const L = 36, R = 8;
  const f = (e.clientX - rect.left - L) / (rect.width - L - R);
  hoverT = Date.now() - CHART_SPAN_MS + Math.min(1, Math.max(0, f)) * CHART_SPAN_MS;
  drawChart();
});
$('chart-svg').addEventListener('pointerleave', () => {
  hoverT = null;
  drawChart();
});

// --- feed ------------------------------------------------------------------------------------------

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

// --- journey log and stats ---------------------------------------------------------------------------

let logKey = '';
function renderLog() {
  const st = M.stats(state, hops, towerStats);
  $('st-hops').textContent = st.hops?.toLocaleString() ?? '–';
  $('st-regions').textContent = st.regions ? String(st.regions) : '–';
  $('st-avg').textContent = st.avgFlightMs ? M.seconds(st.avgFlightMs) : '–';
  $('st-long').textContent = st.longestFlightMs ? M.seconds(st.longestFlightMs) : '–';
  $('st-aborted').textContent = st.aborted?.toLocaleString() ?? '–';
  const note = $('st-note');
  note.hidden = !st.sample;
  note.textContent = st.sample ? `Regions visited and move times cover the last ${st.sample} moves. Build is creating and checking the new copy; edge switch is waiting for CloudFront to send everyone there.` : '';
  const awake = Date.parse(state.awake_since || '');
  const awakeEl = $('awake');
  if (awake && state.status === 'live') {
    awakeEl.replaceChildren('Moving on its own for ', tickEl('since', { 'data-from': awake }, 'b'), ', with no one deploying it.');
  } else awakeEl.replaceChildren();

  const rows = M.journeyRows(hops, 12);
  const key = rows.map((r) => r.hopId + r.outcome).join(',');
  if (key === logKey) return;
  logKey = key;
  const sec = (ms) => (ms == null ? '–' : M.seconds(ms));
  $('journey').replaceChildren(
    ...(rows.length
      ? rows.map((r) => {
          const stepName = M.PHASES.find((p) => p.name === r.abortPhase)?.label.toLowerCase();
          const result = r.outcome === 'cancelled' ? `cancelled${stepName ? ` at “${stepName}”` : ''}, stayed` : r.outcome;
          return h(
            'tr',
            {},
            h('td', {}, r.at ? timeFmt.format(r.at) : '–'),
            h('td', {}, `${city(r.from)} → ${city(r.to)}`, h('span', { class: 'codes' }, `${r.from} → ${r.to}`)),
            h('td', { class: 'num' }, sec(r.buildMs)),
            h('td', { class: 'num' }, sec(r.edgeMs)),
            h('td', { class: 'num' }, sec(r.totalMs)),
            h('td', { class: `result ${r.outcome.replace(' ', '-')}` }, result),
          );
        })
      : [h('tr', {}, h('td', { colSpan: 6, class: 'empty' }, 'No moves yet.'))]),
  );
  tick();
}

// --- screen-reader summary ---------------------------------------------------------------------------

let lastSummary = '';
function renderSummary(v, now) {
  let text;
  const r = v.residence;
  if (v.mode === 'flight') {
    const next = M.PHASES.find((p) => !v.flight.done[p.name]);
    text = `The website is moving from ${near(v.flight.from)} to ${near(v.flight.to)}. ${next ? `Current step: ${next.label}.` : 'Landed.'}`;
  } else if (v.mode === 'resting') {
    text = 'The website is resting. Nothing is running.';
  } else if (v.mode === 'unknown') {
    text = 'Finding the website.';
  } else {
    const mins = r.departAt ? Math.max(0, Math.round((r.departAt - now) / 60000)) : null;
    const when = mins === null ? '' : mins < 1 ? ' Next move in under a minute.' : ` Next move in about ${mins} ${mins === 1 ? 'minute' : 'minutes'}.`;
    const dest = r.nextRegion ? ` Next stop: ${near(r.nextRegion)}.` : '';
    text = `The website is running ${r.near} (${r.region}).${when}${dest}`;
  }
  if (text !== lastSummary) $('map-summary').textContent = lastSummary = text;
}

// --- map -------------------------------------------------------------------------------------------

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
  $('trail').replaceChildren(
    ...tr
      .filter((t) => REGIONS[t.from] && REGIONS[t.to])
      .map((t, i) => s('path', { class: 'trail-arc', d: M.pathData(M.greatCircle(REGIONS[t.from], REGIONS[t.to], 48)), opacity: String(0.6 - i * 0.06), 'stroke-width': String(1.4 * unit) })),
  );

  const marks = [];
  for (const r of DATA.regions) {
    const cls = r.code === here && v.mode !== 'resting' ? 'pin here' : visited.has(r.code) ? 'pin visited' : 'pin';
    const c = s('circle', { class: cls, cx: r.x, cy: r.y, r: String((r.code === here ? 4.5 : 3) * unit) });
    const title = s('title');
    title.textContent = `${r.code} · ${r.near}`;
    c.append(title);
    marks.push(c);
  }
  // Labels sit above-right of the pin; the "here" label drops below when the next stop is close.
  const close = next && REGIONS[next] && REGIONS[here] && Math.hypot(REGIONS[next].x - REGIONS[here].x, REGIONS[next].y - REGIONS[here].y) < 40 * unit;
  const label = (code, text, below = false) => {
    const r = REGIONS[code];
    if (!r) return;
    const right = r.x > DATA.width - 140;
    const dy = below ? 17 : -7;
    const t = s('text', { class: 'pin-label', x: String(r.x + (right ? -9 : 9) * unit), y: String(r.y + dy * unit), 'text-anchor': right ? 'end' : 'start' });
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
    label(next, `next: ${REGIONS[next].city}`);
  }
  if (REGIONS[here]) label(here, v.mode === 'resting' ? `resting: ${REGIONS[here].city}` : REGIONS[here].city, close);
  $('pins').replaceChildren(...marks);

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

// --- per-frame ticking (clocks, countdowns, the plane) ----------------------------------------------

function tick() {
  const now = serverClock.now();
  for (const el of document.querySelectorAll('[data-tick]')) {
    const kind = el.dataset.tick;
    let text = null;
    if (kind === 'countdown' || kind === 'countdown-line') {
      const at = Number(el.dataset.at);
      const left = at - now;
      if (!at) text = '–';
      else if (kind === 'countdown-line') text = left > 0 ? `Leaving in ${M.clock(left)}` : 'Leaving now';
      else text = left > 0 ? M.clock(left) : 'a moment';
    } else if (kind === 'localtime') {
      text = localTime(now, el.dataset.tz);
    } else if (kind === 'elapsed') {
      const from = Number(el.dataset.from);
      text = from ? M.seconds(Math.max(0, now - from)) : '–';
    } else if (kind === 'since') {
      const from = Number(el.dataset.from);
      const ms = Math.max(0, now - from);
      text = !from ? '–' : ms < 3_600_000 ? M.clock(ms) : M.duration(ms);
    }
    if (text !== null && el.textContent !== text) el.textContent = text;
  }
  const settle = $('edge-settle');
  if (!settle.hidden) {
    const since = Number(settle.dataset.since);
    const ms = Math.max(0, now - since);
    $('edge-settle-bar').style.width = `${Math.min(99, (ms / M.EDGE_SETTLE_MS) * 100).toFixed(1)}%`;
    const text = `${Math.floor(ms / 1000)} s of about ${M.EDGE_SETTLE_MS / 1000} s`;
    if ($('edge-settle-text').textContent !== text) $('edge-settle-text').textContent = text;
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

// --- schedule (D7: 1 s while moving, 2 s otherwise, nothing while hidden) ---------------------------

const moving = () => current?.mode === 'flight' || (current?.mode === 'packing' && current.residence.leavesInMs < 5000);

let feedTimer = 0;
async function feedLoop() {
  clearTimeout(feedTimer);
  if (document.hidden) return;
  await pollFeed();
  feedTimer = setTimeout(feedLoop, moving() ? 1000 : 2000);
}

// Ping every 5 s, every 1 s during a move so the chart catches the edge switching over.
let pingTimer = 0;
async function pingLoop() {
  clearTimeout(pingTimer);
  if (document.hidden) return;
  await pollPing();
  pingTimer = setTimeout(pingLoop, moving() ? 1000 : 5000);
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
  drawChart();
});

measure();
feedLoop();
pingLoop();
pollHops();
requestAnimationFrame(frame);
