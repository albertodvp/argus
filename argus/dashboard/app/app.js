// @ts-check
(() => {
// Data comes from the plugin backend (export.build), not a static data.js.
/** @type {Argus.Export | null} */
let D = null;
/** @type {Record<string, Argus.Harness> | null} */
let HS = null;
/**
 * A value the data or the page guarantees (a key taken from the same export, the loaded data once render
 * ran, an element of index.html): throws a TypeError, as the property access on it would without the check.
 * @template T
 * @param {T | null | undefined} v
 * @returns {T}
 */
const must = v => { if (v === null || v === undefined) throw new TypeError(`argus: missing ${v}`); return v; };
/** A JSON list the code maps over; anything else throws the TypeError calling .map on it would. @type {(v: Argus.JsonValue) => Argus.JsonValue[]} */
const jsonList = v => { if (Array.isArray(v)) return v; throw new TypeError(`argus: not a list: ${typeof v}`); };
/** What `${err.message || err}` shows of a thrown value (an Error from api(), normally). @type {(err: unknown) => unknown} */
const errMsg = err => (typeof err === 'object' && err !== null && 'message' in err && err.message) || err;
const BASE = location.pathname.split('/dashboard-plugins/')[0];
const SDK = (() => { try { return window.parent.__HERMES_PLUGIN_SDK__; } catch { return null; } })();
// the routes' JSON is typed by the backend (types.d.ts mirrors argus/exporttypes.py): callers assert it
/** @type {(path: string, init?: RequestInit) => Promise<unknown>} */
const api = (path, init) => SDK?.fetchJSON ? SDK.fetchJSON(path, init)
  : fetch(BASE + path, { credentials: 'same-origin', ...init }).then(r => r.ok ? /** @type {Promise<unknown>} */ (r.json()) : r.text().then(t => { throw new Error(`${r.status}: ${t}`); }));
const firstHarness = () => must(Object.keys(must(HS))[0]);
/** @type {(s: string, el?: ParentNode) => HTMLElement | null} */
const $ = (s, el = document) => { const x = el.querySelector(s); return x instanceof HTMLElement ? x : null; };
/** @type {Record<string, string>} */
const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' };
/** @type {(s: unknown) => string} */
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ESC[c] ?? c);
// Values from the export (model output, session text, ids, names) reach HTML only through esc()
// (text, quoted attributes), tok() (class tokens) or upart() (URL path / query parts).
/** A class token from data: only [a-z0-9-] survive, so a value never leaves the class attribute. @type {(s: unknown) => string} */
const tok = s => String(s ?? '').toLowerCase().replace(/[^a-z0-9-]/g, '');
/** A URL path segment or query value from data; `'` too, as some links sit in inline JS strings. @type {(s: unknown) => string} */
const upart = s => encodeURIComponent(String(s ?? '')).replace(/'/g, '%27');
/** The inverse of upart() for a route part; a malformed escape is kept as it is. @type {(s: string) => string} */
const unpart = s => { try { return decodeURIComponent(s); } catch { return s; } };
/** A shell word from data for a copied command: plain names stay as they are, anything else is
 * single-quoted, so model text can never expand ($, backticks) when pasted into a terminal. @type {(s: unknown) => string} */
const shq = s => { const v = String(s ?? ''); return /^[A-Za-z0-9_.:\/@+=-]+$/.test(v) ? v : `'${v.replace(/'/g, `'\\''`)}'`; };
/** `hermes` for the default profile, else `hermes -p <profile>`. @type {(p: string) => string} */
const hermesFor = p => p === 'default' ? 'hermes' : `hermes -p ${shq(p)}`;
const MON = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];
/** @type {(n: number) => string} */
const pad = n => String(n).padStart(2, '0');
/** @type {(ts: number) => Date} */
const dt = ts => new Date(ts * 1000);
/** @type {(ts: number) => string} */
const day = ts => `${MON[dt(ts).getUTCMonth()]} ${pad(dt(ts).getUTCDate())}`;
/** @type {(ts: number) => string} */
const hm = ts => `${pad(dt(ts).getUTCHours())}:${pad(dt(ts).getUTCMinutes())}`;
/** @type {(ts: number) => string} */
const when = ts => `${day(ts)} ${hm(ts)}`;
/** @type {(ts: number) => number} */
const d0 = ts => Math.floor(ts / 86400) * 86400;
/** @type {(a: number, b: number) => string} */
const dur = (a, b) => { const h = (b - a) / 3600; return h < 1 ? `${Math.max(1, Math.round(h * 60))}m` : h < 48 ? `${Math.round(h)}h` : `${Math.round(h / 24)}d`; };
/** @type {(n: number, one: string, many: string) => string} */
const pl = (n, one, many) => `${n} ${n === 1 ? one : many}`;
/** @type {(n: number) => string} */
const kfmt = n => n >= 10000 ? Math.round(n / 1000) + 'k' : n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n);
/** @type {(a: number, b: number) => string} */
const pct = (a, b) => b ? `${(100 * a / b).toFixed(a / b < .1 ? 1 : 0)}%` : '0%';
/** @type {(s: string) => string} */
const unmention = s => (s || '').replace(/\[([^\]]+)\]\(<?<MENTION:[^>]*>>?\)/g, '"$1"').replace(/\*\*/g, '');
// ---------------------------------------------------------------- disclosure
// One line per item by default; the long text (rules, judgements, legends, commands) opens on request.
// Open rows, legends and folded sections are kept by key across render() (which rebuilds #view), not across a reload.
/** @type {Set<string>} */
const OPEN = new Set();
/** The `open` attribute of a <details data-k>. @type {(k: string) => string} */
const isOpen = k => OPEN.has(k) ? 'open' : '';
/** @type {(s: string, n: number) => string} */
const cut = (s, n) => s.length <= n ? s : s.slice(0, n).trimEnd() + '…';
/** Long text in a one-line row: its first n characters, the whole text once the row (details or data-xrow) is open. @type {(s: string | null | undefined, n: number) => string} */
const clip = (s, n) => { const t = s || ''; return t.length <= n ? esc(t) : `<span class="xs">${esc(cut(t, n))}</span><span class="xf">${esc(t)}</span>`; };
/** A DOM id for a disclosure key (keys hold arbitrary text). @type {(k: string) => string} */
const xid = k => { let h = 5381; for (let i = 0; i < k.length; i++) h = ((h * 33) ^ k.charCodeAt(i)) >>> 0; return 'x' + h.toString(36); };
/** The ▸/▾ toggle of a row marked data-xrow (class xo when open): its .xb parts and clipped text show when open; `id` = what it discloses. @type {(k: string, id: string) => string} */
const xbtn = (k, id) => `<button class="xp" type="button" data-x="${esc(k)}" aria-controls="${esc(id)}" aria-expanded="${OPEN.has(k)}" aria-label="details">${OPEN.has(k) ? '▾' : '▸'}</button>`;
/** @type {(k: string) => string} */
const xo = k => OPEN.has(k) ? 'xo' : '';
/** The '?' of a section: discloses the element with id hb-<k> (its legend). @type {(k: string) => string} */
const hintBtn = k => `<button class="hint" type="button" data-hint="${k}" aria-controls="hb-${k}" aria-expanded="${OPEN.has('hint:' + k)}" aria-label="legend">?</button>`;
/** @type {(k: string) => string} */
const hintOn = k => OPEN.has('hint:' + k) ? 'on' : '';
/** @type {(k: string, html: string) => string} */
const hintBody = (k, html) => `<div class="hb ${hintOn(k)}" id="hb-${k}">${html}</div>`;
/** A section header (title, count) with an optional '?' legend under it. @type {(title: string, count: string | number, k?: string, legend?: string) => string} */
const head = (title, count, k, legend) => `<h2>${title}<span class="c">${count}</span>${k ? hintBtn(k) : ''}</h2>${k ? hintBody(k, legend || '') : ''}`;
/** Legend entries: [swatch class or inline style, label]. @type {(items: [string, string][]) => string} */
const lkeys = items => `<div class="mxl">${items.map(([sw, label]) => `<span>${sw ? `<i ${sw.includes(':') ? `style="${sw}"` : `class="${sw}"`}></i>` : ''}${label}</span>`).join('')}</div>`;
// decisions live in layer 3 of each profile's plugin DB (POST /decision); this mirrors them by profile + UI id
// (ids repeat across profiles: a proposal id is hash(skill, rule)) and is filled from /data on every load
/** @type {Record<string, string>} */
const decisions = {};
/** @type {(prof: string | undefined, id: string | undefined) => string} */
const ekey = (prof, id) => `edit:${prof}:${id}`;   // a learning (ledger entry × file)
/** @type {(prof: string | undefined, id: string | undefined) => string} */
const pkey = (prof, id) => `prop:${prof}:${id}`;   // a proposal

/** @type {Record<string, string>} */
const STATUS = { achieved: 'met', partial: 'partial', failed: 'failed', unclear: 'unclear' };
/** @type {Record<string, string>} */
const WHO = { 'real-user': 'user', team: 'team', 'automated-test': 'test', scheduled: 'cron' };
// cron run results, in Hermes terms: delivered report, [SILENT], [CRON_FAILURE], no reply
/** @type {Record<string, string>} */
const RUN = { reported: 'delivered', silent: 'silent', failed: 'failed', empty: 'no reply' };
// who wrote an edit: the ledger actor, with `curator` split by export.writer_of
/** @type {Record<string, string>} */
const ACTOR = { agent: 'agent at work', review: 'background review', curator: 'curator', user: 'human' };
/** @type {Record<string, string>} */
const ACTOR_TIP = { review: 'Ledger actor "curator" in a session that has messages: the background review Hermes runs after a turn, in that turn\'s session.',
  curator: 'Ledger actor "curator" with no session in state.db: a periodic curator pass, or a background review whose session is gone. Hermes writes the same actor for both.' };
/** @type {Set<string | null>} */
const CLEANUP = new Set(['archive', 'delete', 'remove_file', 'purge']);
/** @type {Record<string, [string, string]>} */
const EV = { speculative: ['no evidence', 't-spec'], 'user-correction': ['from correction', 't-corr'], 'observed-failure': ['from failure', 't-ground'],
  'observed-success': ['from success', 't-ground'], 'no-run': ['no run', 't-unrev'], person: ['by a person', 't-unrev'], unreviewed: ['unreviewed', 't-unrev'], cleanup: ['cleanup', 't-cleanup'] };
const EV_TIP = 'Evidence = something in the run just before the edit that motivates it: a failure, a human correction or a success. "no evidence": the run was read and none was found, so the new rule targets a problem that was never observed. "no run": state.db holds no messages of the edit\'s session (a curator pass, a deleted session), so there is nothing to check it against. Checked by a model; open the edit to verify.';
/** What colours an edit, for the legends of the views that show edits. */
const EV_LEGEND = lkeys([['background:var(--warn)', 'no evidence'], ['background:var(--info)', 'from correction'], ['background:var(--sig)', 'from a run (failure or success)']]) + `<p>${esc(EV_TIP)}</p>`;
/** @type {Record<string, string>} */
const CAT_TIP = { runtime: 'gateway or agent loop: request dropped, turn interrupted, no final reply', policy: 'approval / tool policy blocked the call', env: 'missing binary, module, service or connectivity in the runtime',
  timeout: 'command or call timed out or was interrupted', input: 'tool called with a wrong path, argument or pattern', exit: 'non-zero exit or tool-reported failure' };
/** @type {(e: Argus.Learning) => string} */
const evOf = e => CLEANUP.has(e.action) ? 'cleanup' : e.actor === 'user' || e.action === 'rollback' ? 'person' : (e.review?.evidence_in_run || 'unreviewed');
/** @type {Record<string, string>} */
const EV_COLOR = { speculative: 'var(--warn)', 'user-correction': 'var(--info)', 'observed-failure': 'var(--sig)', 'observed-success': 'var(--sig)' };
/** @type {(k: string) => string} */
const evColor = k => EV_COLOR[k] || 'var(--faint)';
/** @type {(e: Argus.Learning) => string} */
const evTag = e => `<span class="tag ${must(EV[evOf(e)])[1]}">${must(EV[evOf(e)])[0]}</span>`;
/** @type {(c: string) => string} */
const catTag = c => `<span class="tag c-${tok(c)}" data-tip="${esc(CAT_TIP[c] || '')}">${esc(c)}</span>`;
// deterministic flag only: share of a skill's edits without evidence (export.py)
/** @type {Record<string, string>} */
const VERDICT = { 'needs review': 't-review' };
// `\.env` comes last: followed by the alternation bar it reads as an environment dump to install scanners
const SECURITY = /secret|credential|forbidden|prohibit|not allowed|permission|privilege|security|token|auth|exfiltr|sandbox|\broot\b|isolation|other (users|profiles|customers|tenants)|cross-|leak|expos|private|\.env/i;
/** @type {(i: Argus.Intent) => boolean} */
const quiet = i => ['probe', 'chat'].includes(i.kind) && !(i.judgement?.risk || '').trim() && i.judgement?.who !== 'real-user';
/** @type {(hk: string) => string} */
const pfKey = hk => 'argus-pf-' + hk;
/** @type {(hk: string) => string} */
const getPF = hk => sessionStorage.getItem(pfKey(hk)) || 'all';

// ---------------------------------------------------------------- model
/** @type {Record<string, Argus.App.Model>} */
const cache = {};
const dropModels = () => { for (const k of Object.keys(cache)) delete cache[k]; };
/** @param {string} hk @returns {Argus.App.Model} */
function model(hk) {
  const pf = getPF(hk), ck = hk + '|' + pf;
  if (cache[ck]) return cache[ck];
  const H = must(must(HS)[hk]);
  const learnings = pf === 'all' ? H.learnings : H.learnings.filter(e => e.profile === pf);
  /** @type {Record<string, Omit<Argus.App.Sig, 'nsess' | 'k'>>} */
  const sigs = {};   // failure signatures across the filtered intents
  const intents = (pf === 'all' ? H.intents : H.intents.filter(i => i.profiles.includes(pf))).map(raw => {
    const j = raw.judgement, unstable = j.stable === 'unstable', risk = (j.risk || '').trim();
    /** @type {Omit<Argus.App.Intent, keyof Argus.Intent>} */
    const derived = { edits: learnings.filter(e => e.intent === raw.id),
      first: Math.min(...raw.turns.map(t => t.ts)), last: Math.max(...raw.turns.map(t => t.ts)),
      who: raw.kind === 'routine' ? 'scheduled' : j.who || 'team',
      unstable,
      varies: !unstable && raw.kind !== 'routine' && raw.sessions.length >= 3 && !!(j.consistency || '').trim(),
      risk,
      security: !!risk && SECURITY.test(risk),
      status: j.status === 'achieved' && unstable ? 'partial' : j.status,
      calls: 0, errs: 0, rt: 0, tools: {}, score: 0, uses: {}, useSid: {} };
    const i = Object.assign(raw, derived);
    for (const t of i.turns) {
      for (const [tool, [n, e]] of Object.entries(t.tc || {})) { i.calls += n; i.errs += e; const x = (i.tools[tool] ??= [0, 0]); x[0] += n; x[1] += e; }
      i.rt += (t.rt || []).length;
      /** @type {(tool: string, sig: string, cat: string) => void} */
      const hit = (tool, sig, cat) => { const s = (sigs[`${tool}|${sig}`] ??= { tool, sig, cat, hits: 0, sessions: new Set(), intents: {}, first: Infinity, last: 0 });
        s.hits++; s.sessions.add(t.sid); s.intents[i.id] = (s.intents[i.id] || 0) + 1; s.first = Math.min(s.first, t.ts); s.last = Math.max(s.last, t.ts); };
      for (const [tool, sig, cat] of t.errs || []) hit(tool, sig, cat);
      (t.rt || []).forEach((r, k) => hit('runtime', r, (t.rtcat || [])[k] || 'runtime'));
    }
    const sp = i.edits.filter(e => evOf(e) === 'speculative').length;
    i.score = (i.risk ? 6 : 0) + (j.attention || 0) * 2 + (i.status === 'failed' ? 3 : i.status === 'partial' ? 1 : 0) + (i.unstable ? 2 : 0) + Math.min(3, sp);
    return i;
  });
  const props = pf === 'all' ? (H.proposals || []) : (H.proposals || []).filter(p => p.profile === pf);
  // carried out (applied or staged, not rolled back since): kept apart as the record of the decision, out of the
  // proposals and their counts. Otherwise a proposal for a skill no longer in the profile is obsolete, likewise.
  /** @type {Record<'proposals' | 'obsolete' | 'carried', Argus.Proposal[]>} */
  const split = { proposals: [], obsolete: [], carried: [] };
  for (const p of props) split[carried('proposal', p.profile, p.id, p) ? 'carried' : p.skill_gone ? 'obsolete' : 'proposals'].push(p);
  /** @type {Argus.App.Harness} */
  const h = { ...H, trends: H.trends || {}, blame: H.blame || {}, sessions: H.sessions || {}, examples: H.examples || {}, absent: H.absent || {}, intents, learnings, ...split };
  const I = Object.fromEntries(h.intents.map(i => [i.id, i])), E = Object.fromEntries(h.learnings.map(e => [e.id, e]));
  /** @type {Record<string, Argus.Learning[]>} */
  const groups = {};
  for (const e of h.learnings.slice().sort((a, b) => a.ts - b.ts)) (groups[`${e.profile}:${e.skill}`] ??= []).push(e);
  /** @type {(p: string) => string} */
  const prof = p => h.profiles[p] || (/^[0-9a-f]{20,}$/.test(p) ? `tenant ${p.slice(0, 6)}` : p);
  /** @type {Argus.App.Sig[]} */
  const sigList = Object.values(sigs).map(s => ({ ...s, nsess: s.sessions.size, k: 0 })).sort((a, b) => b.hits * Math.log2(b.nsess + 1) - a.hits * Math.log2(a.nsess + 1));
  sigList.forEach((s, k) => { s.k = k; });
  // skill use: explicit skill_view reads per turn, keyed like the ledger groups (profile:skill)
  const known = new Set([...Object.keys(groups), ...[...h.proposals, ...h.carried].map(p => `${p.profile}:${p.skill.replace(/^new:/, '')}`)]);
  /** @type {(p: string, name: string) => string} */
  const keyOf = (p, name) => [`${p}:${name}`, `shared:${name}`, ...[...known].filter(k => k.endsWith(':' + name))].find(k => known.has(k)) || `${p}:${name}`;
  /** @type {Record<string, Argus.App.Use>} */
  const use = {};   // key -> { loads, intents: {id: loads}, runs: [{ts, sid, iid, ok}] }, one run per session that read the skill
  for (const i of h.intents) {
    i.uses = {}; i.useSid = {};   // totals per skill, and per session: sid -> {key: reads}
    /** @type {Set<string>} */
    const seen = new Set();
    for (const t of i.turns) for (const [name, n] of Object.entries(t.sk || {})) {
      const k = keyOf(t.profile, name), u = (use[k] ??= { loads: 0, intents: {}, runs: [] });
      u.loads += n; u.intents[i.id] = (u.intents[i.id] || 0) + n; i.uses[k] = (i.uses[k] || 0) + n;
      const us = (i.useSid[t.sid] ??= {}); us[k] = (us[k] || 0) + n;
      if (seen.has(k + '|' + t.sid)) continue;
      seen.add(k + '|' + t.sid);
      const ro = (i.runs_out || {})[t.sid];
      u.runs.push({ ts: t.ts, sid: t.sid, iid: i.id, ok: ro ? ro.ok : i.status === 'achieved' ? true : i.status === 'failed' ? false : null });
    }
  }
  for (const u of Object.values(use)) u.runs.sort((a, b) => a.ts - b.ts);
  const visible = h.intents.filter(i => !quiet(i));
  const ts = (visible.length ? visible : h.intents).flatMap(i => i.turns.map(t => t.ts));
  const now = Math.floor(Date.now() / 1000);   // no intents yet (backfilled, not derived): an empty period ending today
  return cache[ck] = { hk, h, I, E, groups, prof, pf, visible, use, sigs: sigList, t0: ts.length ? Math.min(...ts) : now, t1: ts.length ? Math.max(...ts) : now };
}
/** @param {Argus.Learning[]} es */
function stats(es) {
  const real = es.filter(e => !CLEANUP.has(e.action));
  /** @type {Record<string, { w0: number, w1: number }>} */
  const files = {};
  for (const e of es) { files[e.file] ??= { w0: e.words_before, w1: 0 }; must(files[e.file]).w1 = e.words_after; }
  /** @type {Record<string, number>} */
  const actors = {}; for (const e of es) actors[String(e.actor)] = (actors[String(e.actor)] || 0) + 1;
  return { n: es.length, real: real.length, spec: real.filter(e => evOf(e) === 'speculative').length,
    synth: real.filter(e => e.intent_who === 'automated-test').length, actors, nfiles: Object.keys(files).length,
    w0: Object.values(files).reduce((a, f) => a + f.w0, 0), w1: Object.values(files).reduce((a, f) => a + f.w1, 0),
    t0: must(es[0]).ts, t1: must(es[es.length - 1]).ts, cleanup: es.some(e => CLEANUP.has(e.action)), desc: real.filter(e => e.desc_change).pop()?.desc_change };
}

// ---------------------------------------------------------------- queue: concrete actions
/** @type {Record<string, string>} */
const SIG_ACTION = { env: 'fix the runtime environment (missing binary, module or service)', policy: 'allow the call for this context or change the job so it does not need it',
  runtime: 'check gateway and agent-loop logs for these sessions', timeout: 'raise the limit or split the work', input: 'teach the skill the right paths and arguments', exit: 'inspect the failing command' };
/** @type {(s: string) => string} */
const firstClause = s => must((s || '').split(/;|, then | then /)[0]).replace(/\.$/, '');
/** @param {string} hk @returns {Argus.App.QueueItem[]} */
function queue(hk) {
  const m = model(hk);
  /** @type {Argus.App.QueueItem[]} */
  const out = [];
  /** @type {(sids: readonly string[] | ReadonlySet<string>) => number} */
  const editsIn = sids => {
    /** @type {Set<string | null>} */
    const ss = new Set(sids);
    return m.h.learnings.filter(e => ss.has(e.sid) && !CLEANUP.has(e.action)).length; };
  /** @type {(i: Argus.App.Intent) => { since: number, sess: number, edits: number }} */
  const ofIntent = i => ({ since: i.first, sess: i.sessions.length, edits: i.edits.length });
  for (const i of m.h.intents) if (i.security || (i.risk && (i.judgement.attention || 0) >= 2))
    out.push({ score: (i.security ? 1000 : 500) + i.score, kind: 'intent', sev: i.security ? 'security' : 'risk', detail: i.security ? 'security risk found in this intent' : 'risk found in this intent', what: i.risk,
      todo: firstClause(i.judgement.next) || 'review the intent', ctx: i.title, href: `#/${upart(hk)}/intents/${upart(i.id)}`, ...ofIntent(i) });
  for (const [k, es] of Object.entries(m.groups)) {
    const s = stats(es), [p, skill] = k.split(':'), tr = m.h.trends[k];
    if ((s.cleanup && !s.real) || (s.spec < 3 && s.real < 8)) continue;
    out.push({ score: 200 + s.spec * 3 + (s.synth > s.real / 2 ? 10 : 0), kind: 'skill', sev: 'review', detail: tr ? `skill flag: ${tr.verdict} (${tr.summary})` : 'skill changed by hermes',
      what: `${skill}: ${s.spec ? `${s.spec} of ${s.real} applied edits without evidence` : `${s.real} edits applied in ${dur(s.t0, s.t1)}`}${s.synth ? `, ${s.synth} from test runs` : ''}`,
      todo: `keep or reject the ${s.spec ? s.spec + ' edits without evidence' : s.real + ' edits'}`, ctx: `${m.prof(must(p))} · ${s.w0}→${s.w1} words`,
      href: `#/${upart(hk)}/skills/${encodeURIComponent(k)}`, since: s.t0, sess: new Set(es.map(e => e.sid)).size, edits: s.real });
  }
  for (const s of m.sigs) {
    if (!(s.hits >= 15 || (s.nsess >= 5 && s.cat !== 'input' && s.cat !== 'exit'))) continue;
    out.push({ score: 100 + Math.min(90, s.hits * Math.log2(s.nsess + 1) / 4), kind: 'error', sev: s.cat === 'policy' || s.cat === 'runtime' ? 'risk' : 'review', detail: `recurring error · class ${s.cat}`, what: `${s.tool === 'runtime' ? '' : s.tool + ': '}${s.sig}`,
      todo: SIG_ACTION[s.cat], ctx: `${s.hits}× · ${pl(s.nsess, 'session', 'sessions')} · last ${day(s.last)}`, href: `#/${upart(hk)}/overview?sig=${upart(s.k)}`, since: s.first, sess: s.nsess, edits: editsIn(s.sessions) });
  }
  for (const p of m.h.proposals || []) if (p.confidence !== 'low' && !decisions[pkey(p.profile, p.id)]) {
    const its = (p.intents || []).map(id => m.I[id]).filter(i => i !== undefined), sids = its.flatMap(i => i.sessions);
    out.push({ since: its.length ? Math.min(...its.map(i => i.first)) : p.ts, sess: new Set(sids).size, edits: editsIn(sids), score: (p.confidence === 'high' ? 300 : 250) + (p.intents || []).length, kind: 'proposal', sev: 'review', detail: 'skill change proposed by this plugin', what: p.rule, todo: `accept or dismiss: ${p.skill.startsWith('new:') ? 'create ' + p.skill.slice(4) : 'patch ' + p.skill}`, ctx: p.why, href: `#/${upart(hk)}/skills/${encodeURIComponent(p.profile + ':' + p.skill.replace(/^new:/, ''))}`, });
  }
  const gone = Object.values(m.groups).filter(es => es.some(e => CLEANUP.has(e.action)));
  if (gone.length >= 3) out.push({ since: Math.min(...gone.flat().filter(e => CLEANUP.has(e.action)).map(e => e.ts)), score: 90, kind: 'cleanup', sev: 'review', detail: 'skills archived or deleted by the curator', what: `${gone.length} skills archived or deleted by the curator`, todo: 'confirm none was still needed', ctx: '', href: `#/${upart(hk)}/skills/~cleanup`, });
  for (const i of m.visible) {
    if (i.risk || i.score < 5 || (i.status === 'achieved' && !i.unstable) || !i.judgement.next) continue;
    out.push({ score: 50 + i.score, kind: 'intent', sev: i.status === 'failed' ? 'risk' : 'review', detail: `intent ${i.unstable ? 'unstable across runs' : STATUS[i.status]}`, what: i.judgement.headline, todo: firstClause(i.judgement.next), ctx: i.title, href: `#/${upart(hk)}/intents/${upart(i.id)}`, ...ofIntent(i) });
  }
  return out.sort((a, b) => b.score - a.score);
}
/** @type {(n: number | undefined) => number | string} */
const qNum = n => n ? n : '<span class="dim">·</span>';
// one line: kind (colour = severity), what, numbers; the action, the reason and the context open with ▸
/** @type {(q: Argus.App.QueueItem) => string} */
const qItem = q => { const k = `q:${q.href}|${q.what}`;
  return `<div class="q sev-${q.sev} k-${q.kind} ${xo(k)}" data-xrow data-href="${esc(q.href)}"><span class="tag sev-${tok(q.sev)} k-${tok(q.kind)}">${esc(q.kind)}</span>
  <a class="qa" href="${esc(q.href)}">${clip(q.what, 90)}</a>
  <span class="qm">${qNum(q.sess)}</span><span class="qm ${q.edits ? 'qe' : ''}">${qNum(q.edits)}</span><span class="qm qs">${q.since ? day(q.since) : ''}</span>${xbtn(k, xid(k))}
  <div class="xb" id="${xid(k)}">${q.sev !== 'review' ? `<span class="qsev sev-${q.sev}">${q.sev}</span> · ` : ''}<span class="dim">${esc(q.detail)}</span>${q.todo ? `<div class="qd">do › ${esc(q.todo)}</div>` : ''}${q.ctx ? `<div class="dim">${esc(q.ctx)}</div>` : ''}</div></div>`; };
const qHead = '<div class="q qh"><span></span><span></span><span class="qm">sess</span><span class="qm">applied</span><span class="qm qs">since</span><span></span></div>';
const Q_LEGEND = `${lkeys([['background:var(--err)', 'security'], ['background:var(--warn)', 'risk'], ['background:#c58cff', 'proposal'], ['background:var(--line2)', 'review']])}
  <p>Security and risk findings first, then skills drifting without evidence, recurring errors, proposed edits. ▸ opens the action and the context.
  sess = sessions involved · applied = skill edits Hermes applied in those sessions · since = when it first appeared.</p>`;

// ---------------------------------------------------------------- overview
/** @param {string} hk @param {string | null} openSig */
function viewOverview(hk, openSig) {
  const m = model(hk), h = m.h, work = m.visible.filter(i => i.kind !== 'routine'), routines = m.visible.filter(i => i.kind === 'routine');
  /** @type {Record<string, number> & { achieved: number, partial: number, failed: number, unclear: number }} */
  const c = { achieved: 0, partial: 0, failed: 0, unclear: 0 }; for (const i of work) c[i.status] = (c[i.status] || 0) + 1;
  const calls = h.intents.reduce((a, i) => a + i.calls, 0), errs = h.intents.reduce((a, i) => a + i.errs, 0);
  const allTurns = h.intents.flatMap(i => i.turns);
  const rtk = { 'dropped': 0, 'interrupted': 0, 'no reply': 0 };
  for (const t of allTurns) for (const r of t.rt || []) rtk[r.startsWith('gateway') ? 'dropped' : r.includes('interrupted') ? 'interrupted' : 'no reply']++;
  const rt = Object.values(rtk).reduce((a, b) => a + b, 0);
  const edits = h.learnings.filter(e => !CLEANUP.has(e.action));
  /** @type {Record<string, number> & { speculative: number, 'user-correction': number, 'observed-failure': number, 'observed-success': number }} */
  const ev = { speculative: 0, 'user-correction': 0, 'observed-failure': 0, 'observed-success': 0 }; for (const e of edits) if (evOf(e) in ev) ev[evOf(e)] = (ev[evOf(e)] || 0) + 1;
  const Q = queue(hk), sec = Q.filter(q => q.sev === 'security').length;
  /** @type {number[]} */
  const days = []; for (let t = d0(m.t0); t <= d0(m.t1); t += 86400) days.push(t);
  const perDay = days.map(t => allTurns.filter(x => x.ts >= t && x.ts < t + 86400).length), pmx = Math.max(1, ...perDay);
  /** @type {(segs: Argus.App.Seg[]) => string} */
  const bar = segs => `<div class="sbar">${segs.filter(s => s[0]).map(([n, color, label]) => `<i style="flex:${n};background:${color}" data-tip="${esc(`${n} ${label}`)}"></i>`).join('')}</div>`;
  // one line per non-empty segment: colour, count, label
  /** @type {(segs: Argus.App.Seg[]) => string} */
  const lgd = segs => `<div class="lgd">${segs.filter(s => s[0]).map(([n, color, label]) => `<span><i style="background:${color}"></i>${n} ${label}</span>`).join('')}</div>`;
  /** @type {(text: string) => string} */
  const note = text => `<div class="lgd"><span>${text}</span></div>`;
  // value and micro-viz always; the per-segment legend (.lgd) and what the number means (.ex) open with the strip's '?'
  /** @type {(v: number | string, k: string, viz: string, lg: string, cls: string, ex: string) => string} */
  const cell = (v, k, viz, lg, cls, ex) => `<div class="stat ${cls}"><div class="k">${k}</div><div class="v">${v}</div>${viz}${lg}${ex ? `<div class="ex">${ex}</div>` : ''}</div>`;
  /** @type {Argus.App.Seg[]} */
  const sOut = [[c.achieved, 'var(--sig)', 'goal met'], [c.partial, 'var(--warn)', 'partial'], [c.failed, 'var(--err)', 'failed'], [c.unclear, 'var(--faint)', 'unclear']];
  /** @type {Argus.App.Seg[]} */
  const sRt = [[rtk.dropped, '#c58cff', 'dropped'], [rtk.interrupted, 'var(--warn)', 'interrupted'], [rtk['no reply'], 'var(--faint)', 'no reply']];
  /** @type {Argus.App.Seg[]} */
  const sEv = [[ev.speculative, 'var(--warn)', 'no evidence'], [ev['user-correction'], 'var(--info)', 'from correction'], [ev['observed-failure'] + ev['observed-success'], 'var(--sig)', 'from a run']];
  const runs = routines.flatMap(i => i.runs || []).sort((a, b) => a.ts - b.ts).slice(-80);
  const hist = `<svg class="hist" viewBox="0 0 ${days.length * 6} 20" preserveAspectRatio="none">${perDay.map((n, k) => `<rect x="${k * 6}" y="${20 - Math.max(n ? 2 : 1, 20 * n / pmx)}" width="5" height="${Math.max(n ? 2 : 1, 20 * n / pmx)}" class="${n ? 'on' : 'off'}"><title>${day(must(days[k]))}: ${n} turns</title></rect>`).join('')}</svg>`;
  return `${h.intents.length ? '' : '<div class="empty">no intents yet: run <code>hermes argus derive</code>, or press refresh</div>'}
  <div class="kpiw"><div class="stats kpi ${hintOn('kpi')}" id="hb-kpi">
    ${cell(work.length, 'intents', bar(sOut), lgd(sOut), '', 'What someone wanted from the agent; spans sessions. Probes and chit-chat without risk excluded.')}
    ${cell(allTurns.length, 'turns', hist, note(allTurns.length ? `${day(m.t0)} – ${day(m.t1)}` : 'none'), '', '')}
    ${cell(kfmt(calls), 'tool calls', bar([[calls - errs, 'var(--sig-dim)', 'ok'], [errs, 'var(--err)', 'errors']]), `<div class="lgd"><span><i style="background:var(--err)"></i>${errs} errors</span><span>${pct(errs, calls)} of calls</span></div>`, '', '')}
    ${cell(rt, 'runtime faults', rt ? bar(sRt) : '', rt ? lgd(sRt) : note('none'), rt ? 'w' : '', 'gateway dropped the request · turn interrupted · no final reply')}
    ${cell(edits.length, 'applied edits', edits.length ? bar(sEv) : '', edits.length ? lgd(sEv) : note('none'), '', 'Edits Hermes made to its own skills (ledger). Proposals from this plugin are separate, in skills.')}
    ${(() => { const P = h.proposals || [];
      /** @type {Argus.App.Seg[]} */
      const seg = [[P.filter(p => p.confidence === 'high').length, '#c58cff', 'high confidence'], [P.filter(p => p.confidence === 'medium').length, '#8a63b3', 'medium'], [P.filter(p => p.confidence === 'low').length, '#4d3a63', 'low']];
      return cell(`<a class="pp" href="#/${upart(hk)}/skills/~proposed">${P.length}</a>`, 'proposed edits', P.length ? bar(seg) : '', P.length ? lgd(seg) : note('none'), 'kp', 'Skill edits this plugin proposes from intents and recurring patterns. Nothing is applied until you accept one and click apply.'); })()}
    ${cell(sec, 'security', '', note(sec ? 'see queue' : 'none'), sec ? 'e' : '', 'Intents where the judge or a deterministic check found a security risk.')}
    ${cell(routines.length, 'cron jobs', routines.length ? `<div class="strip mini">${runs.map(r => `<i class="x-${tok(r.status)}" data-tip="${esc(when(r.ts) + ' · ' + RUN[r.status])}"></i>`).join('')}</div>` : '', routines.length ? note([`${runs.length} runs`, ...['reported', 'silent', 'failed'].map(k => /** @satisfies {[number, string]} */ ([runs.filter(r => r.status === k).length, k])).filter(([n]) => n).map(([n, k]) => `${n} ${RUN[k]}`)].join('<br>')) : note('none'), '', 'Hermes cron jobs: scheduled prompts whose final response is delivered to a channel. Each run is a session with source cron.')}
  </div>${hintBtn('kpi')}</div>
  <div class="home"><section>${skillMap(hk)}</section><section class="hq">
  ${head('to review', Q.length, 'queue', Q_LEGEND)}
  ${Q.length ? `${qHead}<div class="queue qscroll scrollbox">${Q.map(qItem).join('')}</div>` : '<div class="empty">empty</div>'}</section></div>
  ${sigTable(hk, openSig)}${routinesBlock(hk, routines)}`;
}
/** @param {string} hk @param {string | null} openSig */
function sigTable(hk, openSig) {
  const m = model(hk), S = m.sigs;
  if (!S.length) return '';
  /** @type {(s: Argus.App.Sig) => string} */
  const row = s => { const k = `sig:${s.tool}|${s.sig}`;
    return `<details class="sg" id="sig-${s.k}" data-k="${esc(k)}" ${String(s.k) === openSig || OPEN.has(k) ? 'open' : ''}><summary>
      <span>${catTag(s.cat)}</span><span class="tool">${esc(s.tool)}</span><span class="sig">${clip(s.sig, 80)}</span>
      <span class="n">${s.hits}</span><span class="n">${Object.keys(s.intents).length}</span><span class="n dim">${day(s.last)}</span></summary>
      <div class="sgb">${m.h.examples?.[`${s.tool}|${s.sig}`] ? `<pre class="raw">${esc(m.h.examples[`${s.tool}|${s.sig}`])}</pre>` : ''}
        ${Object.entries(s.intents).sort((a, b) => b[1] - a[1]).map(([id, n]) => `<div><span class="n" style="display:inline-block;width:40px">${n}×</span><a href="#/${upart(hk)}/intents/${upart(id)}"><span class="dot s-${tok(must(m.I[id]).status)}"></span>${esc(must(m.I[id]).title)}</a></div>`).join('')}</div></details>`; };
  const legend = `<p>The same error text returned by a tool (non-zero exit, error field, blocked call) or by the gateway/agent loop, normalised (paths, ids, numbers removed) and grouped across sessions. ▸ opens an example and the intents it hit.</p>
    <div class="kv">${Object.entries(CAT_TIP).map(([c, t]) => `<div class="k">${catTag(c)}</div><div>${esc(t)}</div>`).join('')}</div>`;
  return `${head('recurring errors', S.length, 'sigs', legend)}
  <div class="sgt"><div class="sgh"><span>class</span><span>tool</span><span>error</span><span class="n">hits</span><span class="n">intents</span><span class="n">last</span></div>
  ${scrollbox(firstN(S.map(row), 'rest:sigs'), 340)}</div>`;
}
// intents × skills for the home matrix: which skills each intent read, which it taught
// (edits Hermes learned during it), and which edits this plugin proposes from it. Top n skills and intents.
/** @param {Argus.App.Model} m @param {number} [n] */
function mapAxes(m, n = 14) {
  const props = m.h.proposals || [];
  /** @type {(p: Argus.Proposal) => string} */
  const pkey = p => `${p.profile}:${p.skill.replace(/^new:/, '')}`;
  /** @type {Record<string, Argus.App.MapCell>} */
  const cell = {};   // `${iid}|${key}` -> { reads, es, ps }
  /** @type {(iid: string, k: string) => Argus.App.MapCell} */
  const at = (iid, k) => (cell[`${iid}|${k}`] ??= { reads: 0, es: [], ps: [] });
  const its = m.visible.filter(i => i.kind !== 'routine'), vis = new Set(its.map(i => i.id));
  for (const i of its) { for (const [k, c] of Object.entries(i.uses)) at(i.id, k).reads = c; for (const e of i.edits) if (!CLEANUP.has(e.action)) at(i.id, `${e.profile}:${e.skill}`).es.push(e); }
  for (const p of props) for (const id of p.intents || []) if (vis.has(id)) at(id, pkey(p)).ps.push(p);
  /** @type {Record<string, number>} */
  const colScore = {};
  for (const [ik, c] of Object.entries(cell)) { const k = must(ik.split('|')[1]); colScore[k] = (colScore[k] || 0) + 2 + c.es.length + c.ps.length * 6; }
  const cols = Object.keys(colScore).sort((a, b) => must(colScore[b]) - must(colScore[a])).slice(0, n);
  /** @type {(i: Argus.App.Intent) => number} */
  const rowScore = i => cols.reduce((a, k) => { const c = cell[`${i.id}|${k}`]; return a + (c ? 1 + c.es.length * 2 + c.ps.length * 6 : 0); }, 0);
  const allRows = its.filter(i => rowScore(i)).sort((a, b) => rowScore(b) - rowScore(a));
  return { props, pkey, cell, cols, allRows, rows: allRows.slice(0, n) };
}
/** @param {string} hk */
function skillMap(hk) {
  const m = model(hk), { cell, cols, rows } = mapAxes(m);
  if (!cols.length) return '';
  const known = new Set(skillKeys(m));
  // one state per cell, the most important wins: a proposal from this intent, an edit Hermes applied
  // here without evidence, one with evidence, a read; hover and the pair view (click) have the rest
  /** @type {(i: Argus.App.Intent, k: string) => string} */
  const td = (i, k) => { const c = cell[`${i.id}|${k}`];
    if (!c) return '<td class="mx"></td>';
    const sp = c.es.filter(e => evOf(e) === 'speculative').length;
    const state = c.ps.length ? 'c-prop' : sp ? 'c-spec' : c.es.length ? 'c-ev' : 'c-read';
    const tip = cut(`${k.split(':')[1]} · ${c.reads ? `read ${c.reads}×` : 'not read'}${c.es.length ? ` · ${c.es.length} applied${sp ? `, ${sp} no evidence` : ''}` : ''}${c.ps.length ? ` · ${c.ps.length} proposed` : ''}`, 80);
    return `<td class="mx ${state}" data-tip="${esc(tip)}" data-href="#/${upart(hk)}/intents/${upart(i.id)}?skill=${encodeURIComponent(k)}"><i></i></td>`; };
  const keys = lkeys([['c-prop', 'proposal'], ['c-spec', 'changed, no evidence'], ['c-ev', 'changed, with evidence'], ['c-read', 'read']]);
  const legend = `<p>Rows: intents; columns: the skills they touched. Each cell shows the most important thing that happened there: this plugin proposes an edit to the skill from this intent, Hermes changed the skill after a turn of this intent (with or without evidence), or the intent only read it.
    edits = changes Hermes applied to the skill in this period (amber = some without evidence). Click a cell for the pair view.</p>`;
  return `${head('intents × skills', `${rows.length} × ${cols.length}`, 'matrix', legend)}
    <div class="mxwrap"><div>${keys}<table class="mxt">
    <tr class="mf"><td>edits</td>${cols.map(k => { const es = (m.groups[k] || []).filter(e => !CLEANUP.has(e.action)), sp = es.filter(e => evOf(e) === 'speculative').length; return `<td class="n">${es.length ? `<span class="${sp ? 'w' : ''}">${es.length}</span>` : '·'}</td>`; }).join('')}</tr>
    ${rows.map(i => `<tr><td class="mxr"><a href="#/${upart(hk)}/intents/${upart(i.id)}" data-tip="${esc(cut(i.title, 80))}"><span class="dot s-${tok(i.status)}"></span>${esc(i.title)}</a></td>${cols.map(k => td(i, k)).join('')}</tr>`).join('')}
    <tr><th></th>${cols.map(k => { const sk = must(k.split(':')[1]); return `<th class="mxh" ${known.has(k) ? `data-href="#/${upart(hk)}/skills/${encodeURIComponent(k)}"` : ''} data-tip="${esc(`${sk} · ${m.prof(must(k.split(':')[0]))}`)}"><span>${esc(sk.startsWith(hk + '-') ? sk.slice(hk.length + 1) : sk)}</span></th>`; }).join('')}</tr>
    </table></div></div>`;
}
/** @param {string} hk @param {Argus.App.Intent[]} rs */
function routinesBlock(hk, rs) {
  rs = rs.filter(i => i.runs && i.runs.length > 1);
  if (!rs.length) return '';
  // one line per job; the judge's headline and each run's reply open with ▸ (the strip ticks only say when and how)
  return `${head('cron jobs', rs.length, 'cron', '<p>Hermes cron jobs: each run is a session with source cron. delivered = final response sent to the channel · silent = answered [SILENT] · failed = [CRON_FAILURE]. ▸ opens the judgement and the replies.</p>')}<table class="tb"><tr><th>job</th><th class="num">runs</th><th class="num">delivered</th><th class="num">silent</th><th class="num">failed</th><th class="num">tool err</th><th></th><th></th></tr>${rs.map(i => {
    /** @type {Record<string, number>} */
    const c = {}; for (const r of must(i.runs)) c[r.status] = (c[r.status] || 0) + 1;
    const x = 'cron:' + i.id;
    return `<tr class="click ${xo(x)}" data-xrow data-href="#/${upart(hk)}/intents/${upart(i.id)}"><td>${esc(i.title)}<div class="xb" id="${xid(x)}"><div class="muted">${esc(i.judgement.headline)}</div>
      <div class="replies">${must(i.runs).slice().reverse().map(r => `<div><span class="dim">${when(r.ts)} · ${RUN[r.status]}</span> ${esc(unmention(r.reply).slice(0, 240))}</div>`).join('')}</div></div></td>
      <td class="n">${must(i.runs).length}</td><td class="n">${c.reported || '·'}</td><td class="n">${c.silent || '·'}</td><td class="n ${c.failed ? 'e' : ''}">${c.failed || '·'}</td><td class="n ${i.errs ? 'e' : ''}">${i.errs || '·'}</td>
      <td><div class="strip">${must(i.runs).map(r => `<i class="x-${tok(r.status)}" data-tip="${esc(when(r.ts) + ' · ' + RUN[r.status])}"></i>`).join('')}</div></td><td class="xc">${xbtn(x, xid(x))}</td></tr>`; }).join('')}</table>`;
}

// ---------------------------------------------------------------- intents board
/** @param {Argus.App.Model} m @param {Argus.App.Intent} i */
function spark(m, i) {
  /** @type {number[]} */
  const days = []; for (let t = d0(m.t0); t <= d0(m.t1); t += 86400) days.push(t);
  const d = days.slice(-21), w = 4, gap = 1, H = 16;
  const v = d.map(t => i.turns.filter(x => x.ts >= t && x.ts < t + 86400).length), mx = Math.max(1, ...v);
  return `<svg class="spark" width="${d.length * (w + gap)}" height="${H}">${v.map((n, k) => `<rect x="${k * (w + gap)}" y="${H - (n ? Math.max(2, H * n / mx) : 1)}" width="${w}" height="${n ? Math.max(2, H * n / mx) : 1}" class="${n ? 'on s-' + tok(i.status) : 'off'}"/>`).join('')}</svg>`;
}
/** @param {string} hk @param {string | null} [who] */
function viewIntents(hk, who) {
  const m = model(hk), its = m.h.intents.slice().sort((a, b) => b.score - a.score || b.last - a.last);
  const showSpark = (m.t1 - m.t0) / 86400 >= 7;   // a per-day sparkline says nothing over a few days
  const vis = its.filter(i => !quiet(i) && (!who || i.who === who)), q = its.filter(quiet);
  const open = vis.filter(i => i.kind !== 'routine' && (i.status !== 'achieved' || i.risk || i.unstable || i.varies));
  const done = vis.filter(i => i.kind !== 'routine' && !open.includes(i)), rts = vis.filter(i => i.kind === 'routine');
  const whos = [...new Set(m.visible.map(i => i.who))];
  // one line per intent; the judge's headline opens with ▸
  /** @type {(i: Argus.App.Intent) => string} */
  const row = i => { const x = 'board:' + i.id;
    return `<tr class="click ${xo(x)}" data-xrow data-href="#/${upart(hk)}/intents/${upart(i.id)}">
    <td class="nw"><span class="dot s-${tok(i.status)}"></span>${STATUS[i.status]}</td>
    <td><div class="it">${i.security ? '<span class="tag t-sec">sec</span> ' : i.risk ? '<span class="tag t-risk">risk</span> ' : ''}${i.unstable ? '<span class="tag t-unstable">unstable</span> ' : i.varies ? '<span class="tag t-unstable">varies</span> ' : ''}${esc(i.title)}</div><div class="xb muted" id="${xid(x)}">${esc(i.judgement.headline)}</div></td>
    <td class="muted nw">${WHO[i.who]}</td>${showSpark ? `<td>${spark(m, i)}</td>` : ''}<td class="n">${i.sessions.length}</td>
    <td class="n ${i.errs ? 'e' : ''}">${i.errs || '·'}</td><td class="n ${i.edits.some(e => evOf(e) === 'speculative') ? 'w' : ''}">${i.edits.length || '·'}</td><td class="n dim">${day(i.last)}</td><td class="xc">${xbtn(x, xid(x))}</td></tr>`; };
  const thead = `<tr><th>state</th><th>intent</th><th>who</th>${showSpark ? '<th>activity</th>' : ''}<th class="num">sess</th><th class="num">err</th><th class="num">edits</th><th class="num">last</th><th></th></tr>`;
  /** @type {(a: Argus.App.Intent[]) => string} */
  const table = a => `<table class="tb board">${thead}${a.map(row).join('')}</table>`;
  /** @type {(w: string) => number} */
  const cnt = w => m.visible.filter(i => !w || i.who === w).length;
  const legend = `<p>requested by = who started the intent: end users of the product (user), the team building/operating the harness (team), scripted test prompts (test), or cron jobs (cron).
    err = tool results with an error, non-zero exit or blocked call · edits = skill edits Hermes applied during this intent, amber if some have no evidence · probes = technical probes and chit-chat without risk.
    done and probes are folded: ▸ opens them.</p>`;
  return `<div class="filt"><label class="sel"><span class="dim">requested by</span>
    <select onchange="location.hash='#/${upart(hk)}/intents'+(this.value?'?who='+encodeURIComponent(this.value):'')">${['', ...whos].map(w => `<option value="${esc(w)}" ${(who || '') === w ? 'selected' : ''}>${w ? WHO[w] : 'anyone'} · ${cnt(w)}</option>`).join('')}</select></label>${hintBtn('board')}</div>${hintBody('board', legend)}
  ${open.length ? head('open', open.length) + table(open) : ''}${done.length ? fold('done', done.length, 'board:done', table(done)) : ''}${rts.length ? head('cron jobs', rts.length) + table(rts) : ''}
  ${q.length && !who ? fold('probes', q.length, 'board:probes', scrollbox(table(q), 360)) : ''}`;
}
// long lists scroll inside a fixed height and fade at the bottom until the end is reached
/** @type {(html: string, h: number) => string} */
const scrollbox = (html, h) => `<div class="scrollbox" style="--h:${h}px">${html}</div>`;
/** A section folded until opened: its header is the toggle. @type {(title: string, count: string | number, k: string, html: string) => string} */
const fold = (title, count, k, html) => `<details class="fold" data-k="${esc(k)}" ${isOpen(k)}><summary><h2>${title}<span class="c">${count}</span></h2></summary>${html}</details>`;
/** A long list: its first n rows, the rest folded under "▸ N more". @type {(rows: string[], k: string, n?: number) => string} */
const firstN = (rows, k, n = 20) => rows.length <= n + 5 ? rows.join('')
  : `${rows.slice(0, n).join('')}<details class="rest" data-k="${esc(k)}" ${isOpen(k)}><summary>${rows.length - n} more</summary>${rows.slice(n).join('')}</details>`;

// ---------------------------------------------------------------- intent detail
/** @param {string} base @param {string | undefined} s */
function wordDelta(base, s) {
  const bw = new Set((base || '').toLowerCase().split(/\s+/)), sw = new Set((s || '').toLowerCase().split(/\s+/));
  const add = (s || '').split(/\s+/).filter(w => w && !bw.has(w.toLowerCase()));
  const del = (base || '').split(/\s+/).filter(w => w && !sw.has(w.toLowerCase()));
  if (!add.length && !del.length) return '<span class="dim">= same request</span>';
  return `${add.length ? `<span class="add">+ ${esc(add.slice(0, 14).join(' '))}${add.length > 14 ? ' …' : ''}</span>` : ''}${del.length ? ` <span class="del">− ${esc(del.slice(0, 10).join(' '))}${del.length > 10 ? ' …' : ''}</span>` : ''}`;
}
/** @param {Argus.App.Model} m @param {Argus.App.Intent} i @param {number} ts @returns {Argus.App.SkillVersion | null} */
function skillVersion(m, i, ts) {
  /** @type {Record<string, number>} */
  const counts = {};
  for (const e of m.h.learnings) if (i.profiles.includes(e.profile) && !CLEANUP.has(e.action) && e.ts >= i.first - 86400 && e.ts <= i.last) counts[`${e.profile}:${e.skill}`] = (counts[`${e.profile}:${e.skill}`] || 0) + 1;
  const top = Object.entries(counts).sort((a, b) => b[1] - a[1])[0];
  return top ? { skill: top[0].split(':')[1], v: (m.groups[top[0]] || []).filter(e => e.ts < ts).length } : null;
}
// intent × skill × time with the intent fixed: skills × runs of this intent. A cell = the skill read in that run;
// a bar on its right edge = edits Hermes applied to the skill before the next run.
/** @param {string} hk @param {Argus.App.Intent} i @param {Argus.App.SessionRun[]} per @param {string | null} focus */
function skillRuns(hk, i, per, focus) {
  const m = model(hk), cites = (/** @type {Argus.Proposal} */ p) => (p.intents || []).includes(i.id), mine = m.h.proposals.filter(cites);
  /** @type {(p: Argus.Proposal) => string} */
  const pkey = p => `${p.profile}:${p.skill.replace(/^new:/, '')}`;
  /** @type {Record<string, Argus.App.SkillRow>} */
  const rows = {};
  /** @type {(k: string) => Argus.App.SkillRow} */
  const at = k => (rows[k] ??= { k, reads: 0, es: [], ps: [] });
  for (const [k, n] of Object.entries(i.uses)) at(k).reads = n;
  for (const e of i.edits) if (!CLEANUP.has(e.action)) at(`${e.profile}:${e.skill}`).es.push(e);
  for (const p of mine) at(pkey(p)).ps.push(p);
  for (const p of m.h.carried.filter(cites)) at(pkey(p));   // carried out: the skill stays linked, out of the counts
  const list = Object.values(rows).sort((a, b) => b.ps.length - a.ps.length || b.es.length - a.es.length || b.reads - a.reads);
  if (!list.length) return '';
  /** @type {(k: string) => Argus.Learning[]} */
  const live = k => (m.groups[k] || []).filter(e => !CLEANUP.has(e.action));
  const starts = per.map(p => p.ts), end = i.last + 1;
  /** @type {(k: string, j: number) => Argus.Learning[]} */
  const between = (k, j) => live(k).filter(e => e.ts >= must(starts[j]) && e.ts < (j + 1 < starts.length ? must(starts[j + 1]) : end));
  /** @type {(k: string, ts: number) => number} */
  const ver = (k, ts) => live(k).filter(e => e.ts < ts).length;
  /** @type {(p: Argus.App.SessionRun) => string} */
  const outcome = p => p.ro ? (p.ro.ok ? 'met' : 'failed') : per.length === 1 ? (i.status === 'achieved' ? 'met' : i.status === 'failed' ? 'failed' : 'unclear') : 'unclear';
  const mx = Math.max(1, ...per.flatMap(p => Object.values(i.useSid[p.sid] || {})));
  /** @type {(k: string) => string} */
  const focusHref = k => `#/${upart(hk)}/intents/${upart(i.id)}?skill=${encodeURIComponent(k)}`;
  /** @type {(r: Argus.App.SkillRow, p: Argus.App.SessionRun, j: number) => string} */
  const cell = (r, p, j) => {
    const n = (i.useSid[p.sid] || {})[r.k] || 0, es = between(r.k, j), sk = r.k.split(':')[1];
    const worst = es.find(e => evOf(e) === 'speculative') || es[0];
    const tip = cut(`${sk} · run ${j + 1} · ${outcome(p)} · ${n ? `read ${n}× at v${ver(r.k, p.ts)}` : 'not read'}${es.length ? ` · then ${pl(es.length, 'edit', 'edits')}` : ''}`, 80);
    return `<td class="sc ${n ? 'rd' : ''}" style="--a:${n ? (0.3 + 0.7 * Math.log(n + 1) / Math.log(mx + 1)).toFixed(2) : 0}" data-tip="${esc(tip)}"><i></i>${worst ? `<b style="background:${evColor(evOf(worst))};opacity:${es.some(e => e.intent === i.id) ? 1 : 0.45}"></b>` : ''}</td>`;
  };
  /** @type {(_r: Argus.App.SkillRow, cls: string, v: number, tip: string) => string} */
  const total = (_r, cls, v, tip) => `<td class="n ${cls}" data-tip="${tip}">${v || '·'}</td>`;
  /** @type {(r: Argus.App.SkillRow) => number} */
  const after = r => live(r.k).filter(e => e.ts >= end).length;
  const grid = `<table class="srt"><tr><th class="sl"></th>${per.map((p, j) => `<th class="rh o-${outcome(p)}" data-tip="${esc(`run ${j + 1} · ${when(p.ts)} · ${outcome(p)}`)}">${j + 1}</th>`).join('')}
      <th class="rl" data-tip="edits applied to the skill after this intent ended">later</th><th class="n">read</th><th class="n">applied</th><th class="n">proposed</th></tr>
    ${list.map(r => `<tr class="${focus === r.k ? 'on' : ''}" data-href="${focusHref(r.k)}"><td class="sl"><span>${esc(r.k.split(':')[1])}</span></td>${per.map((p, j) => cell(r, p, j)).join('')}
      <td class="rl">${after(r) ? `<span class="dim">+${after(r)}</span>` : ''}</td>${total(r, '', r.reads, 'skill_view calls in this intent')}${total(r, r.es.some(e => evOf(e) === 'speculative') ? 'w' : '', r.es.length, 'edits Hermes applied after turns of this intent; amber = some without evidence')}${total(r, 'pp', r.ps.length, 'edits proposed by this plugin from this intent')}</tr>`).join('')}</table>`;
  const f = focus && rows[focus] ? rows[focus] : null;
  let pair = '';
  if (f) {
    const C = changesOf(m, f.k), sk = f.k.split(':')[1], readIn = per.filter(p => (i.useSid[p.sid] || {})[f.k]).length;
    const during = C.hermes.filter(c => c.ts >= i.first - 3600 && c.ts < end).reverse(), props = [...C.plugin, ...C.carried].filter(c => (c.p.intents || []).includes(i.id));
    const sp = f.es.filter(e => evOf(e) === 'speculative').length;
    pair = `<div class="pair" id="pair"><div class="ph"><b>${esc(sk)}</b><span class="dim">in this intent</span><span class="spacer"></span><a href="#/${upart(hk)}/skills/${encodeURIComponent(f.k)}">open skill →</a><a href="#/${upart(hk)}/intents/${upart(i.id)}">close ✕</a></div>
      <div class="pm">read in ${readIn} of ${pl(per.length, 'run', 'runs')} · v${ver(f.k, i.first)} → v${ver(f.k, end)} during the intent · ${f.es.length} applied here${sp ? ` <span class="w">(${sp} no evidence)</span>` : ''} · <span class="pp">${f.ps.length} proposed</span></div>
      ${props.length || during.length ? `<div class="hist">${props.map(c => changeRow(hk, c)).join('')}${firstN(during.map(c => changeRow(hk, c)), `rest:${i.id}:${f.k}`)}</div>` : '<div class="empty">no edits to this skill while the intent ran</div>'}</div>`;
  }
  const legend = `${lkeys([['mr', 'read in the run'], ['eb', 'edits applied before the next run'], ['eb ef', 'from another intent']])}
    <p>One row per skill the intent read, taught or motivates; one column per run (session), oldest first; column colour = run result. Bar on the right edge = edits applied to the skill before the next run (colour = evidence, faded = learned in another intent).
    later = edits applied after this intent ended · read = skill_view calls in this intent · applied = edits Hermes applied after its turns (amber = some without evidence) · proposed = edits this plugin proposes from it. Click a row for the pair view.</p>${EV_LEGEND}`;
  return `${head('skills × runs', `${list.length} × ${per.length}`, 'sxr', legend)}
    <div class="srwrap">${scrollbox(grid, 420)}</div>
    ${pair}`;
}
/** @param {string} hk @param {string} id @param {string | null} focus */
function viewIntent(hk, id, focus) {
  const m = model(hk), i = m.I[id];
  if (!i) return viewIntents(hk);
  const j = i.judgement;
  const sids = i.sessions.slice().sort((a, b) => (m.h.sessions[a]?.start || 0) - (m.h.sessions[b]?.start || 0));
  /** @type {Argus.App.SessionRun[]} */
  const per = sids.map(sid => { const tt = i.turns.filter(t => t.sid === sid); return { sid, tt, ts: must(tt[0]).ts, tools: tt.reduce((a, t) => a + t.tools, 0),
    errs: tt.reduce((a, t) => a + (t.errs || []).length, 0), rt: tt.reduce((a, t) => a + (t.rt || []).length, 0), ro: (i.runs_out || {})[sid], sv: skillVersion(m, i, must(tt[0]).ts) }; });
  const mx = Math.max(1, ...per.map(p => p.tools));
  const strip = i.kind === 'routine' ? '' : `<div class="lane" data-tip="${esc(`block = session · width = tool calls · red = errors${per.some(p => p.sv?.v) ? ' · vN = skill version' : ''}`)}">${per.map(p => {
    const st = p.ro ? (p.ro.ok ? 'achieved' : 'failed') : i.status;
    // the run's result is in the runs table and the transcript
    return `<a class="blk s-${tok(st)} ${p.errs || p.rt ? 'haserr' : ''}" href="#t-${esc(p.sid)}" style="flex:${Math.max(1, p.tools) / mx * 6 + 1}" data-tip="${esc(cut(`${when(p.ts)} · ${p.tools} tools · ${p.errs} errors${p.rt ? ` · ${p.rt} runtime` : ''}${p.ro ? ` · ${p.ro.ok ? '✓' : '✗'}` : ''}${p.sv ? ` · ${p.sv.skill} v${p.sv.v}` : ''}`, 80))}">${p.sv && p.sv.v ? `<span>v${p.sv.v}</span>` : ''}</a>`; }).join('')}</div>
    <div class="lanek"><span>${when(i.first)}</span><span>${when(i.last)}</span></div>`;
  const tools = Object.entries(i.tools).sort((a, b) => b[1][0] - a[1][0]).slice(0, 6), tmx = Math.max(1, ...tools.map(t => t[1][0]));
  const mySigs = m.sigs.filter(s => s.intents[i.id]).sort((a, b) => must(b.intents[i.id]) - must(a.intents[i.id])).slice(0, 8);
  const first = per[0]?.tt[0]?.text || '';
  const runs = i.kind !== 'routine' && per.length >= 3 ? `${head('runs', per.length)}
    <details class="req" data-k="${esc('req:' + i.id)}" ${isOpen('req:' + i.id)}><summary><span class="dim">first request › </span>${clip(first, 100)}</summary></details>
    <table class="tb runs"><tr><th>when</th><th class="num">tools</th><th class="num">err</th>${per.some(p => p.sv) ? `<th class="num">${esc(per.find(p => p.sv)?.sv?.skill)}</th>` : ''}<th>request delta</th><th>result</th><th></th></tr>${per.map((p, k) => { const x = `run:${i.id}:${p.sid}`;
      return `<tr id="t-${esc(p.sid)}" data-xrow class="${xo(x)}">
      <td class="nw muted">${when(p.ts)}</td><td class="n">${p.tools}</td><td class="n ${p.errs ? 'e' : ''}">${p.errs || '·'}</td>${per.some(y => y.sv) ? `<td class="n">${p.sv ? 'v' + p.sv.v : ''}</td>` : ''}
      <td class="delta">${k ? wordDelta(first, must(p.tt[0]).text) : '<span class="dim">first</span>'}</td><td class="res">${p.ro ? `<span class="${p.ro.ok ? 'add' : 'del'}">${p.ro.ok ? '✓' : '✗'}</span> ${clip(p.ro.result, 60)}` : '·'}</td><td class="xc">${xbtn(x, 't-' + p.sid)}</td></tr>`; }).join('')}</table>` : '';
  // the strip says when and how each run went; every reply, newest first, is one line that opens to the full text
  const routine = i.kind === 'routine' ? `${head('runs', must(i.runs).length)}<div class="strip" style="margin-bottom:10px">${must(i.runs).map(r => `<i class="x-${tok(r.status)}" data-tip="${esc(when(r.ts) + ' · ' + RUN[r.status])}"></i>`).join('')}</div>
    ${firstN(must(i.runs).slice().reverse().map(r => { const x = `reply:${i.id}:${r.ts}`;
      return `<details class="turn reply" data-k="${esc(x)}" ${isOpen(x)}><summary><span class="tm">${when(r.ts)} · ${RUN[r.status]} · ${r.tools} tools</span> <span class="a">${clip(unmention(r.reply), 100)}</span></summary></details>`; }), 'rest:replies:' + i.id, 8)}` : '';
  const transcript = i.kind === 'routine' ? '' : `<details class="more" data-k="${esc('tr:' + i.id)}" ${isOpen('tr:' + i.id)}><summary>transcript · ${pl(i.turns.length, 'turn', 'turns')}</summary>${sids.map(sid => {
    /** @type {Partial<Argus.Session>} */
    const s = m.h.sessions[sid] || {};
    const tt = i.turns.filter(t => t.sid === sid), ch = i.children.filter(c => m.h.sessions[c.sid]?.parent === sid);
    const ro = (i.runs_out || {})[sid];
    return `<div class="sess" id="${per.length < 3 ? 't-' + esc(sid) : ''}">${when(must(tt[0]).ts)} · ${esc(s.source || '')} · ${esc(m.prof(s.profile || ''))} · ${esc(sid.slice(-8))}${ro ? ` · <span class="${ro.ok ? 'add' : 'del'}">${ro.ok ? '✓' : '✗'}</span> ${esc(ro.result)}` : ''}</div>` + tt.map(t => `<div class="turn"><div class="tm">${hm(t.ts)} · ${t.tools} tools · ${dur(t.ts, t.ts + (t.dur || 0))}${(t.errs || []).length ? ` · <span class="e">${t.errs.length} err</span>` : ''}${(t.rt || []).map(r => ` · <span class="w">${esc(r)}</span>`).join('')}</div>
      <div class="u">${esc(t.text.slice(0, 500))}</div>${t.reply ? `<div class="a clip" data-expand>${esc(unmention(t.reply).slice(0, 1500))}</div>` : ''}</div>`).join('') +
      (ch.length ? `<div class="turn"><div class="tm">↳ ${pl(ch.length, 'subagent', 'subagents')}: ${ch.map(c => esc(c.title.slice(0, 60))).join(' · ')}</div></div>` : '');
  }).join('')}</details>`;
  // the judgement: one line each for risk and next (full text on open), the rest under "judgement"
  /** @type {(cls: string, label: string, text: string, k: string) => string} */
  const call = (cls, label, text, k) => `<details class="callout ${cls}" data-k="${esc(k)}" ${isOpen(k)}><summary><b>${label}</b>${clip(text, 70)}</summary></details>`;
  /** @type {(v: number | string, k: string, cls?: string) => string} */
  const num = (v, k, cls) => ` · <span class="${cls || ''}">${v}</span> ${k}`;
  return `<a class="back" href="#/${upart(hk)}/intents">← intents</a>
  <h1>${esc(i.title)}</h1>
  <div class="meta"><span class="tag t-${tok(i.status)}">${STATUS[i.status]}</span> ${i.unstable ? '<span class="tag t-unstable">unstable</span> ' : i.varies ? '<span class="tag t-unstable">varies</span> ' : ''}${i.security ? '<span class="tag t-sec">sec</span> ' : i.risk ? '<span class="tag t-risk">risk</span> ' : ''}${WHO[i.who]} · ${day(i.first)}${day(i.last) !== day(i.first) ? ` → ${day(i.last)}` : ''}${num(i.sessions.length, 'sessions')}${num(kfmt(i.calls), 'tool calls')}${num(i.errs, 'tool errors', i.errs ? 'e' : '')}${i.rt ? num(i.rt, 'runtime faults', 'w') : ''}${i.edits.length ? num(i.edits.length, 'skill edits applied', i.edits.some(e => evOf(e) === 'speculative') ? 'w' : '') : ''}</div>
  ${i.risk ? call(i.security ? 'e' : 'w', i.security ? 'security' : 'risk', i.risk, 'risk:' + i.id) : ''}
  ${j.next ? call('n', 'next', j.next, 'next:' + i.id) : ''}
  <details class="more" data-k="${esc('judge:' + i.id)}" ${isOpen('judge:' + i.id)}><summary>judgement</summary><div class="kv" style="margin-top:6px">${j.headline ? `<div class="k">headline</div><div>${esc(j.headline)}</div>` : ''}${i.goal ? `<div class="k">goal</div><div>${esc(i.goal)}</div>` : ''}<div class="k">outcome</div><div>${esc(j.outcome)}</div>${j.consistency ? `<div class="k">across runs</div><div>${esc(j.consistency)}</div>` : ''}${j.friction ? `<div class="k">friction</div><div>${esc(j.friction)}</div>` : ''}</div></details>
  ${strip}
  <div class="${mySigs.length ? 'grid2' : ''}">
    <div><h2>tools<span class="c">${Object.keys(i.tools).length}</span></h2><div class="bars">${tools.map(([t, [n, e]]) => `<div class="br"><span class="bn">${esc(t)}</span><span class="bb"><i style="width:${100 * n / tmx}%"></i><i class="e" style="width:${100 * e / tmx}%"></i></span><span class="n">${n}${e ? ` <span class="e">${e}</span>` : ''}</span></div>`).join('') || '<div class="empty">no tool calls</div>'}</div></div>
    ${mySigs.length ? `<div>${head('recurring errors', mySigs.length)}${mySigs.map(s => `<a class="fs" href="#/${upart(hk)}/overview?sig=${upart(s.k)}">${catTag(s.cat)} <span class="n">${s.intents[i.id]}×</span> <span class="dim">${esc(s.tool)}</span> ${esc(cut(s.sig, 90))}</a>`).join('')}</div>` : ''}
  </div>
  ${runs}${routine}
  ${skillRuns(hk, i, per, focus)}
  ${transcript}`;
}

// ---------------------------------------------------------------- skills
/** @type {(d: string) => string} */
const diffHtml = d => esc(d).split('\n').map(l => l.startsWith('+') ? `<span class="add">${l}</span>` : l.startsWith('-') ? `<span class="del">${l}</span>` : l + '\n').join('');
/** @type {(e: Argus.Learning) => Argus.App.HermesChange} */
const hermesChange = e => ({ id: e.id, src: 'hermes', status: CLEANUP.has(e.action) ? 'removed' : 'applied', ts: e.ts, who: ACTOR[String(e.writer)] || e.writer,
  ev: evOf(e), rule: e.review?.learned || '', why: e.review?.trigger || '', diff: e.diff, plus: e.plus, minus: e.minus, file: e.file, e });
// One change model for both sources: Hermes ledger edits (applied) and this plugin's proposals (pending).
/** @param {Argus.App.Model} m @param {string} key @returns {Argus.App.Changes} */
function changesOf(m, key) {
  const [prof, skill] = key.split(':');
  const hermes = (m.groups[key] || []).map(hermesChange);
  /** @type {Argus.App.PluginChange[]} */
  /** @type {(ps: Argus.Proposal[]) => Argus.App.PluginChange[]} */
  const of = ps => ps.filter(p => `${p.profile}:${p.skill.replace(/^new:/, '')}` === key).map(p => ({ id: 'prop:' + p.id, src: 'plugin', status: 'pending',
    ts: p.ts, who: 'plugin', ev: 'proposal', rule: p.rule, why: p.why, diff: p.diff || '', plus: p.plus || 0, minus: p.minus || 0, file: p.file || '', p }));
  return { hermes, plugin: of(m.h.proposals || []), obsolete: of(m.h.obsolete || []), carried: of(m.h.carried || []), prof, skill };
}
/** @param {Argus.App.Model} m */
function skillKeys(m) {
  const keys = new Set(Object.keys(m.groups));
  for (const p of [...m.h.proposals || [], ...m.h.obsolete || [], ...m.h.carried || []]) keys.add(`${p.profile}:${p.skill.replace(/^new:/, '')}`);
  return [...keys];
}
/** @type {(r: Argus.App.UseRun) => string} */
const runOk = r => r.ok === true ? 'met' : r.ok === false ? 'failed' : 'unclear';
/** @type {(r: Argus.App.UseRun) => string} */
const runColor = r => r.ok === true ? 'var(--sig)' : r.ok === false ? 'var(--err)' : 'var(--faint)';
// where an edit came from and which runs read the skill afterwards
/** @param {Argus.App.Model} m @param {Argus.Learning} e @returns {Argus.App.Lineage} */
function lineage(m, e) {
  const src = m.I[String(e.intent)], u = m.use[`${e.profile}:${e.skill}`];
  const after = (u?.runs || []).filter(r => r.ts > e.ts), ids = [...new Set(after.map(r => r.iid))];
  return { src, after, ids, ok: after.filter(r => r.ok === true).length, ko: after.filter(r => r.ok === false).length,
    leak: src?.who === 'automated-test' && ids.some(id => m.I[id] && m.I[id].who !== 'automated-test') };
}
// one line per change (rule clipped); why, lineage, evidence, diff and the decision buttons open with the row
/** @param {string} hk @param {Argus.App.Change} c */
function changeRow(hk, c) {
  const m = model(hk), k = 'ch:' + c.id;
  if (c.src === 'plugin') {
    const p = c.p, dec = decisions[pkey(p.profile, p.id)];
    const pats = (p.patterns || []).map(key => m.sigs.find(s => `${s.tool}|${s.sig}` === key)).filter(s => s !== undefined);
    return `<details class="ch plugin ${dec ? 'd-' + tok(dec) : ''}" id="e-${esc(c.id)}" data-k="${esc(k)}" ${isOpen(k)}><summary>
      <span class="cdot"></span><span class="cw">proposed</span><span class="ctags"><span class="tag c-${tok(p.confidence)}" data-tip="model confidence">${esc(p.confidence)}</span>${applyTag('proposal', p.profile, p.id, p)}</span><span class="crule">${clip(c.rule, 60)}</span><span class="cd"><span class="add">+${c.plus}</span></span></summary>
      <div class="cb"><div class="p">${esc(c.why)}</div>${applyLine('proposal', p.profile, p.id, p)}
        <div class="ev2">${(p.intents || []).map(id => m.I[id]).filter(i => i !== undefined).map(i => `<a class="chip" href="#/${upart(hk)}/intents/${upart(i.id)}"><span class="dot s-${tok(i.status)}"></span>${esc(i.title)}</a>`).join('')}
          ${pats.map(s => `<a class="chip" href="#/${upart(hk)}/overview?sig=${upart(s.k)}">${catTag(s.cat)} ${s.hits}× ${esc((s.tool === 'runtime' ? '' : s.tool + ': ') + s.sig).slice(0, 70)}</a>`).join('')}
          ${jsonList(p.overlaps || []).map(o => m.h.learnings.find(e => e.entry === o)).filter(e => e !== undefined).map(e => `<a class="chip" href="javascript:void 0" data-goto="${esc(e.id)}" data-tip="${esc(cut(e.review?.learned || '', 80))}">overlaps hermes edit ${when(e.ts)}</a>`).join('')}</div>
        <pre class="diff">${diffHtml(c.diff)}</pre>
        ${carried('proposal', p.profile, p.id, p) ? '' : `<div class="acts"><button class="btn keep ${dec === 'accept' ? 'on' : ''}" data-pdec="accept" data-id="${esc(p.id)}" data-prof="${esc(p.profile)}" data-tip="adds it to the dock; nothing runs until apply">accept</button>
          <button class="btn rej ${dec === 'dismiss' ? 'on' : ''}" data-pdec="dismiss" data-id="${esc(p.id)}" data-prof="${esc(p.profile)}">dismiss</button></div>`}</div></details>`;
  }
  const e = c.e, dec = decisions[ekey(e.profile, e.id)], L = lineage(m, e), fp = fromProposal(m, e);
  /** @type {(i: Argus.App.Intent) => string} */
  const ichip = i => `<a class="chip" href="#/${upart(hk)}/intents/${upart(i.id)}"><span class="dot s-${tok(i.status)}"></span>${esc(i.title)}</a>`;
  const rule = c.rule || fp?.rule || '';
  const from = fp ? `<span class="pp">proposal ${esc(fp.id)}</span> ${(fp.intents || []).map(id => m.I[id]).filter(i => i !== undefined).map(ichip).join('')}`
    : L.src ? `${ichip(L.src)} <span class="dim">${WHO[L.src.who]}</span>` : '<span class="dim">no intent</span>';
  return `<details class="ch hermes ${dec ? 'd-' + tok(dec) : ''}" id="e-${esc(e.id)}" data-k="${esc(k)}" ${isOpen(k)}><summary>
    <span class="cdot" style="background:${fp ? '#c58cff' : evColor(c.ev)}"></span><span class="cw">${when(c.ts)}</span><span class="ctags">${fp ? '<span class="tag k-proposal" data-tip="this edit applied a proposal of this plugin, accepted from the dashboard or the CLI">from proposal</span>' : evTag(e)}${applyTag('skill_edit', e.profile, e.entry, e)}</span><span class="crule">${clip(rule, 60)}</span>
    <span class="cu ${L.leak ? 'w' : ''}" data-tip="${esc(L.ids.length ? `read afterwards by ${pl(L.ids.length, 'intent', 'intents')} in ${pl(L.after.length, 'run', 'runs')}${L.leak ? '\nfrom an automated test' : ''}` : 'not read again after this edit')}">${L.ids.length ? `↳ ${L.ids.length}` : ''}</span><span class="cd"><span class="add">+${c.plus}</span> <span class="del">−${c.minus}</span></span></summary>
    <div class="cb">${c.why || fp?.why ? `<div class="p">${esc(c.why || fp?.why || '')}</div>` : ''}
      <div class="lin"><span class="lk">by</span>${esc(c.who)}${ACTOR_TIP[String(e.writer)] ? ` <span class="dim">${esc(ACTOR_TIP[String(e.writer)])}</span>` : ''}</div>${applyLine('skill_edit', e.profile, e.entry, e)}
      <div class="lin"><span class="lk">from</span>${from}</div>
      <div class="lin"><span class="lk">read after</span>${L.ids.length ? `<span>${pl(L.after.length, 'run', 'runs')} · <span class="add">${L.ok} met</span>${L.ko ? ` · <span class="del">${L.ko} failed</span>` : ''}</span> ${L.ids.slice(0, 4).map(id => ichip(must(m.I[id]))).join('')}${L.ids.length > 4 ? `<span class="dim">+${L.ids.length - 4}</span>` : ''}` : '<span class="dim">not yet</span>'}</div>
      ${L.leak ? '<div class="lin w"><span class="lk">note</span>came from an automated test, now read by team or user intents</div>' : ''}
      ${e.desc_change ? `<div class="p w">description: ${esc(e.desc_change)}</div>` : ''}
      <pre class="diff">${diffHtml(c.diff)}</pre>
      <div class="acts"><button class="btn keep ${dec === 'keep' ? 'on' : ''}" data-dec="keep" data-id="${esc(e.id)}" data-prof="${esc(e.profile)}">keep</button>
        <button class="btn rej ${dec === 'reject' ? 'on' : ''}" data-dec="reject" data-id="${esc(e.id)}" data-prof="${esc(e.profile)}" data-tip="adds a rollback to the dock; nothing runs until apply">reject</button>
</div></div></details>`;
}
/** The proposal this edit carried out: an applied proposal whose ledger entry is this edit's.
 * @param {Argus.App.Model} m @param {Argus.Learning} e @returns {Argus.Proposal | undefined} */
function fromProposal(m, e) {
  return m.h.carried.find(p => p.profile === e.profile && applyState('proposal', p.profile, p.id, p).succ?.ledger_entry === e.entry);
}
/** @param {string} hk @param {string | undefined} key @param {string | undefined} mode */
function viewSkills(hk, key, mode) {
  const m = model(hk);
  /** @type {Record<string, number>} */
  const order = { 'needs review': 0 };
  const keys = skillKeys(m);
  if (!keys.length) return '<div class="empty">no skill changes in this period</div>';
  const info = keys.map(k => { const C = changesOf(m, k), s = C.hermes.length ? stats(must(m.groups[k])) : null;
    return { k, C, s, t: m.h.trends[k], ab: m.h.absent[k], applied: C.hermes.filter(c => c.status === 'applied').length, removed: C.hermes.some(c => c.status === 'removed') && !C.hermes.some(c => c.status === 'applied') }; });
  // skills no longer in their profile (their ledger history stays readable) go last, apart from the live ones
  const away = info.filter(g => !g.removed && g.ab);
  const live = info.filter(g => !g.removed && !g.ab).sort((a, b) => (order[String(a.t?.verdict)] ?? 4) - (order[String(b.t?.verdict)] ?? 4) || (b.s?.spec || 0) - (a.s?.spec || 0) || (b.applied + b.C.plugin.length) - (a.applied + a.C.plugin.length));
  const gone = info.filter(g => g.removed);
  if (key === '~proposed') key = (live.find(g => g.C.plugin.length) || live[0])?.k;
  const sel = key === '~cleanup' ? null : (info.find(g => g.k === key) || live[0]);
  /** @type {Record<string, typeof info>} */
  const byProf = {}; for (const g of live) (byProf[must(g.k.split(':')[0])] ??= []).push(g);
  /** @type {(g: (typeof info)[number]) => string} */
  const item = g => `<a class="${sel && g.k === sel.k ? 'on' : ''}" href="#/${upart(hk)}/skills/${encodeURIComponent(g.k)}" data-tip="${esc(`${g.applied} applied by hermes${g.s?.spec ? ` · ${g.s.spec} without evidence` : ''}${g.t ? ` · ${g.t.verdict}` : ''}`)}">
      <span class="vdot ${g.t ? VERDICT[g.t.verdict] : 'dim'}">${g.t ? '●' : '○'}</span><span class="sn">${esc(g.k.split(':')[1])}</span>${g.C.plugin.length ? `<span class="pb">${g.C.plugin.length}</span>` : ''}</a>`;
  /** @type {(g: (typeof info)[number]) => string} */
  const awayItem = g => `<a class="away ${sel && g.k === sel.k ? 'on' : ''}" href="#/${upart(hk)}/skills/${encodeURIComponent(g.k)}" data-tip="${esc(`${m.prof(must(g.k.split(':')[0]))} · ${g.ab?.state === 'missing' ? 'removed outside Hermes: no delete in the skill ledger' : 'removed by Hermes'}`)}">
      <span class="vdot ${g.ab?.state === 'missing' ? 'w' : 'dim'}">✕</span><span class="sn">${esc(g.k.split(':')[1])}</span><span class="tag ${g.ab?.state === 'missing' ? 'w' : ''}">${esc(g.ab?.state || '')}</span></a>`;
  const multi = Object.keys(byProf).length > 1 || gone.length || away.length;
  const side = `<div class="side scrollbox">
    ${Object.entries(byProf).map(([p, arr]) => `<div class="sgrp">${multi ? `<div class="sp"><span>${esc(m.prof(p))}</span><span>${arr.length}</span></div>` : ''}${arr.map(item).join('')}</div>`).join('')}
    ${away.length ? `<div class="sgrp away"><div class="sp"><span>no longer in the profile</span><span>${away.length}</span></div>${away.map(awayItem).join('')}</div>` : ''}
    ${gone.length ? `<div class="sgrp"><div class="sp"><span>removed</span><span>${gone.length}</span></div><a class="${key === '~cleanup' ? 'on' : ''}" href="#/${upart(hk)}/skills/~cleanup"><span class="vdot dim">○</span><span class="sn">archived / deleted</span></a></div>` : ''}</div>`;
  /** @type {string} */
  let main;
  if (!sel) main = `<h1>archived / deleted</h1><div class="meta" style="margin-bottom:12px">ledger actor "curator"; each entry can be rolled back</div><div class="hist">${firstN(gone.flatMap(g => g.C.hermes).map(c => changeRow(hk, c)), 'rest:~cleanup')}</div>`;
  else {
    const { C, s, t, ab } = sel, [prof, skill] = sel.k.split(':');
    const abNote = !ab ? '' : `<div class="callout ${ab.state === 'missing' ? 'w' : ''} away-note"><b>${ab.state === 'missing' ? 'missing' : 'removed'}</b>${ab.state === 'missing'
      ? `This skill is no longer in profile ${esc(m.prof(must(prof)))}, and the skill ledger has no delete or archive for it${ab.ts ? ` (last entry: ${esc(String(ab.action))} ${when(ab.ts)})` : ''}: it was removed outside Hermes. The agent cannot load it.`
      : `Removed by Hermes (${esc(String(ab.action))} ${ab.ts ? when(ab.ts) : ''}). The agent cannot load it.`} Its history stays below${C.obsolete.length ? `; its ${pl(C.obsolete.length, 'proposal is', 'proposals are')} obsolete` : ''}.</div>`;
    const files = [...new Set(C.hermes.map(c => c.file))];
    /** @type {[string, Argus.BlameLine[]][]} */
    const bf = [];
    for (const f of files) { const b = m.h.blame[`${prof}:${f}`]; if (b && b.length) bf.push([f, b]); }
    /** @type {Argus.App.Use} */
    const U = m.use[sel.k] || { loads: 0, intents: {}, runs: [] };
    mode = mode === 'content' && bf.length ? 'content' : 'activity';
    /** @type {(x: string) => string} */
    const href = x => `#/${upart(hk)}/skills/${encodeURIComponent(sel.k)}/${x}`;
    const applied = C.hermes.slice().reverse(), rel = relatedIntents(m, sel.k, C, U);
    main = `<h1 data-tip="${esc(m.prof(must(prof)) + (s ? ` · ${s.w0} → ${s.w1} words` : ''))}">${esc(skill)}${t ? ` <span class="tag ${VERDICT[t.verdict]}">${esc(t.verdict)}</span>` : ''}</h1>
      <div class="seg"><a class="${mode === 'activity' ? 'on' : ''}" href="${href('activity')}">activity</a>${bf.length ? `<a class="${mode === 'content' ? 'on' : ''}" href="${href('content')}">content</a>` : ''}${hintBtn('skill')}</div>
      ${abNote}
      ${hintBody('skill', `${t ? `<p>${esc(t.verdict)}: ${esc(t.summary)}</p>` : ''}<p>activity = intents that read, taught or motivate this skill, and its changes over time · content = current SKILL.md, each line coloured by the edit that wrote it.</p>${mode === 'content' ? BLAME_LEGEND : ''}`)}
      ${mode === 'content' ? blame(hk, bf) : `${swimlane(hk, sel.k, C, U, rel)}
        ${head('changes', C.hermes.length + C.plugin.length, 'changes', `<p>proposed = skill edits this plugin proposes from intents and recurring errors, not applied · applied = by Hermes (skill ledger), newest first · ↳ n = read afterwards by n intents · +/− = lines added/removed. ▸ opens the reason, lineage, diff and the decision.</p>${EV_LEGEND}`)}
        <div class="hist">${C.plugin.length ? `<div class="hsep">proposed · ${C.plugin.length}</div>${C.plugin.map(c => changeRow(hk, c)).join('')}` : ''}
        ${applied.length ? `<div class="hsep">applied · ${applied.length}${s?.spec ? ` · <span class="w">${s.spec} no evidence</span>` : ''}${s?.synth ? ` · ${s.synth} from test runs` : ''}</div>${firstN(applied.map(c => changeRow(hk, c)), 'rest:' + sel.k)}` : ''}
        ${C.carried.length ? `<details class="obs" data-k="car:${esc(sel.k)}" ${isOpen('car:' + sel.k)}><summary>carried out · ${C.carried.length} <span class="dim">proposals applied (their edit is in the skill ledger) or staged for /skills approve; out of every count</span></summary>${C.carried.map(c => changeRow(hk, c)).join('')}</details>` : ''}
        ${C.obsolete.length ? `<details class="obs" data-k="obs:${esc(sel.k)}" ${isOpen('obs:' + sel.k)}><summary>obsolete · ${C.obsolete.length} <span class="dim">proposals for this skill, made while it existed; out of every count</span></summary>${C.obsolete.map(c => changeRow(hk, c)).join('')}</details>` : ''}</div>`}`;
  }
  return `<div class="split">${side}<div>${main}</div></div>`;
}
// every intent linked to a skill: it read it, Hermes applied an edit after one of its turns, or a proposal cites it
/** @param {Argus.App.Model} m @param {string} _key @param {Argus.App.Changes} C @param {Argus.App.Use} U @returns {Argus.App.Related[]} */
function relatedIntents(m, _key, C, U) {
  /** @type {Record<string, Argus.App.Related>} */
  const rel = {};
  /** @type {(id: string) => Argus.App.Related} */
  const at = id => (rel[id] ??= { id, reads: 0, applied: 0, proposed: 0 });
  for (const [id, n] of Object.entries(U.intents)) at(id).reads = n;
  for (const c of C.hermes) if (c.e.intent && m.I[c.e.intent] && c.status === 'applied') at(c.e.intent).applied++;
  for (const c of C.plugin) for (const id of c.p.intents || []) if (m.I[id]) at(id).proposed++;
  for (const c of C.carried) for (const id of c.p.intents || []) if (m.I[id]) at(id);   // linked, not counted
  return Object.values(rel).filter(r => m.I[r.id]).sort((a, b) => b.proposed - a.proposed || b.applied - a.applied || b.reads - a.reads);
}
// intent × skill × time with the skill fixed: intents × time. Row = intent (its active span), tick = one of its sessions
// (tall and coloured by result if it read the skill), vertical line = an edit Hermes applied to the skill.
/** @param {string} hk @param {string} key @param {Argus.App.Changes} C @param {Argus.App.Use} U @param {Argus.App.Related[]} rel */
function swimlane(hk, key, C, U, rel) {
  const m = model(hk);
  if (!rel.length) return '<h2>intents<span class="c">0</span></h2><div class="empty">no intent read, taught or motivates this skill in this period</div>';
  const t0 = d0(m.t0), t1 = d0(m.t1) + 86400;
  /** @type {(ts: number) => string} */
  const x = ts => Math.max(0, Math.min(100, (ts - t0) / (t1 - t0) * 100)).toFixed(2);
  /** @type {number[]} */
  const days = []; for (let t = t0; t < t1; t += 86400) days.push(t);
  const step = days.length > 21 ? 7 : days.length > 10 ? 2 : 1;
  const read = new Map(U.runs.map(r => [r.sid + '|' + r.iid, r]));
  const lines = C.hermes.filter(c => c.status === 'applied');
  /** @type {(r: Argus.App.Related) => string} */
  const row = r => { const i = must(m.I[r.id]);
    const sess = [...new Map(i.turns.map(t => [t.sid, t.ts])).entries()].map(([sid]) => ({ sid, ts: Math.min(...i.turns.filter(t => t.sid === sid).map(t => t.ts)) }));
    const ticks = sess.map(s => { const u = read.get(s.sid + '|' + i.id);
      return u ? `<i class="tk rd" style="left:${x(s.ts)}%;background:${runColor(u)}" data-tip="${esc(`${when(s.ts)} · read the skill · ${runOk(u)}`)}"></i>` : `<i class="tk" style="left:${x(s.ts)}%" data-tip="${esc(`${when(s.ts)} · did not read the skill`)}"></i>`; }).join('');
    return `<div class="swr" data-href="#/${upart(hk)}/intents/${upart(i.id)}?skill=${encodeURIComponent(key)}"><div class="swl"><span class="dot s-${tok(i.status)}"></span><span class="swt" data-tip="${esc(cut(i.title, 80))}">${esc(i.title)}</span><span class="dim">${WHO[i.who]}</span></div>
      <div class="swa"><span class="span" style="left:${x(i.first)}%;width:${Math.max(0.4, Number(x(i.last)) - Number(x(i.first)))}%"></span>${ticks}</div>
      <div class="n">${r.reads || '·'}</div><div class="n">${r.applied || '·'}</div><div class="n">${r.proposed ? `<span class="pp">${r.proposed}</span>` : '·'}</div></div>`; };
  const overlay = `<div class="swo">${days.filter((_, k) => k % step === 0).map(t => `<i class="gd" style="left:${x(t)}%"></i>`).join('')}
    ${lines.map(c => `<i class="el" style="left:${x(c.ts)}%;background:${evColor(c.ev)}" data-goto="${esc(c.id)}" data-tip="${esc(`${when(c.ts)} · edit applied · ${must(EV[c.ev])[0]}`)}"></i>`).join('')}</div>`;
  const legend = `${lkeys([['tki', 'session that read it (colour = result)'], ['tki sh', 'did not read it'], ['eli', 'edit applied (colour = evidence)'], ['pp-i', 'proposed from the intent']])}
    <p>One row per intent linked to this skill, on the same time axis as its edits; the row is the intent's active span. Click a row for the pair view, a line for the edit.</p>`;
  return `${head('intents × time', `${rel.length} intents · ${lines.length} edits`, 'swim', legend)}
    <div class="sw">
      <div class="swr swh"><div></div><div class="swa ax">${days.filter((_, k) => k % step === 0).map(t => `<span style="left:${x(t)}%">${day(t)}</span>`).join('')}</div><div class="n">read</div><div class="n">applied</div><div class="n">proposed</div></div>
      ${scrollbox(`<div class="swb">${overlay}${rel.map(row).join('')}</div>`, 380)}
    </div>`;
}
const BLAME_LEGEND = `${lkeys([['background:var(--warn)', 'no evidence'], ['background:var(--info)', 'from correction'], ['background:var(--sig)', 'from a run'], ['background:#c58cff', 'from a proposal'], ['background:var(--line2)', 'pre-existing']])}<p>strikethrough = rejected · click a line for its edit</p>`;
/** @param {string} hk @param {[string, Argus.BlameLine[]][]} files */
function blame(hk, files) {
  const m = model(hk);
  return files.map(([f, lines]) => `<div class="dim" style="margin:12px 0 4px">${esc(f)}</div><div class="blame">${lines.map(l => {
      const e = m.E[l.o], rej = e && decisions[ekey(e.profile, e.id)] === 'reject', fp = e && fromProposal(m, e);
      const tip = e ? `${when(e.ts)} · ${fp ? `from proposal ${fp.id}` : must(EV[evOf(e)])[0]}` : '';
      return `<div class="bl ${e ? 'hit' : ''} ${rej ? 'rej' : ''}" ${e ? `data-goto="${esc(e.id)}" data-tip="${esc(tip)}"` : ''}><i style="background:${fp ? '#c58cff' : e ? evColor(evOf(e)) : 'var(--line2)'}"></i><span>${esc(l.t) || ' '}</span></div>`; }).join('')}</div>`).join('');
}

// ---------------------------------------------------------------- decisions
// apply state per target, from the export (apply_log): `applied` = latest success (applied, or staged while the
// pending write exists), `last_attempt` = latest row of any outcome. This page's click wins until the next load.
/** @type {Record<string, Argus.App.ApplyView>} */
const applying = {};   // `${kind}|${profile}|${target id}` -> POST /apply result, or { outcome: 'running' }
/** @type {readonly unknown[]} */
const DONE = ['applied', 'staged'];
/** @param {Argus.App.ApplyView | null | undefined} a @returns {a is Argus.App.ApplyView} */
const OK = a => !!a && DONE.includes(a.outcome);
/** @param {Argus.DecisionKind} kind @param {string} prof @param {string} id @param {Argus.App.Applicable} item */
function applyState(kind, prof, id, item) {
  const c = applying[`${kind}|${prof}|${id}`];
  return { succ: OK(c) ? c : item.applied || null, last: c || item.last_attempt || null };
}
/** Carried out and not undone since: applied or staged, not rolled back. @param {Argus.DecisionKind} kind @param {string} prof @param {string} id @param {Argus.App.Applicable} item */
function carried(kind, prof, id, item) { const s = applyState(kind, prof, id, item).succ; return !!s && !s.rolled_back; }
/** @type {Record<Argus.DecisionKind, string>} */
const APPLIED = { skill_edit: 'rolled back', proposal: 'applied' };
/** @param {Argus.DecisionKind} kind @param {string} prof @param {string} id @param {Argus.App.Applicable} item */
function applyTag(kind, prof, id, item) {
  const a = applyState(kind, prof, id, item).succ;
  if (!a) return '';
  if (a.rolled_back) return `<span class="tag t-unrev" data-tip="${esc(`applied as ledger entry ${a.ledger_entry}, rolled back by ${a.rolled_back}`)}">rolled back</span>`;
  return `<span class="tag t-${tok(a.outcome)}" data-tip="${esc(cut([a.ledger_entry ? `ledger entry ${a.ledger_entry}` : '', a.message || ''].filter(Boolean).join(' · '), 80))}">${a.outcome === 'staged' ? 'staged' : APPLIED[kind]}</span>`;
}
/** The apply result in full, for the opened change row (the tag's tooltip is cut). @param {Argus.DecisionKind} kind @param {string} prof @param {string} id @param {Argus.App.Applicable} item */
function applyLine(kind, prof, id, item) {
  const a = applyState(kind, prof, id, item).succ;
  if (!a) return '';
  return `<div class="lin"><span class="lk">apply</span>${esc([a.rolled_back ? `rolled back by ${a.rolled_back}` : a.outcome, a.ledger_entry ? `ledger entry ${a.ledger_entry}` : '', a.message || ''].filter(Boolean).join(' · '))}</div>`;
}
// the message's first 80 characters on hover; the dock row's ▸ shows it whole
/** @param {Argus.DecisionKind} kind @param {Argus.App.ApplyView} a */
function outcomeHtml(kind, a) {
  const tip = `data-tip="${esc(cut(a.message || '', 80))}"`;
  if (a.outcome === 'running') return '<span class="dim">applying…</span>';
  if (a.rolled_back) return `<span class="dim" ${tip}>applied as ${esc(a.ledger_entry)}, rolled back by ${esc(a.rolled_back)}</span>`;
  if (a.outcome === 'applied') return `<span class="add" ${tip}>${APPLIED[kind]} ✓</span>${a.ledger_entry ? ` <span class="dim">ledger ${esc(a.ledger_entry)}</span>` : ''}`;
  if (a.outcome === 'staged') return `<span class="w" ${tip}>staged: approve with /skills approve</span>`;
  return `<span class="e" ${tip}>${esc(a.outcome)}: ${esc(cut(a.message || '', 80))}</span>`;
}
/** @type {Argus.App.DockRow[]} */
let dockRows = [];
let dockText = '';
function dock() {
  /** @type {[string, Argus.Learning][]} */
  const rej = [];
  /** @type {Argus.Learning[]} */
  const keep = [];
  /** @type {[string, Argus.Proposal][]} */
  const acc = [];
  for (const [hk, h] of Object.entries(must(HS))) {
    for (const e of h.learnings) { const d = decisions[ekey(e.profile, e.id)]; if (d === 'reject') rej.push([hk, e]); else if (d === 'keep') keep.push(e); }
    for (const p of h.proposals || []) if (decisions[pkey(p.profile, p.id)] === 'accept') acc.push([hk, p]);
  }
  const el = must($('#dock'));
  if (!rej.length && !keep.length && !acc.length) { el.hidden = true; return; }
  /** @type {Set<string>} */
  const seen = new Set();
  // one row per command: what `copy` copies (as before) and what `apply` runs (`hermes -p P argus apply …`, via the backend)
  /** @type {Argus.App.DockRow[]} */
  const rows = rej.sort((a, b) => b[1].ts - a[1].ts).filter(([, e]) => !seen.has(e.profile + e.entry) && seen.add(e.profile + e.entry)).map(([hk, e]) => {
    const later = applyState('skill_edit', e.profile, e.entry, e).succ ? 0   // rolled back: its own rollback entry is not a later edit
      : (model(hk).groups[`${e.profile}:${e.skill}`] || []).filter(x => x.file === e.file && x.ts > e.ts && decisions[ekey(x.profile, x.id)] !== 'reject').length;
    const tenant = /^[0-9a-f]{40,}$/.test(e.profile), files = [...new Set(must(must(HS)[hk]).learnings.filter(x => x.entry === e.entry && x.profile === e.profile).map(x => x.file))];
    const note = `${e.skill} ${when(e.ts)}${later ? ` · ${pl(later, 'later edit', 'later edits')} to this file not rejected` : ''}`;
    return { kind: 'skill_edit', id: e.entry, prof: e.profile, item: e, can: !tenant, off: tenant ? 'tenant homes not verified' : '', label: note,
      cmd: tenant ? `# tenant ${e.profile.slice(0, 8)}: rollback command for tenant homes not verified` : `${hermesFor(e.profile)} curator rollback ${shq(e.entry)}`,
      note,
      cli: `hermes -p ${shq(e.profile)} argus apply --rollback ${shq(e.entry)}`,
      ask: { title: `roll back ${e.skill}`, tone: 'danger', ok: 'roll back',
        facts: [['profile', e.profile], ['ledger entry', e.entry], ['edit', when(e.ts)], [files.length === 1 ? 'file' : 'files', files.join('\n') || e.file]],
        notes: [`Restores ${files.length > 1 ? 'the files' : 'the file'} to the state before this edit, via Hermes rollback_entry as user.`, `Refused if ${files.length > 1 ? 'a file' : 'the file'} changed since.`,
          'A plain restore: the curator lock and write approval do not apply.', ...(later ? [`${pl(later, 'later edit', 'later edits')} to this file not rejected: the rollback is refused while they exist.`] : [])] } };
  });
  for (const [hk, p] of acc) {
    const name = p.skill.replace(/^new:/, '');
    const ask = p.skill.startsWith('new:') ? `Use skill_manage to create the skill ${name} with this rule: ${p.rule}` : `Use skill_manage to patch the skill ${name}: add this rule: ${p.rule}`;
    const plus = (p.diff || '').split('\n').filter(l => l.startsWith('+') && !l.startsWith('+++')).map(l => l.slice(1)).join('\n');
    const off = p.skill_gone ? `skill ${name} was removed or renamed after this proposal: dismiss it, or recreate the skill and re-run derive`
      : p.read_only ? `skill ${name} is read-only in this profile (a shared skills.external_dirs): add the rule where that skill is maintained` : '';
    rows.push({ kind: 'proposal', id: p.id, prof: p.profile, item: p, can: !off, off, label: `${name} · ${cut(p.rule, 90)}`,
      cmd: `${hermesFor(p.profile || 'default')} -z ${shq(ask)}`, note: `${hk} · proposal ${p.id}`,
      cli: `hermes -p ${shq(p.profile)} argus apply --proposal ${shq(p.id)}`,
      ask: p.skill.startsWith('new:')
        ? { title: `create skill ${name}`, tone: 'go', ok: 'create skill', facts: [['profile', p.profile], ['skill', `${name} (new)`], ['confidence', p.confidence || '-']],
          preview: { label: 'SKILL.md', text: newSkill(name, p.rule), diff: false },
          notes: ['Created with skill_manage as user; the description is the rule\'s first sentence, cut to the 60-character skill index. Edit it later if needed.',
            `Refused if a skill named ${name} exists by then.`, 'Staged for /skills approve (not written yet) if skills.write_approval is on.'] }
        : { title: `patch ${name}`, tone: 'go', ok: 'apply patch', facts: [['profile', p.profile], ['file', p.file || name], ['change', `+${p.plus || plus.split('\n').filter(Boolean).length} −${p.minus || 0}`], ['confidence', p.confidence || '-']],
          ...(p.diff ? { preview: { label: 'diff', text: p.diff, diff: true } } : {}),
          notes: ['Patched with skill_manage as user.', 'Refused if the file changed since the proposal, or the skill is locked.', 'Staged for /skills approve (not written yet) if skills.write_approval is on.'] } });
  }
  dockRows = rows;
  /** carried out already (rolled back / applied, not undone since): listed apart, folded @type {(r: Argus.App.DockRow) => boolean} */
  const isDone = r => carried(r.kind, r.prof, r.id, r.item);
  /** an accepted proposal for a skill no longer in the profile: nothing left to run @type {(r: Argus.App.DockRow) => boolean} */
  const isObs = r => r.kind === 'proposal' && 'skill_gone' in r.item && r.item.skill_gone && !isDone(r);
  const indexed = rows.map((r, k) => /** @type {[Argus.App.DockRow, number]} */ ([r, k]));
  const todo = indexed.filter(([r]) => !isDone(r) && !isObs(r)), done = indexed.filter(([r]) => isDone(r)), obs = indexed.filter(([r]) => isObs(r));
  const rb = todo.filter(([r]) => r.kind === 'skill_edit').map(([r]) => r), ac = todo.filter(([r]) => r.kind === 'proposal').map(([r]) => r);
  /** One script line: the command, then its note as a comment that cannot spill onto a new line. @type {(r: Argus.App.DockRow) => string} */
  const line = r => `${r.cmd}   # ${r.note.replace(/\s+/g, ' ')}`;
  dockText = [...(rb.length ? ['# reject applied edits (rollback), newest first', ...rb.map(line)] : []),
    ...(ac.length ? ['# accept proposed edits', ...ac.map(line)] : [])].join('\n') || '# nothing left to run';
  /** @type {(r: Argus.App.DockRow, k: number) => string} */
  const act = (r, k) => {
    const { succ, last } = applyState(r.kind, r.prof, r.id, r.item);
    if (last?.outcome === 'running') return `${outcomeHtml(r.kind, last)}<button class="btn" data-copyrow="${k}">copy</button>`;
    const done = succ && !succ.rolled_back;
    const shown = done ? outcomeHtml(r.kind, succ) : `${succ ? outcomeHtml(r.kind, succ) : ''}${last && !OK(last) ? outcomeHtml(r.kind, last) : ''}`;
    return `${shown}<button class="btn" data-copyrow="${k}">copy</button>${done ? '' : r.can ? `<button class="btn apply" data-apply="${k}">apply</button>`
      : `<span class="${r.kind === 'proposal' ? 'w' : 'dim'}" data-tip="${esc(r.off)}">${r.kind !== 'proposal' ? 'copy only' : 'read_only' in r.item && r.item.read_only ? 'read-only' : 'skill removed'}</span>`}`; };
  // one line per command (verb, what, outcome, copy, apply); the command itself, its note and the terminal equivalent open with ▸
  /** @type {(r: Argus.App.DockRow, k: number) => string} */
  const row = (r, k) => { const x = `dock:${r.kind}:${r.prof}:${r.id}`, { succ, last } = applyState(r.kind, r.prof, r.id, r.item);
    const msgs = [...new Set([succ, last].filter(a => a && a.message).map(a => `# ${a?.outcome}: ${a?.message}`))];
    return `<div class="crow ${xo(x)}" data-xrow>${xbtn(x, xid(x))}<span class="${r.kind === 'proposal' ? 'add' : 'e'}">${r.kind === 'proposal' ? 'accept' : 'rollback'}</span><span class="cl">${esc(r.label)}</span><span class="cact">${act(r, k)}</span>
      <div class="xb" id="${xid(x)}"><code>${esc(r.cmd)}</code>${r.note !== r.label ? `<div class="dim"># ${esc(r.note)}</div>` : ''}<div class="dim"># in a terminal (profiles with the plugin enabled): ${esc(r.cli)}</div>${msgs.map(s => `<div class="dim">${esc(s)}</div>`).join('')}</div></div>`; };
  const open = sessionStorage.getItem('argus-dock') === 'open';
  /** a folded group at the bottom of the dock @type {(id: string, label: string, note: string, list: [Argus.App.DockRow, number][]) => string} */
  const grp = (id, label, note, list) => { if (!list.length) return ''; const on = sessionStorage.getItem('argus-dock-' + id) === 'open';
    return `<div class="dgrp"><button class="lnk" type="button" data-dockgrp="${id}" aria-expanded="${on}">${on ? '▾' : '▸'} ${label} · ${list.length}</button><span class="dim"> ${note}</span></div>${on ? list.map(([r, k]) => row(r, k)).join('') : ''}`; };
  const doneHtml = grp('obsolete', 'obsolete', 'accepted, but the skill is no longer in the profile: nothing to run', obs)
    + grp('done', 'carried out', 'applied or rolled back; kept as the record of the decision', done);
  el.hidden = false;
  el.innerHTML = `<div class="in"><div class="dsum"><span class="e">${rej.length} rejected</span> · <span class="add">${keep.length} kept</span> · <span class="add">${acc.length} accepted</span>
    <button class="lnk" type="button" data-dock="1" aria-expanded="${open}" ${open ? 'aria-controls="cmds"' : ''}>${open ? '▾' : '▸'} ${pl(todo.length, 'command', 'commands')}${done.length ? ` <span class="dim">+ ${done.length} done</span>` : ''}${obs.length ? ` <span class="dim">+ ${obs.length} obsolete</span>` : ''}</button><span class="spacer"></span>
    ${open ? '<button class="btn" data-copy="1">copy all</button><button class="btn" data-export="1">export json</button><button class="btn" data-clear="1">clear</button>' : ''}${hintBtn('dock')}</div>
    ${hintBody('dock', '<p>Decisions are saved in the plugin DB of each profile; apply runs only when you click it. Rollbacks of rejected edits first (newest first), then accepted proposals; the ones already carried out fold at the bottom. copy all = every command still to run, as a script; ▸ shows one command and its terminal equivalent.</p>')}
    ${open ? `<div id="cmds" class="cmds">${todo.length ? todo.map(([r, k]) => row(r, k)).join('') : '<div class="dh"># nothing left to run</div>'}${doneHtml}</div>` : ''}</div>`;
}
/** The SKILL.md that apply creates for a new-skill proposal: mirrors argus/apply.py new_skill.
 * @type {(name: string, rule: string) => string} */
const newSkill = (name, rule) => {
  const flat = rule.split(/\s+/).filter(Boolean).join(' '), m = /[.!?](?= )/.exec(flat);
  let desc = m ? flat.slice(0, m.index + 1) : flat;
  if (desc.length > 60) desc = desc.slice(0, 59).replace(/ [^ ]*$/, '').replace(/[ ,;:]+$/, '') + '…';
  return `---\nname: ${name}\ndescription: ${JSON.stringify(desc)}\n---\n\n# ${name}\n\n${rule.trim()}\n`;
};
/** Ask before a change, in the page's own dialog; resolves true only on the confirm button.
 * @type {(a: Argus.App.Ask) => Promise<boolean>} */
function confirmAsk(a) {
  const d = document.createElement('dialog');
  d.className = `ask ${a.tone}`;
  d.setAttribute('aria-labelledby', 'ask-t');
  d.innerHTML = `<form method="dialog">
    <header><span class="ask-k">${a.tone === 'danger' ? 'confirm' : 'apply'}</span><h3 id="ask-t">${esc(a.title)}</h3></header>
    <dl>${a.facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>
    ${a.preview ? `<div class="ask-pl">${esc(a.preview.label)}</div><pre class="diff">${a.preview.diff ? diffHtml(a.preview.text) : esc(a.preview.text)}</pre>` : ''}
    <ul>${a.notes.map(n => `<li>${esc(n)}</li>`).join('')}</ul>
    <footer><button class="btn" value="cancel" autofocus>cancel</button><button class="btn ok" value="ok">${esc(a.ok)}</button></footer></form>`;
  document.body.append(d);
  return new Promise(res => {
    // settle on the first of: a button (form submit), Esc (cancel), a click on the backdrop, close.
    // Not on `close` alone: browsers queue it as a task, which a hidden page may never run.
    let done = false;
    /** @param {boolean} ok */
    const end = ok => { if (done) return; done = true; if (d.open) d.close(); d.remove(); res(ok); };
    d.addEventListener('submit', e => { e.preventDefault(); end(e instanceof SubmitEvent && e.submitter instanceof HTMLButtonElement && e.submitter.value === 'ok'); });
    d.addEventListener('cancel', e => { e.preventDefault(); end(false); });
    d.addEventListener('click', e => { if (e.target === d) end(false); });
    d.addEventListener('close', () => end(false));
    d.showModal();
  });
}
/** @param {Argus.App.DockRow} r */
async function applyRow(r) {
  const key = `${r.kind}|${r.prof}|${r.id}`;
  if (applying[key]?.outcome === 'running' || !(await confirmAsk(r.ask))) return;
  applying[key] = { outcome: 'running' };
  dock();
  try {
    await decChain.catch(() => {});   // the decision just clicked must be saved before the API checks it
    // the /apply response (apply.ApplyResult + profile), as the backend types it
    applying[key] = /** @type {Argus.ApplyResult} */ (await api('/api/plugins/argus/apply', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile: r.prof, target_kind: r.kind, target_id: r.id }) }));
  } catch (err) {
    applying[key] = { outcome: 'failed', message: String(errMsg(err)) };
    showError(`apply failed: ${errMsg(err)}`);
  }
  dropModels();   // model() reads `applying` (carried): show the result now, not only after the re-fetch
  const y = scrollY; render(); scrollTo(0, y);
  load();   // the ledger, the skill and apply_log changed: re-fetch
}
/** @type {(k: string, v: string | undefined) => void} */
const setDec = (k, v) => { if (v) decisions[k] = v; else delete decisions[k]; };
// one request at a time, in click order, so the server ends in the state of the last click
/** @type {Promise<unknown>} */
let decChain = Promise.resolve();
/** @type {(body: Argus.DecisionBody) => Promise<unknown>} */
const postDecision = body => (decChain = decChain.catch(() => {}).then(() => api('/api/plugins/argus/decision',
  { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })));
// what a click decides on: the request body (without action) and the UI ids that share the decision.
// A skill-edit decision targets its ledger entry (rollback is per entry), i.e. every file of the entry.
/** @param {Argus.DecisionKind} kind @param {string | undefined} id @param {string | undefined} prof @returns {Argus.App.DecisionTarget | null} */
function decTarget(kind, id, prof) {
  for (const h of Object.values(must(HS))) {
    if (kind === 'proposal') {
      const p = (h.proposals || []).find(x => x.id === id && x.profile === prof);
      if (p) return { ids: [pkey(p.profile, p.id)], body: { target_kind: 'proposal', target_id: p.id, profile: p.profile,
        snapshot: { skill: p.skill, rule: p.rule, why: p.why, confidence: p.confidence, file: p.file, diff: p.diff } } };
    } else {
      const e = h.learnings.find(x => x.id === id && x.profile === prof);
      if (e) return { ids: h.learnings.filter(x => x.entry === e.entry && x.profile === e.profile).map(x => ekey(x.profile, x.id)),
        body: { target_kind: 'skill_edit', target_id: e.entry, profile: e.profile,
          snapshot: { entry: e.entry, skill: e.skill, file: e.file, action: e.action, actor: e.actor, evidence: evOf(e), learned: e.review?.learned || '', diff: e.diff } } };
    }
  }
  return null;
}
// optimistic: show the click at once, roll back (unless clicked again meanwhile) and say so if the server refuses
/** @param {Argus.DecisionKind} kind @param {string | undefined} id @param {string | undefined} prof @param {string | undefined} value @param {string} key @param {(ids: string[]) => void} repaint */
function decide(kind, id, prof, value, key, repaint) {
  const tg = decTarget(kind, id, prof);
  if (!tg) { showError(`decision target not found: ${id}`); return; }
  const next = decisions[key] === value ? undefined : value, prev = Object.fromEntries(tg.ids.map(k => [k, decisions[k]]));
  tg.ids.forEach(k => setDec(k, next));
  repaint(tg.ids);
  postDecision({ ...tg.body, action: next || 'clear' }).catch((/** @type {unknown} */ err) => {
    tg.ids.forEach(k => { if (decisions[k] === next) setDec(k, prev[k]); });
    repaint(tg.ids);
    showError(`could not save the decision: ${errMsg(err)}`);
  });
}
async function clearDecisions() {
  /** @type {Map<string, Argus.DecisionBody>} */
  const targets = new Map([]);
  for (const h of Object.values(must(HS))) {
    for (const e of h.learnings) if (decisions[ekey(e.profile, e.id)]) targets.set(`s|${e.profile}|${e.entry}`, { target_kind: 'skill_edit', target_id: e.entry, profile: e.profile, action: 'clear' });
    for (const p of h.proposals || []) if (decisions[pkey(p.profile, p.id)]) targets.set(`p|${p.profile}|${p.id}`, { target_kind: 'proposal', target_id: p.id, profile: p.profile, action: 'clear' });
  }
  for (const k of Object.keys(decisions)) delete decisions[k];
  render();
  const bad = (await Promise.allSettled([...targets.values()].map(postDecision))).filter(r => r.status === 'rejected');
  if (bad.length) {
    /** @type {unknown} */
    const reason = must(bad[0]).reason;
    showError(`could not clear ${pl(bad.length, 'decision', 'decisions')}: ${errMsg(reason)}`); load(); }
}
/** @param {string} msg @param {string} [detail] */
function showError(msg, detail) {
  let el = $('#errbox');
  if (!el) {
    el = document.createElement('div');
    el.id = 'errbox'; el.className = 'callout e'; el.setAttribute('role', 'alert');
    el.style.cssText = 'position:fixed;top:12px;right:12px;z-index:1000;max-width:560px;margin:0';
    document.body.append(el);
  }
  el.innerHTML = `<b>error</b>${esc(msg)} <button class="btn" data-errclose="1">close</button>${detail ? `<pre style="white-space:pre-wrap;max-height:240px;overflow:auto;margin:8px 0 0">${esc(detail)}</pre>` : ''}`;
  must($('[data-errclose]', el)).onclick = () => { el.hidden = true; };
  el.hidden = false;
}

// ---------------------------------------------------------------- router
function render() {
  if (!HS) return;
  const [path, query] = location.hash.split('?');
  const [, hkRaw, tabRaw, a, b] = must(path).split('/');
  const qs = new URLSearchParams(query || '');
  const hkKey = hkRaw === undefined ? undefined : unpart(hkRaw);
  const hk = hkKey !== undefined && HS[hkKey] ? hkKey : firstHarness(), tab = tabRaw !== undefined && ['overview', 'intents', 'skills'].includes(tabRaw) ? tabRaw : 'overview';
  const m = model(hk), nq = queue(hk).length;
  must($('#tip')).hidden = true;   // the hovered element is gone after a re-render
  const nsk = Object.values(m.groups).filter(es => es.some(e => !CLEANUP.has(e.action))).length;
  const review = Object.entries(m.h.trends).some(([k, t]) => m.groups[k] && t.verdict === 'needs review');
  must($('#harnesses')).innerHTML = Object.keys(HS).length > 1 ? Object.entries(HS).map(([k, h]) => `<a class="${k === hk ? 'on' : ''}" href="#/${upart(k)}/${tab}">${esc(h.label)}</a>`).join('') : '';
  must($('#tabs')).innerHTML = [['overview', nq, true], ['intents', m.visible.length, false], ['skills', nsk, review]]
    .map(([k, n, w]) => `<a class="${k === tab ? 'on' : ''}" href="#/${upart(hk)}/${k}">${k}<span class="n ${w && n ? 'w' : ''}">${n || ''}</span></a>`).join('');
  const ps = Object.keys(must(HS[hk]).profiles || {});
  must($('#pf')).innerHTML = ps.length > 1 ? `<label class="sel"><span class="dim">profile</span><select id="pfsel">${[['all', 'all'], ...ps.map(p => [p, must(must(HS)[hk]).profiles[p]])].map(([k, l]) => `<option value="${esc(k)}" ${m.pf === k ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select></label>` : '';
  $('#pfsel')?.addEventListener('change', ev => { if (!(ev.target instanceof HTMLSelectElement)) return; sessionStorage.setItem(pfKey(hk), ev.target.value); const y = scrollY; render(); scrollTo(0, y); });
  must($('#view')).innerHTML = tab === 'intents' ? (a ? viewIntent(hk, unpart(a), qs.get('skill')) : viewIntents(hk, qs.get('who'))) : tab === 'skills' ? viewSkills(hk, a && unpart(a), b) : viewOverview(hk, qs.get('sig'));
  dock();
  const target = qs.get('sig') ? document.getElementById('sig-' + qs.get('sig')) : null;
  for (let d = target?.parentElement?.closest('details') || null; d; d = d.parentElement?.closest('details') || null) d.open = true;   // a row in the folded remainder
  document.querySelectorAll('.scrollbox').forEach(markScrollEnd);
  if (target) setTimeout(() => target.scrollIntoView({ block: 'center' }), 20);
  const pair = qs.get('skill') && document.getElementById('pair');
  if (pair) setTimeout(() => { pair.scrollIntoView({ block: 'start' }); scrollBy(0, -120); }, 20);
}
/** @param {Element} el */
function markScrollEnd(el) { el.classList.toggle('at-end', el.scrollTop + el.clientHeight >= el.scrollHeight - 2); }
/** The element (an HTML or SVG element: the ones with data-* attributes) a document listener acts on. @type {(el: Element | null) => HTMLElement | SVGElement | null} */
const dataEl = el => el instanceof HTMLElement || el instanceof SVGElement ? el : null;
document.addEventListener('scroll', e => { if (e.target instanceof Element && e.target.classList.contains('scrollbox')) markScrollEnd(e.target); }, true);
document.addEventListener('toggle', e => {
  if (!(e.target instanceof HTMLDetailsElement)) return;
  const d = e.target, k = d.dataset.k;
  if (k) { if (d.open) OPEN.add(k); else OPEN.delete(k); }
  const b = d.closest('.scrollbox'); if (b) markScrollEnd(b);
  if (d.open) d.querySelectorAll('.scrollbox').forEach(markScrollEnd);
}, true);
document.addEventListener('click', e => {
  if (!(e.target instanceof Element)) return;
  const t = dataEl(e.target.closest('[data-x],[data-hint],[data-dec],[data-pdec],[data-dock],[data-dockgrp],[data-clear],[data-goto],[data-expand],[data-copy],[data-copyrow],[data-apply],[data-export],[data-href],a[href^="#t-"]'));
  if (!t) return;
  // disclosures change the page in place (no render): a row's ▸/▾, a section's '?'
  if (t.dataset.x !== undefined) { const k = t.dataset.x, on = !OPEN.has(k); if (on) OPEN.add(k); else OPEN.delete(k);
    t.setAttribute('aria-expanded', String(on)); t.textContent = on ? '▾' : '▸'; t.closest('[data-xrow]')?.classList.toggle('xo', on); return; }
  if (t.dataset.hint !== undefined) { const k = 'hint:' + t.dataset.hint, on = !OPEN.has(k); if (on) OPEN.add(k); else OPEN.delete(k);
    t.setAttribute('aria-expanded', String(on)); document.getElementById('hb-' + t.dataset.hint)?.classList.toggle('on', on); return; }
  if (t.dataset.pdec) { decide('proposal', t.dataset.id, t.dataset.prof, t.dataset.pdec, pkey(t.dataset.prof, t.dataset.id), () => { const y = scrollY; render(); scrollTo(0, y); }); return; }
  if (t.matches('a[href^="#t-"]')) { e.preventDefault(); const el = document.getElementById(must(t.getAttribute('href')).slice(1)); if (el) { el.closest('details')?.setAttribute('open', ''); el.scrollIntoView({ block: 'center' }); el.classList.add('flash'); setTimeout(() => el.classList.remove('flash'), 1200); } return; }
  if (t.dataset.dock) { sessionStorage.setItem('argus-dock', sessionStorage.getItem('argus-dock') === 'open' ? 'closed' : 'open'); dock(); return; }
  if (t.dataset.dockgrp) { const k = 'argus-dock-' + t.dataset.dockgrp; sessionStorage.setItem(k, sessionStorage.getItem(k) === 'open' ? 'closed' : 'open'); dock(); return; }
  if (t.dataset.href) { location.hash = t.dataset.href; return; }
  if (t.dataset.dec) decide('skill_edit', t.dataset.id, t.dataset.prof, t.dataset.dec, ekey(t.dataset.prof, t.dataset.id), () => {
    document.querySelectorAll('[data-dec]').forEach(b => { const d = dataEl(b); if (d) d.classList.toggle('on', decisions[ekey(d.dataset.prof, d.dataset.id)] === d.dataset.dec); });
    dock(); });
  if (t.dataset.pf) { const raw = location.hash.split('/')[1]?.split('?')[0], hk = raw ? unpart(raw) : firstHarness(); sessionStorage.setItem(pfKey(hk), t.dataset.pf); const y = scrollY; render(); scrollTo(0, y); }
  if (t.dataset.clear) { const n = Object.keys(decisions).length;
    confirmAsk({ title: 'clear all decisions', tone: 'danger', ok: 'clear', facts: [['decisions', String(n)]],
      notes: ['Removes every keep, reject, accept and dismiss from the plugin database of each profile.', 'Changes already applied stay: roll them back from their skill page.'] })
      .then(ok => { if (ok) clearDecisions(); }); }
  if (t.dataset.copy) { navigator.clipboard?.writeText(dockText); t.textContent = 'copied'; }
  if (t.dataset.copyrow !== undefined) { navigator.clipboard?.writeText(must(dockRows[+t.dataset.copyrow]).cmd); t.textContent = 'copied'; }
  if (t.dataset.apply !== undefined) applyRow(must(dockRows[+t.dataset.apply]));
  if (t.dataset.export) { const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([JSON.stringify(decisions, null, 1)], { type: 'application/json' })); a.download = 'skill-decisions.json'; a.click(); }
  if (t.dataset.expand !== undefined) t.classList.toggle('clip');
  if (t.dataset.goto) { const [, hk, , k] = location.hash.split('/'); if (!location.hash.startsWith(`#/${upart(hk)}/skills/${k}`) || location.hash.endsWith('/content')) location.hash = `#/${upart(hk)}/skills/${k}/activity`;
    setTimeout(() => { const el = document.getElementById('e-' + t.dataset.goto); if (el) { el.classList.add('target');
      for (let d = el.closest('details'); d; d = d.parentElement?.closest('details') || null) d.open = true;   // the edit, and a folded "N more" around it
      el.querySelector('details')?.setAttribute('open', ''); el.scrollIntoView({ block: 'center' }); } }, 40); }
});
const tipEl = must($('#tip'));
document.addEventListener('mousemove', e => {
  const t = dataEl(e.target instanceof Element ? e.target.closest('[data-tip]') : null);
  if (!t || !t.dataset.tip) { tipEl.hidden = true; return; }
  tipEl.textContent = t.dataset.tip; tipEl.hidden = false;
  tipEl.style.left = Math.min(e.clientX + 14, innerWidth - 480) + 'px'; tipEl.style.top = Math.min(e.clientY + 16, innerHeight - 140) + 'px';
});
addEventListener('hashchange', () => { render(); if (!location.hash.includes('sig=')) scrollTo(0, 0); });

// ---------------------------------------------------------------- loading, errors, refresh
/** A log or error list in the header: a short label that opens to the full text (tooltips stay short). @type {(label: string, text: string) => string} */
const runlog = (label, text) => `<details class="runlog"><summary>${esc(label)}</summary><pre>${esc(text)}</pre></details>`;
/** @param {'running' | 'idle'} state @param {string} [title] @param {string} [log] */
function setRefresh(state, title, log) {
  const el = must($('#refresh'));
  el.innerHTML = `${log ? runlog('log', log) : ''}<button class="btn" data-refresh="1" ${state === 'running' ? 'disabled' : ''} data-tip="${esc(cut(title || 'hermes argus derive, then reload', 80))}">${state === 'running' ? 'deriving…' : 'refresh'}</button>`;
}
async function load() {
  if (!D) must($('#view')).innerHTML = '<div class="empty">loading…</div>';
  try {
    // the export (argus/exporttypes.py Export), as the backend types it
    D = /** @type {Argus.Export} */ (await api('/api/plugins/argus/data'));
    HS = D.harnesses || {};
    dropModels();
    for (const k of Object.keys(decisions)) delete decisions[k];
    for (const k of Object.keys(applying)) if (applying[k]?.outcome !== 'running') delete applying[k];   // the export has them (apply_log)
    for (const h of Object.values(HS)) Object.assign(decisions, h.decisions || {});
    if (!Object.keys(HS).length) { must($('#view')).innerHTML = '<div class="empty">no Hermes profile found</div>'; return; }
    const errs = Object.entries(D.meta?.errors || {});
    must($('#gen')).innerHTML = `${D.meta?.generated ? `data ${esc(D.meta.generated)}` : ''}${errs.length ? ' ' + runlog(pl(errs.length, 'profile error', 'profile errors'), errs.map(([p, m]) => `${p}: ${m}`).join('\n')) : ''}`;
    render();
  } catch (err) {
    must($('#view')).innerHTML = `<div class="callout e"><b>error</b> could not load Argus data: ${esc(errMsg(err))} <button class="btn" data-retry="1">retry</button></div>`;
  }
}
/** @type {(f: Argus.RunError) => string} */
const failNote = f => `last derivation failed (${f.profile}, exit ${f.exit_code}, ${when(f.at)})`;
async function pollDerive() {
  try {
    // the /status payload (argus/exporttypes.py Status), as the backend types it
    const s = /** @type {Argus.Status} */ (await api('/api/plugins/argus/status'));
    if (s.running) { setRefresh('running'); setTimeout(pollDerive, 10000); return; }
    const f = s.last_run_error, failed = f && (!s.last_derive || f.at >= s.last_derive.ts) ? f : null;
    setRefresh('idle', failed ? failNote(failed) : s.last_derive ? `last derivation ${when(s.last_derive.ts)} (${s.last_derive.profile})` : '', failed?.log);
  } catch { setRefresh('idle'); return; }
}
document.addEventListener('click', async e => {
  const el = e.target instanceof Element ? e.target : null;
  if (el?.closest('[data-retry]')) { load(); return; }
  if (!el?.closest('[data-refresh]')) return;
  setRefresh('running');
  const t0 = Math.floor(Date.now() / 1000) - 1;
  try { await api('/api/plugins/argus/refresh', { method: 'POST' }); } catch (err) {
    setRefresh('idle', `refresh failed: ${errMsg(err)}`); showError(`refresh failed: ${errMsg(err)}`); return; }
  const wait = async () => { const s = /** @type {Partial<Argus.Status>} */ (await api('/api/plugins/argus/status').catch(() => ({ running: false })));
    if (s.running) { setTimeout(wait, 10000); return; }
    const f = s.last_run_error;
    if (f && f.at >= t0) { setRefresh('idle', failNote(f), f.log); showError(failNote(f), f.log); return; }
    setRefresh('idle'); load(); };
  setTimeout(wait, 3000);
});
setRefresh('idle');
pollDerive();
load();
})();
