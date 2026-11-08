/* Agent Bullpen — dashboard front end. Plain JavaScript, no build step.
 *
 * Routes (the part after # in the address bar):
 *   #/             overview: machines and recent jobs, "Add a machine"
 *   #/task         send one prompt to one agent
 *   #/council      debate or build+review across several agents
 *   #/history      every job, a page at a time (?runner=&agent=&kind=&status=&q=)
 *   #/job/123      follow a job or council
 *   #/machine/x    one machine: runner, agents, projects, its recent work
 *   #/memory       shared notes
 *   #/mcp          the central MCP server list
 *   #/usage        Claude plan limits, tokens per agent, Gemini quota, API spend and links
 */
'use strict';

const view = document.getElementById('view');
let state = { runners: [], jobs: [], admin_jobs: [], links: [] };
let timer = null;

// Small conveniences remembered by this browser only (list or cards, folded sections). Storage
// can be missing or blocked (private windows), so every read and write is allowed to fail.
const prefs = {
  get(key, fallback) {
    try { const v = localStorage.getItem(`bullpen.${key}`); return v === null ? fallback : JSON.parse(v); } catch (e) { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(`bullpen.${key}`, JSON.stringify(value)); } catch (e) { /* not remembered, that's all */ }
  },
};

// ------------------------------------------------------------------ helpers
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function ago(ts) {
  if (!ts) return '—';
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function duration(job) {
  if (!job.started_at) return '';
  const end = job.finished_at || Date.now() / 1000;
  const s = Math.round(end - job.started_at);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

const statusChip = (s) => `<span class="status ${esc(s)}">${esc(s)}</span>`;
// A job waiting for you to answer one of its questions (Allow or Deny on its page).
const waitingChip = (j) => (j.waiting_approval ? ' <span class="status warn">waiting for you</span>' : '');

// Characters that don't show on screen but change what text means or does: control characters
// (except newline and tab), bidi controls, zero-width and other invisible characters. visible()
// writes each one as a highlighted code (\r, \x1b, \u202e), so you see exactly what would run.
const HIDDEN_RE = /[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u2028-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufffb\u{1bca0}-\u{1bca3}\u{1d173}-\u{1d17a}\u{e0000}-\u{e0fff}]/gu;
function hiddenCode(ch) {
  if (ch === '\r') return '\\r';
  const cp = ch.codePointAt(0);
  if (cp <= 0xff) return `\\x${cp.toString(16).padStart(2, '0')}`;
  if (cp <= 0xffff) return `\\u${cp.toString(16).padStart(4, '0')}`;
  return `\\u{${cp.toString(16)}}`;
}
// Escaped HTML for text, hidden characters made visible. `seen.hidden` becomes true when there were any.
function visible(text, seen) {
  const s = String(text ?? '');
  let out = ''; let last = 0;
  for (const m of s.matchAll(HIDDEN_RE)) {
    const cp = m[0].codePointAt(0).toString(16).toUpperCase().padStart(4, '0');
    out += `${esc(s.slice(last, m.index))}<span class="hc" title="A hidden character (U+${cp})">${esc(hiddenCode(m[0]))}</span>`;
    last = m.index + m[0].length;
    if (seen) seen.hidden = true;
  }
  return out + esc(s.slice(last));
}

// Each visit to a page has a number. A page's own reads (GET) that come back after you moved to
// another page are dropped (StaleNav), so a slow answer can never draw the old page over the new one.
// Actions (saves, sign-ins, ...) are never dropped.
let navGen = 0;
class StaleNav extends Error { constructor() { super(''); this.name = 'StaleNav'; } }

async function api(path, opts = {}) {
  const gen = navGen;
  const reading = !opts.method || opts.method === 'GET';
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (reading && gen !== navGen) throw new StaleNav();
  if (res.status === 401) { showLogin(); throw new Error('Not logged in'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)) : `HTTP ${res.status}`);
  return data;
}

// Polling: one loop at a time. Each loop has a generation number, so a loop that was stopped
// (or replaced by a newer one) never schedules itself again, even if it was mid-request.
let timerGen = 0;
function stopTimer() { timerGen += 1; if (timer) { clearTimeout(timer); timer = null; } }
function every(ms, fn) {
  stopTimer();
  const gen = timerGen;
  const tick = async () => {
    if (gen !== timerGen) return;
    try { await fn(); } catch (e) { /* keep polling */ }
    if (gen === timerGen) timer = setTimeout(tick, ms);
  };
  timer = setTimeout(tick, ms);
}
const vnum = (v) => String(v || '0').split('.').slice(0, 3).map((x) => parseInt(x, 10) || 0);
const vless = (a, b) => { const x = vnum(a); const y = vnum(b); for (let i = 0; i < 3; i += 1) { if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) < (y[i] || 0); } return false; };

// -------------------------------------------------------- tiny markdown
function inline(s) {
  return String(s).split(/(`[^`]+`)/).map((part) => {
    if (part.length > 1 && part.startsWith('`') && part.endsWith('`')) return `<code>${esc(part.slice(1, -1))}</code>`;
    let t = esc(part);
    t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    t = t.replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\w)/g, '$1<em>$2</em>');
    t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    return t;
  }).join('');
}

function md(src) {
  const lines = String(src ?? '').replace(/\r\n/g, '\n').split('\n');
  const out = []; let para = []; let list = null; let code = null; let table = [];
  const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join(' '))}</p>`); para = []; } };
  const flushList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const flushTable = () => {
    if (!table.length) return;
    const rows = table.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r)).map((r) => r.trim().replace(/^\||\|$/g, '').split('|'));
    out.push('<div class="table-wrap"><table>' + rows.map((cells, i) => '<tr>' + cells.map((c) => (i === 0 ? `<th>${inline(c.trim())}</th>` : `<td>${inline(c.trim())}</td>`)).join('') + '</tr>').join('') + '</table></div>');
    table = [];
  };
  const flushAll = () => { flushPara(); flushList(); flushTable(); };
  for (const line of lines) {
    if (line.trim().startsWith('```')) {
      if (code) { out.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`); code = null; } else { flushAll(); code = []; }
      continue;
    }
    if (code) { code.push(line); continue; }
    let m;
    if (/^\s*\|.*\|\s*$/.test(line)) { flushPara(); flushList(); table.push(line); continue; }
    flushTable();
    if (!line.trim()) { flushPara(); flushList(); continue; }
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) { flushAll(); const n = Math.min(m[1].length + 1, 5); out.push(`<h${n}>${inline(m[2])}</h${n}>`); continue; }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { flushAll(); out.push('<hr>'); continue; }
    if ((m = line.match(/^\s*[-*+]\s+(.*)$/))) { flushPara(); if (list !== 'ul') { flushList(); out.push('<ul>'); list = 'ul'; } out.push(`<li>${inline(m[1])}</li>`); continue; }
    if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { flushPara(); if (list !== 'ol') { flushList(); out.push('<ol>'); list = 'ol'; } out.push(`<li>${inline(m[1])}</li>`); continue; }
    if ((m = line.match(/^>\s?(.*)$/))) { flushAll(); out.push(`<blockquote>${inline(m[1])}</blockquote>`); continue; }
    flushList(); para.push(line);
  }
  if (code) out.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`);
  flushAll();
  return out.join('\n');
}

// ------------------------------------------------------------------ login
const loginDialog = document.getElementById('login');
function showLogin() { if (!loginDialog.open) loginDialog.showModal(); document.getElementById('login-password').focus(); }
document.getElementById('login-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const err = document.getElementById('login-error');
  err.hidden = true;
  try {
    await api('/api/login', { method: 'POST', body: { password: document.getElementById('login-password').value } });
    loginDialog.close();
    route();
  } catch (e) { err.textContent = e.message; err.hidden = false; }
});
document.getElementById('logout').addEventListener('click', async () => { await api('/api/logout', { method: 'POST' }); showLogin(); });

async function loadState() {
  state = await api('/api/state');
  document.getElementById('logout').hidden = !state.password_enabled;
  return state;
}

// ---------------------------------------------------------------- machines
const outdated = (r) => state.runner_latest && r.version && vless(r.version, state.runner_latest);
const busyOf = (r) => (r.running || []).length;
const slotsOf = (r) => Math.max(1, Number(r.max_parallel) || 1);
const byOnlineThenName = (a, b) => (Number(b.online) - Number(a.online)) || String(a.name).localeCompare(String(b.name));
const sortedRunners = () => [...state.runners].sort(byOnlineThenName);
const clashOf = (r) => String((r.info || {}).name_conflict || '');
const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || `${one}s`)}`;

// Does a machine match a filter such as "win gemini"? Every word must appear in its name,
// host name, system, agents or tags.
function machineMatches(r, filter) {
  const words = String(filter || '').toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const hay = [r.name, r.hostname, r.os, ...(r.agents || []).map((a) => `${a.name} ${a.label || ''}`),
    ...(Array.isArray(r.tags) ? r.tags : [])].join(' ').toLowerCase();
  return words.every((w) => hay.includes(w));
}

// How a machine signs in to the hub: its own token, or the hub's shared one.
function authBadge(r) {
  if (r.auth === 'machine') return '<span class="chip" title="It signs in to the hub with its own token. Forget revokes it.">own token</span>';
  if (r.auth === 'shared') return '<span class="chip" title="It uses the hub\'s shared runner token. Open the machine to give it its own.">shared token</span>';
  return '';
}

const agentChips = (r) => (r.agents || []).map((a) => `<span class="chip ${a.ready ? 'ok' : 'warn'}" title="${esc(a.note || 'ready')}">${esc(a.name)}${a.ready ? '' : ' ⚠'}</span>`).join('')
  || '<span class="muted small">no agents</span>';
const machineState = (r) => (r.online ? 'online' : `offline · ${ago(r.last_seen)}`);
const machineLink = (r) => `<a href="#/machine/${encodeURIComponent(r.name)}">${esc(r.name)}</a>`;

// A machine's tags as chips: those that count first, then, dimmed, those it reports that you haven't
// accepted. '' when it has none.
function tagChips(r) {
  const counted = Array.isArray(r.accepted_tags) ? r.accepted_tags.filter((t) => typeof t === 'string') : [];
  const waiting = (Array.isArray(r.tags) ? r.tags : []).filter((t) => typeof t === 'string' && !counted.includes(t));
  return counted.map((t) => `<span class="chip tag">${esc(t)}</span>`)
    .concat(waiting.map((t) => `<span class="chip tag dim" title="It reports this tag, but you haven't accepted it">${esc(t)} · not accepted</span>`))
    .join('');
}

function machineCard(r) {
  const clash = clashOf(r);
  return `<div class="card machine">
    <div class="m-head"><h3><span class="dot ${r.online ? 'on' : ''}"></span> ${machineLink(r)}</h3>
      <span class="muted small m-state">${machineState(r)}</span></div>
    ${clash ? `<div class="clash small">Two computers use this name: ${esc(r.hostname || '?')} and ${esc(clash)}</div>` : ''}
    <div class="muted small">${esc(r.os)} · runner ${esc(r.version)} ${outdated(r) ? `<span class="chip warn">update to ${esc(state.runner_latest)}</span>` : ''} ${authBadge(r)}</div>
    <div class="m-agents">${agentChips(r)}</div>
    ${tagChips(r) ? `<div class="muted small m-tags">tags: ${tagChips(r)}</div>` : ''}
    <div class="muted small">${plural((r.projects || []).length, 'project')} · ${busyOf(r)}/${slotsOf(r)} busy</div>
    ${r.online ? '' : `<button class="ghost small" data-forget="${esc(r.name)}" style="margin-top:8px">Forget this machine</button>`}
  </div>`;
}

function machineRow(r) {
  const clash = clashOf(r);
  return `<tr><td class="ml-name"><span class="dot ${r.online ? 'on' : ''}"></span> ${machineLink(r)}
      ${clash ? `<span class="chip warn" title="Two computers use this name: ${esc(r.hostname || '?')} and ${esc(clash)}">name clash</span>` : ''}</td>
    <td class="ml-state small muted">${machineState(r)}</td>
    <td class="ml-agents">${agentChips(r)}</td>
    <td class="ml-tags">${tagChips(r) ? `<span class="phone-only muted small">tags: </span>${tagChips(r)}` : ''}</td>
    <td class="ml-busy small">${busyOf(r)}/${slotsOf(r)} busy</td>
    <td class="ml-ver small"><span class="phone-only">runner </span>${esc(r.version)} ${outdated(r) ? '<span class="chip warn">update</span>' : ''} ${authBadge(r)}</td>
    <td class="ml-act">${r.online ? '' : `<button class="ghost small" data-forget="${esc(r.name)}">Forget</button>`}</td></tr>`;
}

function machinesBlock(list, mode) {
  if (mode === 'list') {
    return `<div class="card table-wrap"><table class="stack machines"><thead><tr><th>Machine</th><th>Status</th><th>Agents</th><th>Tags</th><th>Busy</th><th>Runner</th><th></th></tr></thead>
      <tbody>${list.map(machineRow).join('')}</tbody></table></div>`;
  }
  return `<div class="grid">${list.map(machineCard).join('')}</div>`;
}

async function forgetMachine(name) {
  const r = state.runners.find((x) => x.name === name) || {};
  const what = r.auth === 'machine'
    ? `Remove ${name}? Its own token is revoked, so it can't check in again until you add it again with a new code.`
    : `Remove ${name} from the list? It comes back if its runner checks in again.`;
  const busy = (r.running || []).length;
  const warn = busy ? `\n\nIt is running ${busy} job(s) right now. They, and anything queued for it, are cancelled.` : '';
  if (!confirm(what + warn)) return false;
  await api(`/api/runners/${encodeURIComponent(name)}`, { method: 'DELETE' });
  return true;
}

// ------------------------------------------------------------------ job lists
function fullDate(ts) { return ts ? new Date(ts * 1000).toLocaleString() : ''; }

// Where a job runs: its machine, or "any machine tagged gpu" while a job aimed at a tag waits for one.
const whereRunner = (j) => (j.runner || !j.target_tag ? esc(j.runner || '') : `any machine tagged ${esc(j.target_tag)}`);

function jobWhere(j) {
  if (j.kind === 'council') {
    const m = j.meta || {}; const ms = j.machines || [];
    const on = ms.length === 1 ? ` · ${esc(ms[0])}` : ms.length ? ` · ${ms.length} machines` : '';
    return `council · ${m.mode === 'review' ? 'build + review' : 'debate'}${on}`;
  }
  if (j.kind === 'admin') return `machine admin @ ${esc(j.runner)}`;
  // A subtask starts with the job that handed it out; a task that handed out subtasks says how many.
  const parent = j.delegated_by ? `↳ <a href="#/job/${Number(j.delegated_by)}">#${Number(j.delegated_by)}</a> · ` : '';
  const kids = Number(j.subtasks) > 0 ? ` · ${plural(Number(j.subtasks), 'subtask')}` : '';
  return `${parent}${esc(j.agent)}${j.model ? ` (${esc(j.model)})` : ''} @ ${whereRunner(j)} · ${esc(j.project)}${kids}${creditsShort(j)}`;
}

// A list of jobs. On a phone each row becomes a small card (see table.jobs in style.css).
function jobTable(jobs, empty) {
  if (!jobs.length) return `<div class="card empty">${empty}</div>`;
  return `<div class="card table-wrap"><table class="stack jobs"><thead><tr><th>#</th><th>What</th><th>Where</th><th>Status</th><th>When</th></tr></thead><tbody>
  ${jobs.map((j) => `<tr class="link" data-job="${j.id}">
    <td class="j-id small muted">${j.id}</td>
    <td class="j-what"><a href="#/job/${j.id}"><span class="j-num">#${j.id} </span>${esc(j.title)}</a></td>
    <td class="j-where small">${jobWhere(j)}</td>
    <td class="j-status">${statusChip(j.status)}${waitingChip(j)}${j.error_kind ? ` ${errorKindChip(j.error_kind, false)}` : ''}</td>
    <td class="j-when small muted" title="${esc(fullDate(j.created_at))}">${ago(j.created_at)}${j.started_at ? ` · ${duration(j)}` : ''}</td></tr>`).join('')}
  </tbody></table></div>`;
}

// Rows anywhere inside `el` open their job when clicked (the title itself is a link too).
function openJobsOnClick(el) {
  el.addEventListener('click', (ev) => {
    const row = ev.target.closest('[data-job]');
    if (row && !ev.target.closest('a, button')) location.hash = `#/job/${row.dataset.job}`;
  });
}

// "Skipped: a, b (offline); c (remote updates are off)."
function skippedText(res) {
  const groups = {};
  for (const name of res.skipped || []) {
    const why = String((res.reasons || {})[name] || 'skipped').replace(/\.$/, '');
    (groups[why] = groups[why] || []).push(name);
  }
  const parts = Object.entries(groups).map(([why, names]) => {
    const shown = names.slice(0, 8).map(esc).join(', ');
    return `${shown}${names.length > 8 ? ` and ${names.length - 8} more` : ''} (${esc(why)})`;
  });
  return parts.length ? ` Skipped: ${parts.join('; ')}.` : '';
}

// ------------------------------------------------------- plan limits and tokens
// Display only: these figures never decide where work goes (hub/limits.py).

// A table lookup by a name a runner sent: only the table's own entries ("constructor" isn't one).
const own = (table, key) => (Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined);

// Token counts as 950, 1.2k, 34k, 3.4M.
function count(n) {
  let v = Math.max(0, Number(n) || 0);
  if (v < 1000) return String(Math.round(v));
  for (const unit of ['k', 'M', 'B']) {
    v /= 1000;
    const shown = v >= 100 ? String(Math.round(v)) : v.toFixed(1).replace(/\.0$/, '');
    if (Number(shown) < 1000 || unit === 'B') return shown + unit;
  }
  return String(v);
}

// "~$3.12", "~$0.004". Claude Code's own estimate at API list prices.
function estimate(cost) {
  const v = Math.max(0, Number(cost) || 0);
  if (v === 0 || v >= 0.01) return `~$${v.toFixed(2)}`;
  return v >= 0.001 ? `~$${v.toFixed(3)}` : 'under $0.001';
}

// Everything the agent read: new input, input read from the cache and input written to it.
const tokensIn = (u) => (Number(u.input_tokens) || 0) + (Number(u.cache_read_tokens) || 0) + (Number(u.cache_write_tokens) || 0);
const tokensInTitle = (u) => `${Number(u.input_tokens) || 0} new, ${Number(u.cache_read_tokens) || 0} read from the cache, ${Number(u.cache_write_tokens) || 0} written to the cache`;
const COST_TITLE = "Claude Code's own estimate at API list prices. A subscription isn't billed per token; this shows how heavy the job was.";

// A job that failed at a usage limit says which kind. Links go to where to look next.
const GEMINI_LIMITS_URL = 'https://aistudio.google.com/rate-limit';
const ERROR_KINDS = { quota: 'Gemini quota hit', claude_limit: 'Claude usage limit', credits: 'Platform credits used up' };
function errorKindChip(kind, linked) {
  const text = own(ERROR_KINDS, kind);
  if (!text) return '';
  if (!linked) return `<span class="chip bad">${text}</span>`;
  if (kind === 'quota') return `<a class="chip bad" href="${GEMINI_LIMITS_URL}" target="_blank" rel="noopener noreferrer" title="Gemini API rate limits in Google AI Studio">${text}</a>`;
  return kind === 'credits'
    ? `<a class="chip bad" href="#/usage" title="Platform credits on the Usage page">${text}</a>`
    : `<a class="chip bad" href="#/usage" title="Plan limits on the Usage page">${text}</a>`;
}

// ------------------------------------------------------------ platform credits
// A job that ran on its machine's credits agent instead of the agent its seat or subtask named, a
// retry on credits, or a step that ran again on the named agent once the credits ran out.
function creditsInfo(j) { return j && j.credits && typeof j.credits === 'object' ? j.credits : {}; }
function creditsShort(j) {
  const c = creditsInfo(j);
  if (j.named_agent) return ` · <span class="chip credits" title="Ran on platform credits">on credits instead of ${esc(j.named_agent)}</span>`;
  if (c.how === 'retry') return ' · <span class="chip credits" title="A retry on platform credits">retried on credits</span>';
  if (c.requeued) return ` · <span class="chip" title="${esc(c.agent || '')} found the platform credits used up">credits used up; ran as named</span>`;
  return '';
}
function creditsMetaHtml(j) {
  const c = creditsInfo(j);
  const parts = [];
  if (j.named_agent) parts.push(`<span class="chip credits">on credits (agent ${esc(j.agent)}) instead of ${esc(j.named_agent)}</span>${c.org ? ` <span class="small">${esc(c.org)}</span>` : ''}`);
  if (c.how === 'retry' && j.retry_of) parts.push(`<span class="chip credits">retried on credits</span> (${esc(j.agent)} instead of ${esc(c.named_agent || '')}) after <a href="#/job/${Number(j.retry_of)}">#${Number(j.retry_of)}</a> stopped at a usage limit`);
  if (c.requeued) parts.push(`<span class="chip">credits used up</span> on ${esc(c.agent || '')}; ran again on ${esc(j.agent)}, the agent named`);
  return parts.map((x) => ` · ${x}`).join('');
}
// The state of an organization's platform credits, as a chip.
const CREDIT_STATES = { available: ['ok', 'available'], reserve: ['warn', 'reserve'], exhausted: ['bad', 'used up'], unknown: ['', 'not set up'] };
function creditStateChip(state) {
  const [cls, text] = own(CREDIT_STATES, state) || ['', 'unknown'];
  return `<span class="chip ${cls}">${text}</span>`;
}

// When a limit resets, in the viewer's own time: "Thu 23:00", or with the date when it is
// more than six days away.
function whenLocal(ts) {
  const far = Math.abs(ts - Date.now() / 1000) > 6 * 86400;
  return new Date(ts * 1000).toLocaleString(undefined, far
    ? { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }
    : { weekday: 'short', hour: 'numeric', minute: '2-digit' });
}

// Windows in page order (as hub/limits.py sorts them): 5-hour, week, the other weeks by name,
// current, extra usage.
const WINDOW_RANK = { five_hour: 0, seven_day: 1, current: 3, overage: 4 };
const windowRank = (id) => own(WINDOW_RANK, id) ?? (String(id).startsWith('seven_day_') ? 2 : 5);
const byWindow = (a, b) => (windowRank(a) - windowRank(b)) || String(a).localeCompare(String(b));
const WINDOW_SHORT = { five_hour: '5h', seven_day: 'week', seven_day_overage_included: 'Fable', current: 'limit', overage: 'extra' };
function windowShort(id) {
  if (own(WINDOW_SHORT, id)) return WINDOW_SHORT[id];
  const s = String(id);
  const rest = s.startsWith('seven_day_') ? s.slice(10) : s;
  return rest.replace(/_/g, ' ').replace(/\b[a-z]/g, (c) => c.toUpperCase());
}

// Percent used, floored as Claude Code prints it (0.29 is 29 %, not 28).
const percent = (used) => Math.floor(Number(used) * 100 + 1e-6);
const isExpired = (w, now) => w.resets_at != null && w.resets_at < now;
// A window's figures are merged one by one, each with its own time. Its status still counts
// unless a newer figure for how much is used came after it (Check limits reports no status).
function statusCurrent(w) {
  const statusAt = Number(w.status_at) || 0;
  const usedAt = Number(w.used_at) || 0;
  return w.used === null || w.used === undefined || !statusAt || !usedAt || usedAt <= statusAt + 1;
}
// How a window's figure looks: 'bad' at or over the limit (or refused), 'warn' when close.
function windowLevel(w) {
  const used = w.used === null || w.used === undefined ? null : Number(w.used);
  const status = statusCurrent(w) ? w.status : null;
  if (status === 'rejected' || (used !== null && used >= 1)) return 'bad';
  if (status === 'allowed_warning' || (used !== null && used >= 0.9)) return 'warn';
  return '';
}

// "Limit reached" only when Claude Code said requests are refused (the account's own status,
// not one window's) and neither a reset nor a newer figure has undone it since. Gives the time
// jobs work again (null when unknown), or undefined when the limit isn't reached.
function limitReachedUntil(status, windows, now) {
  if (!status || status.value !== 'rejected') return undefined;
  const list = windows || [];
  // The windows still refused: a window's own 'rejected', unless a newer figure under the limit
  // came after it, or it has reset since.
  const still = list.filter((w) => w.status === 'rejected' && !isExpired(w, now) && (statusCurrent(w) || Number(w.used) >= 1));
  if (still.length) {
    const times = still.map((w) => w.resets_at).filter((t) => t !== null && t !== undefined);
    return times.length ? Math.max(...times) : null;
  }
  // No window is refused now. If any figure is newer than the refusal, or a window has reset
  // since, the refusal is over; otherwise nothing has said so yet.
  const since = (Number(status.observed_at) || 0) + 1;
  const newer = list.some((w) => Math.max(Number(w.used_at) || 0, Number(w.status_at) || 0) > since);
  const reset = list.some((w) => w.resets_at !== null && w.resets_at !== undefined && w.resets_at > since && w.resets_at <= now);
  return newer || reset ? undefined : null;
}

// Why extra usage can't be used, in words (Claude Code's overageDisabledReason).
const OVERAGE_REASONS = {
  overage_not_provisioned: 'not set up for this account',
  org_level_disabled: 'turned off for the organization',
  org_level_disabled_until: 'the spending cap is reached for now',
  out_of_credits: 'no credits left',
  seat_tier_level_disabled: "this seat doesn't include it",
  member_level_disabled: 'turned off for this member',
  seat_tier_zero_credit_limit: 'no credit set for this seat',
  group_zero_credit_limit: 'no credit set for this group',
  member_zero_credit_limit: 'no credit set for this member',
  org_service_level_disabled: 'turned off for this service',
  no_limits_configured: 'no spending limit is set',
  fetch_error: "Claude Code couldn't check",
  unknown: 'reason unknown',
};
const overageReason = (r) => own(OVERAGE_REASONS, r) || String(r || '').replace(/_/g, ' ');

// ---------------------------------------------------------------- overview
// The page is drawn once; every 5 seconds only the machine and job lists are redrawn, so the
// filter box keeps what you type and where the cursor is.
const overview = { filter: '', stopping: new Set() };
const isOverview = () => location.hash === '' || location.hash === '#/' || location.hash === '#';

function viewMode(count) {
  const saved = prefs.get('machineView', '');
  if (saved === 'cards' || saved === 'list') return saved;
  return count > 12 ? 'list' : 'cards';
}

function fleetMsg(html, isError, boxId = 'fleet-msg') {
  const box = document.getElementById(boxId);
  if (!box) return;
  box.className = `notice${isError ? ' bad' : ''}`;
  box.innerHTML = `<div class="row spread"><span>${html}</span><button type="button" class="ghost small" data-close-msg>OK</button></div>`;
  box.hidden = false;
}

async function renderOverview() {
  stopTimer();
  await loadState();
  view.innerHTML = `
    <h1>Overview</h1>
    <div id="o-approvals"></div>
    <div id="o-accept"></div>
    <div id="o-rc"></div>
    <div class="row spread"><h2 class="tight">Machines <span class="muted small" id="m-count"></span></h2>
      <button type="button" class="small" id="add-machine">Add a machine</button></div>
    <div class="row m-tools" id="m-tools">
      <input type="search" id="m-filter" placeholder="Filter by name, agent, system or tag" aria-label="Filter machines" autocomplete="off">
      <div class="seg" role="group" aria-label="Show machines as">
        <button type="button" class="ghost small" data-view="cards" aria-pressed="false">Cards</button><button type="button" class="ghost small" data-view="list" aria-pressed="false">List</button></div>
      <button type="button" class="ghost small" id="sync-all" title="Ask every online machine to pick up the latest notes, add-ons and MCP servers now">Sync all</button>
      <button type="button" class="ghost small" id="update-all" hidden></button>
    </div>
    <div id="fleet-msg" class="notice" hidden></div>
    <div id="m-clash"></div>
    <div id="machines"></div>
    <div class="row spread"><h2>Recent work</h2><a class="small" href="#/history">All work in History</a></div>
    <div id="recent"></div>
    <details id="admin-box" ${prefs.get('adminOpen', false) ? 'open' : ''}><summary>Machine admin <span class="muted small">updates, syncs and project changes</span></summary><div id="admin-jobs"></div></details>`;

  const filter = document.getElementById('m-filter');
  filter.value = overview.filter;
  filter.addEventListener('input', () => { overview.filter = filter.value; drawOverview(); });
  view.querySelectorAll('[data-view]').forEach((b) => b.addEventListener('click', () => { prefs.set('machineView', b.dataset.view); drawOverview(); }));
  document.getElementById('add-machine').addEventListener('click', () => openAddMachine());
  document.getElementById('admin-box').addEventListener('toggle', (ev) => prefs.set('adminOpen', ev.target.open));
  document.getElementById('fleet-msg').addEventListener('click', (ev) => { if (ev.target.closest('[data-close-msg]')) ev.currentTarget.hidden = true; });
  const machines = document.getElementById('machines');
  machines.addEventListener('click', async (ev) => {
    const b = ev.target.closest('button');
    if (!b) return;
    if (b.dataset.forget) {
      try { if (await forgetMachine(b.dataset.forget)) { await loadState(); drawOverview(); } } catch (e) { fleetMsg(esc(e.message), true); }
    } else if (b.dataset.addMachine !== undefined) openAddMachine();
  });
  openJobsOnClick(document.getElementById('recent'));
  openJobsOnClick(document.getElementById('admin-jobs'));
  document.getElementById('o-rc').addEventListener('click', async (ev) => {
    const b = ev.target.closest('[data-rc-stop]');
    if (!b) return;
    const { runner, agent, project } = b.dataset;
    const key = rcKey(runner, agent, project);
    overview.stopping.add(key); b.disabled = true; b.textContent = 'Stopping…';
    try {
      const id = await rcAction(runner, 'rc.stop', agent, project);
      fleetMsg(`Asked ${esc(runner)} to stop Remote Control for ${esc(agent)} in ${esc(project)} (<a href="#/job/${id}">#${id}</a>).`);
    } catch (e) { overview.stopping.delete(key); fleetMsg(esc(e.message), true); drawOverview(); }
  });

  document.getElementById('sync-all').addEventListener('click', async (ev) => {
    const b = ev.currentTarget; b.disabled = true;
    try {
      const r = await api('/api/runners-sync-all', { method: 'POST', body: {} });
      fleetMsg(`${r.queued.length ? `Asked ${plural(r.queued.length, 'machine')} to sync now.` : 'No machine was asked to sync.'}${skippedText(r)}`);
      await loadState(); drawOverview();
    } catch (e) { fleetMsg(esc(e.message), true); } finally { b.disabled = false; }
  });
  document.getElementById('update-all').addEventListener('click', async (ev) => {
    const b = ev.currentTarget; b.disabled = true;
    try {
      const r = await api('/api/runners-update-all', { method: 'POST', body: {} });
      fleetMsg(`${r.queued.length ? `Queued the update to ${esc(r.version)} on ${plural(r.queued.length, 'machine')}. You can follow it under Machine admin below.` : 'No update was queued.'}${skippedText(r)}`);
      await loadState(); drawOverview();
    } catch (e) { fleetMsg(esc(e.message), true); } finally { b.disabled = false; }
  });

  drawOverview();
  every(5000, async () => {
    if (!isOverview()) { stopTimer(); return; }
    await loadState();
    if (isOverview()) drawOverview();
  });
}

// "2 jobs waiting for your approval: #12 Fix the tests (Bash), ..." Questions from 'ask' jobs,
// oldest first; each job once, with the tools it asks about.
function approvalsNoticeHtml() {
  const n = Number(state.approvals_pending) || 0;
  const list = Array.isArray(state.approvals) ? state.approvals : [];
  if (!n) return '';
  const jobs = new Map();
  for (const a of list) {
    const e = jobs.get(a.job_id) || { title: a.title, tools: [] };
    if (!e.tools.includes(a.tool_name)) e.tools.push(a.tool_name);
    jobs.set(a.job_id, e);
  }
  const links = [...jobs].map(([id, e]) => `<a href="#/job/${Number(id)}">#${Number(id)} ${esc(e.title)}</a> (${e.tools.map(esc).join(', ')})`);
  const more = n > list.length ? `, and ${plural(n - list.length, 'more question')}` : '';
  const count = jobs.size ? plural(jobs.size, 'job') : plural(n, 'question');
  return `<div class="notice approvals-notice"><strong>${count} waiting for your approval:</strong> ${links.join(', ')}${more}</div>`;
}

// Machines that report tags or account labels you haven't accepted yet, each linked to its page.
function acceptNoticeHtml(runners) {
  const list = runners.filter(hasToAccept);
  if (!list.length) return '';
  const links = list.map(machineLink);
  const names = links.length === 1 ? links[0] : `${links.slice(0, -1).join(', ')} and ${links[links.length - 1]}`;
  return `<div class="notice accept-notice">${names} ${list.length === 1 ? 'reports' : 'report'} new tags or account labels.
    They count once you accept them on ${list.length === 1 ? 'its page' : 'their pages'}.</div>`;
}

// Remote Control servers running on any machine, each with Stop.
function rcRunningHtml(runners) {
  const rows = [];
  for (const r of runners) {
    for (const e of rcEntries(r)) if (e.state === 'running' || e.state === 'starting') rows.push({ r, e });
  }
  const live = new Set(rows.map(({ r, e }) => rcKey(r.name, e.agent, e.project)));
  for (const key of [...overview.stopping]) if (!live.has(key)) overview.stopping.delete(key);
  if (!rows.length) return '';
  return `<div class="card rc-running"><h3>Remote Control running</h3>${rows.map(({ r, e }) => {
    const stopping = overview.stopping.has(rcKey(r.name, e.agent, e.project));
    return `<div class="rc-line"><span>${esc(e.agent)} in ${esc(e.project)} on ${machineLink(r)}${e.state === 'starting' ? ' <span class="status starting">starting</span>' : ''}
      <span class="muted small">· since <span title="${esc(fullDate(e.since))}">${ago(e.since)}</span> · stops at ${esc(rcTime(e.stops_at))}</span></span>
      <button type="button" class="ghost small" data-rc-stop data-runner="${esc(r.name)}" data-agent="${esc(e.agent)}" data-project="${esc(e.project)}"${stopping ? ' disabled' : ''}>${stopping ? 'Stopping…' : 'Stop'}</button></div>`;
  }).join('')}</div>`;
}

function drawOverview() {
  const all = sortedRunners();
  const f = overview.filter.trim();
  const shown = all.filter((r) => machineMatches(r, f));
  const online = shown.filter((r) => r.online);
  const offline = shown.filter((r) => !r.online);
  const mode = viewMode(all.length);
  const onlineCount = all.filter((r) => r.online).length;

  document.getElementById('m-count').textContent = all.length
    ? `${onlineCount} of ${all.length} online${f ? ` · ${shown.length} shown` : ''}` : '';
  document.getElementById('m-filter').hidden = all.length <= 6 && !f;
  view.querySelector('.seg').hidden = all.length < 2;
  view.querySelectorAll('[data-view]').forEach((b) => {
    b.classList.toggle('active', b.dataset.view === mode);
    b.setAttribute('aria-pressed', String(b.dataset.view === mode));
  });
  document.getElementById('sync-all').hidden = !onlineCount;
  const upd = document.getElementById('update-all');
  const behind = all.filter(outdated).length;
  upd.hidden = !behind;
  if (behind) upd.textContent = `Update ${behind === all.length ? 'all runners' : plural(behind, 'runner')} to ${state.runner_latest}`;
  document.getElementById('m-tools').hidden = !all.length;
  setRegion(document.getElementById('o-approvals'), approvalsNoticeHtml());
  setRegion(document.getElementById('o-accept'), acceptNoticeHtml(all));
  setRegion(document.getElementById('o-rc'), rcRunningHtml(all));

  const clashes = all.filter(clashOf);
  document.getElementById('m-clash').innerHTML = clashes.length ? `<div class="notice"><strong>Two computers share a machine name.</strong>
    ${clashes.map((r) => `${machineLink(r)} (${esc(r.hostname || '?')} and ${esc(clashOf(r))})`).join(', ')}.
    They show up as one machine here and can pick up each other's jobs. Open the machine to see how to fix it.</div>` : '';

  const box = document.getElementById('machines');
  if (!all.length) {
    box.innerHTML = `<div class="card empty">No machine has checked in yet.<br>
      <button type="button" data-add-machine style="margin-top:12px">Add a machine</button>
      <p class="small">It gives you one command to run on the machine. You can also set it up by hand with the setup guide.</p></div>`;
  } else if (!shown.length) {
    box.innerHTML = `<div class="card empty">No machine matches “${esc(f)}”.</div>`;
  } else {
    // Offline machines fold away below the online ones. A filter, or no online machine, opens them.
    const forced = !!f || !online.length;
    const open = forced || prefs.get('offlineOpen', false);
    box.innerHTML = (online.length ? machinesBlock(online, mode) : '')
      + (offline.length ? `<details class="offline" ${open ? 'open' : ''}><summary>Offline <span class="muted small">${offline.length}</span></summary>${machinesBlock(offline, mode)}</details>` : '');
    const det = box.querySelector('details.offline');
    if (det && !forced) det.addEventListener('toggle', () => prefs.set('offlineOpen', det.open));
  }

  document.getElementById('recent').innerHTML = jobTable(state.jobs || [],
    'Nothing yet. Send a <a href="#/task">task</a> or start a <a href="#/council">council</a>.');
  const adminJobs = state.admin_jobs || [];
  document.getElementById('admin-jobs').innerHTML = jobTable(adminJobs, 'No machine admin jobs yet.');
  // The list holds the newest few; the hub counts all of them that are still waiting or running.
  const running = state.admin_open ?? adminJobs.filter((j) => j.status === 'queued' || j.status === 'running').length;
  view.querySelector('#admin-box > summary').innerHTML = `Machine admin <span class="muted small">${running ? `${running} waiting or running · ` : ''}updates, syncs and project changes</span>`;
}

// ---------------------------------------------------------------- add a machine
// One-time enrollment codes (POST/GET/DELETE /api/enroll-codes). The command a code comes with
// installs the runner on the new machine and trades the code for that machine's own token.
const addDialog = document.getElementById('add-machine-dialog');
addDialog.addEventListener('click', (ev) => {
  if (ev.target === addDialog || ev.target.closest('[data-close]')) addDialog.close();  // backdrop or Close
});

async function copyText(text, btn) {
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); ok = true; }
  } catch (e) { ok = false; }
  if (!ok) {
    // A plain http:// address (the usual LAN setup) has no clipboard API, so copy from a hidden text box.
    const ta = document.createElement('textarea');
    ta.value = text; ta.setAttribute('readonly', ''); ta.className = 'offscreen';
    (btn && btn.closest('dialog') ? btn.closest('dialog') : document.body).appendChild(ta);
    ta.select();
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    ta.remove();
  }
  if (btn) {
    const label = btn.dataset.label || btn.textContent;
    btn.dataset.label = label;
    btn.textContent = ok ? 'Copied' : 'Select and copy';
    setTimeout(() => { btn.textContent = label; }, 2500);
  }
  return ok;
}

function minutes(sec) { const m = Math.max(1, Math.round((sec || 0) / 60)); return plural(m, 'minute'); }

function openAddMachine(name) {
  const body = document.getElementById('add-machine-body');
  const lastUrl = prefs.get('enrollHubUrl', '') || state.runner_url || '';
  body.innerHTML = `
    <p class="muted">This makes a one-time setup command. Run it on the new machine: it installs the runner, gives the
      machine its own sign-in to this hub, and the machine appears on the Overview.</p>
    <form id="am-form">
      <label for="am-name">Machine name <span class="muted">optional; blank uses the computer's own name</span></label>
      <input type="text" id="am-name" maxlength="64" placeholder="e.g. build-box" autocomplete="off" value="${esc(name || '')}">
      <label for="am-url">Hub address for machines <span class="muted">how the machine reaches this hub</span></label>
      <input type="text" id="am-url" placeholder="blank: the hub's own setting (HUB_RUNNER_URL)" autocomplete="off" value="${esc(lastUrl)}">
      <p class="muted small">Usually the hub's address on your network, such as <code>http://192.168.100.20:8080</code>.
        An address behind a login page (such as Cloudflare Access) doesn't work here, because the runner can't sign in to it.</p>
      <p class="error" id="am-error" hidden></p>
      <button type="submit" id="am-make">Create the command</button>
    </form>
    <div id="am-result" hidden></div>
    <h3 class="am-codes-title">Unused codes</h3>
    <div id="am-codes" class="small muted">Loading…</div>`;
  if (!addDialog.open) addDialog.showModal();
  const nameIn = document.getElementById('am-name');
  const urlIn = document.getElementById('am-url');
  (name ? urlIn : nameIn).focus();
  let made = null;  // the last code made here, so editing the form can offer a fresh one

  const loadCodes = async () => {
    const box = document.getElementById('am-codes');
    try {
      const data = await api('/api/enroll-codes');
      const codes = data.codes || [];
      if (!urlIn.value && codes.length && codes[0].hub_url) urlIn.placeholder = codes[0].hub_url;
      box.innerHTML = codes.length ? `<ul class="codes">${codes.map((c) => `<li class="row spread"><span><code>${esc(c.code_hint)}…</code>
          ${c.name ? `for <strong>${esc(c.name)}</strong>` : 'any name'} · expires in ${minutes(c.expires_in)}</span>
          <button type="button" class="ghost small" data-revoke="${esc(c.code_hint)}">Revoke</button></li>`).join('')}</ul>`
        : 'None. Codes you make here stay listed until they are used or expire.';
    } catch (e) {
      box.textContent = /HTTP 404|Not Found/i.test(e.message) ? '' : e.message;
      if (/HTTP 404|Not Found/i.test(e.message)) {
        document.getElementById('am-form').hidden = true;
        document.querySelector('.am-codes-title').hidden = true;
        document.getElementById('am-result').hidden = false;
        document.getElementById('am-result').innerHTML = '<p class="error">This hub can\'t make setup commands yet. Update the hub, or set the machine up by hand with the setup guide.</p>';
      }
    }
  };
  document.getElementById('am-codes').onclick = async (ev) => {
    const b = ev.target.closest('[data-revoke]');
    if (!b) return;
    b.disabled = true;
    try { await api(`/api/enroll-codes/${encodeURIComponent(b.dataset.revoke)}`, { method: 'DELETE' }); } catch (e) { /* shown by the reload */ }
    if (made && made.code_hint === b.dataset.revoke) { document.getElementById('am-result').hidden = true; made = null; }
    loadCodes();
  };
  const stale = () => { if (made) document.getElementById('am-make').textContent = 'Make a new code with these settings'; };
  nameIn.addEventListener('input', stale);
  urlIn.addEventListener('input', stale);

  document.getElementById('am-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const err = document.getElementById('am-error'); err.hidden = true;
    const btn = document.getElementById('am-make'); btn.disabled = true;
    try {
      const reqBody = {};
      if (nameIn.value.trim()) reqBody.name = nameIn.value.trim();
      if (urlIn.value.trim()) reqBody.hub_url = urlIn.value.trim();
      made = await api('/api/enroll-codes', { method: 'POST', body: reqBody });
      if (made.hub_url) { urlIn.value = made.hub_url; if (reqBody.hub_url) prefs.set('enrollHubUrl', made.hub_url); }
      const until = new Date(Date.now() + (made.expires_in || 3600) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      const res = document.getElementById('am-result');
      res.innerHTML = `
        <div class="cmd-title"><strong>Linux or macOS</strong> <span class="muted small">in a terminal, as your normal user (not root)</span></div>
        <div class="cmd"><code>${esc(made.linux)}</code><button type="button" class="small" data-copy="linux">Copy</button></div>
        <div class="cmd-title"><strong>Windows</strong> <span class="muted small">in a normal PowerShell window (not as administrator)</span></div>
        <div class="cmd"><code>${esc(made.windows)}</code><button type="button" class="small" data-copy="windows">Copy</button></div>
        <p class="small">Code <code>${esc(made.code)}</code>${made.name ? ` for <strong>${esc(made.name)}</strong>` : ''}. It works <strong>once</strong>, on one machine,
          and expires in ${minutes(made.expires_in || 3600)} (at ${esc(until)}). Make a new code for each machine.
          The machine has to reach <code>${esc(made.hub_url || '')}</code>.</p>`;
      res.hidden = false;
      res.scrollIntoView({ block: 'start', behavior: 'smooth' });  // on a phone the commands start below the form
      res.querySelectorAll('[data-copy]').forEach((b) => b.addEventListener('click', () => copyText(made[b.dataset.copy], b)));
      btn.textContent = 'Make another code';
      loadCodes();
    } catch (e) { err.textContent = e.message; err.hidden = false; } finally { btn.disabled = false; }
  });
  loadCodes();
}

// ------------------------------------------------------------- seat picker
// A "seat" is machine + agent + project (+ model), used by the task and council forms. Instead of a
// machine, a seat can name a tag (value "tag:gpu"): the job then runs on the first free machine
// with that tag, the agent and the project.
function machineLabel(r) {
  if (!r.online) return r.name;
  const busy = busyOf(r);
  return busy ? `${r.name} · busy ${busy}/${slotsOf(r)}` : r.name;
}

// The tags a job can be aimed at, each with its machines: the tags that count (accepted ones and
// the platform tag) on machines with their own runner token, since tag jobs only go to those.
function tagMachines() {
  const tags = new Map();
  for (const r of sortedRunners()) {
    if (r.auth !== 'machine') continue;
    for (const t of Array.isArray(r.accepted_tags) ? r.accepted_tags : []) {
      if (typeof t === 'string') tags.set(t, [...(tags.get(t) || []), r]);
    }
  }
  return new Map([...tags].sort((a, b) => a[0].localeCompare(b[0])));
}
const TAG_VALUE = 'tag:';  // machine names can't contain ':', so a tag option can't clash with a machine
const seatTag = (value) => (String(value || '').startsWith(TAG_VALUE) ? String(value).slice(TAG_VALUE.length) : '');
// Which account an agent counts as for routing: its accepted label, or "<machine>/<agent>".
function accountOf(r, agent) {
  const key = r && r.accounts && typeof r.accounts === 'object' ? own(r.accounts, agent) : undefined;
  return typeof key === 'string' ? key : `${(r && r.name) || ''}/${agent}`;
}
const hasAgent = (r, agent) => (r.agents || []).some((a) => a.name === agent);
// The machines a job for `tag` could go to: the tag, the agent and, when given, the project and the
// account key (the hub's rule, common.tag_target).
function tagSeatMachines(tag, agent, project, account) {
  return (tagMachines().get(tag) || []).filter((r) => hasAgent(r, agent)
    && (!project || (r.projects || []).includes(project)) && (!account || accountOf(r, agent) === account));
}
// The account keys an agent has on those machines, each with its machines.
function tagSeatAccounts(tag, agent, project) {
  const keys = new Map();
  for (const r of tagSeatMachines(tag, agent, project)) {
    const k = accountOf(r, agent);
    keys.set(k, [...(keys.get(k) || []), r.name]);
  }
  return keys;
}

// Machine choices: online ones first, in two groups, then "Any machine tagged…". `filter` narrows
// the list; the machine or tag that is already chosen always stays in it.
function machineOptions(selected, filter) {
  const list = sortedRunners().filter((r) => r.name === selected || machineMatches(r, filter));
  const group = (label, rs) => (rs.length ? `<optgroup label="${label}">${rs.map((r) => `<option value="${esc(r.name)}" ${r.name === selected ? 'selected' : ''}>${esc(machineLabel(r))}</option>`).join('')}</optgroup>` : '');
  const words = String(filter || '').toLowerCase().split(/\s+/).filter(Boolean);
  const chosen = seatTag(selected);
  const tags = [...tagMachines()].filter(([t]) => t === chosen || words.every((w) => `tag tagged ${t}`.includes(w)));
  if (chosen && !tags.some(([t]) => t === chosen)) tags.push([chosen, []]);
  const tagGroup = tags.length ? `<optgroup label="Any machine tagged…">${tags.map(([t, rs]) => {
    const label = rs.length ? `Any machine tagged ${t} (${plural(rs.length, 'machine')}, ${rs.filter((r) => r.online).length} online)`
      : `Any machine tagged ${t} (no machine now)`;
    return `<option value="${esc(TAG_VALUE + t)}" ${t === chosen ? 'selected' : ''}>${esc(label)}</option>`;
  }).join('')}</optgroup>` : '';
  return (group('Online', list.filter((r) => r.online)) + group('Offline', list.filter((r) => !r.online)) + tagGroup)
    || '<option value="" disabled selected>no machine matches</option>';
}

// With many machines, one box narrows every machine list in the form.
const machineFinder = () => (state.runners.length > 8
  ? `<div class="finder"><input type="search" class="machine-find" placeholder="Find a machine: name, agent, system or tag" aria-label="Find a machine for the lists below" autocomplete="off"></div>` : '');
const finderText = (el) => ((el.closest('form') || document).querySelector('.machine-find')?.value || '').trim();

function wireMachineFinder(form) {
  const input = form.querySelector('.machine-find');
  if (!input) return;
  input.addEventListener('input', () => {
    form.querySelectorAll('select[data-part="runner"]').forEach((sel) => {
      const cur = sel.value;
      sel.innerHTML = machineOptions(cur, input.value.trim());
      if (cur) sel.value = cur;
      else sel.dispatchEvent(new Event('change', { bubbles: true }));
    });
  });
}

const TAG_SEAT_NOTE = 'Runs on the first free machine with this tag that has the agent and project, and stays there.';

// tag: the seat's name in a council ("Response A", "Judge"); null for the single seat of a task.
function seatHtml(id, tag, opts = {}) {
  const solo = tag === null;
  return `<div class="seat${solo ? ' solo' : ''}" id="${id}">
    ${solo ? '' : `<div class="tag">${esc(tag)}</div>`}
    <label class="part"><span>Machine</span><select data-part="runner"></select></label>
    <label class="part"><span>Agent</span><select data-part="agent"></select></label>
    <label class="part"><span>Project</span><select data-part="project">${opts.anyProject ? '<option value="">chat (default)</option>' : ''}</select></label>
    <label class="part"><span>Model</span><input type="text" data-part="model" placeholder="default" list="${id}-models" autocomplete="off"></label>
    <datalist id="${id}-models"></datalist>
    ${solo ? '' : `<div class="rm">${opts.removable ? '<button type="button" class="ghost small" data-remove>Remove</button>' : ''}</div>`}
    <label class="part seat-acct" hidden><span>Account <span class="muted">this agent is a different account on different machines with this tag</span></span><select data-part="account" disabled></select></label>
    <p class="muted small seat-note" hidden>${esc(TAG_SEAT_NOTE)}</p>
  </div>`;
}

function wireSeat(el, preset = {}) {
  const [rSel, aSel, pSel, mIn, cSel] = ['runner', 'agent', 'project', 'model', 'account'].map((p) => el.querySelector(`[data-part="${p}"]`));
  const list = el.querySelector('datalist');
  const note = el.querySelector('.seat-note');
  const keepBlank = pSel.querySelector('option[value=""]') !== null;
  rSel.innerHTML = machineOptions(preset.tag ? TAG_VALUE + preset.tag : (preset.runner || ''), finderText(el));
  const tagOf = () => seatTag(rSel.value);
  const machine = () => state.runners.find((x) => x.name === rSel.value) || { agents: [], projects: [] };
  // The machines this seat may run on: the chosen one, or those with the tag and the agent.
  const machinesNow = () => (tagOf() ? tagSeatMachines(tagOf(), aSel.value) : [machine()]);
  const fillModels = () => {
    const agents = machinesNow().map((r) => (r.agents || []).find((x) => x.name === aSel.value)).filter(Boolean);
    list.innerHTML = [...new Set(agents.flatMap((a) => a.models || []))].map((m) => `<option value="${esc(m)}">`).join('');
    mIn.placeholder = `${(agents.find((a) => a.default_model) || {}).default_model || 'default'}`;
  };
  // A tag seat whose agent is a different account on different machines needs an account.
  const fillAccounts = () => {
    const tag = tagOf();
    const keys = tag ? tagSeatAccounts(tag, aSel.value, pSel.value || 'chat') : new Map();
    const show = keys.size > 1;
    const cur = cSel.value;
    cSel.closest('.part').hidden = !show;
    cSel.disabled = !show;
    cSel.innerHTML = show ? `<option value="">Choose the account</option>${[...keys].sort((a, b) => a[0].localeCompare(b[0]))
      .map(([k, ms]) => `<option value="${esc(k)}">${esc(k)} (${esc(ms.join(', '))})</option>`).join('')}` : '';
    if (show && keys.has(cur)) cSel.value = cur;
    note.hidden = !tag;
  };
  const fillProjects = () => {
    const cur = pSel.value;
    const projects = tagOf() ? [...new Set(machinesNow().flatMap((r) => r.projects || []))].sort() : (machine().projects || []);
    pSel.innerHTML = (keepBlank ? '<option value="">chat (default)</option>' : '') + projects.filter((p) => !(keepBlank && p === 'chat')).map((p) => `<option value="${esc(p)}">${esc(p)}</option>`).join('');
    if ([...pSel.options].some((o) => o.value === cur)) pSel.value = cur;
    fillAccounts();
  };
  const fill = () => {
    const tag = tagOf();
    if (tag) {
      // Every agent on the machines with the tag, with the account it is there.
      const rs = tagMachines().get(tag) || [];
      const names = [...new Set(rs.flatMap((r) => (r.agents || []).map((a) => a.name)))].sort();
      aSel.innerHTML = names.map((n) => {
        const keys = [...new Set(rs.filter((r) => hasAgent(r, n)).map((r) => accountOf(r, n)))];
        return `<option value="${esc(n)}">${esc(n)} · ${keys.length === 1 ? esc(keys[0]) : `${keys.length} accounts`}</option>`;
      }).join('');
    } else {
      aSel.innerHTML = (machine().agents || []).map((a) => `<option value="${esc(a.name)}">${esc(a.label || a.name)}${a.ready ? '' : ' (not ready)'}</option>`).join('');
    }
    fillProjects();
    fillModels();
  };
  rSel.addEventListener('change', () => { mIn.value = ''; fill(); });
  aSel.addEventListener('change', () => { mIn.value = ''; fillProjects(); fillModels(); });
  pSel.addEventListener('change', fillAccounts);
  fill();
  if (preset.agent) { aSel.value = preset.agent; fillProjects(); }
  if (preset.project !== undefined) { pSel.value = preset.project; fillAccounts(); }
  if (preset.account && [...cSel.options].some((o) => o.value === preset.account)) cSel.value = preset.account;
  fillModels();
  if (preset.model) mIn.value = preset.model;
}

// A seat as the hub takes it: a machine (runner) or a tag, with the account for a tag seat when the
// agent is a different account on different machines.
function readSeat(el) {
  const get = (p) => el.querySelector(`[data-part="${p}"]`).value;
  const tag = seatTag(get('runner'));
  const acct = el.querySelector('[data-part="account"]');
  return { runner: tag ? '' : get('runner'), tag: tag || null, account: tag && !acct.disabled && acct.value ? acct.value : null,
    agent: get('agent'), project: get('project') || null, model: get('model').trim() || null };
}

// What a seat's agent can do: the agent on its machine, or for a tag, what the agent can do on the
// machines the job could go to (the hub holds the job for one that can do all it needs).
function seatAgent(s) {
  if (!s.tag) return agentInfo(s.runner, s.agent);
  const agents = tagSeatMachines(s.tag, s.agent, s.project || 'chat', s.account)
    .map((r) => (r.agents || []).find((a) => a.name === s.agent)).filter(Boolean);
  if (!agents.length) return null;
  return { name: s.agent, features: [...new Set(agents.flatMap((a) => (Array.isArray(a.features) ? a.features : [])))] };
}

// The account a seat's job runs under: its machine's, or for a tag the one chosen (or the only one).
function seatOwnKey(s) {
  if (!s.tag) {
    const r = state.runners.find((x) => x.name === s.runner);
    return r ? accountOf(r, s.agent) : null;
  }
  if (s.account) return s.account;
  const keys = tagSeatAccounts(s.tag, s.agent, s.project || 'chat');
  return keys.size === 1 ? [...keys.keys()][0] : null;
}

// The next seat to suggest, given the seats already taken: a ready agent on an online machine,
// on a machine with a free slot if there is one, then a different agent, then a machine that is
// used less (or idle), so a council spreads out instead of piling onto the first machine.
function suggestSeat(taken = []) {
  const tiers = [
    (r, a) => r.online && a.ready && a.name !== 'selftest',
    (r, a) => r.online && a.name !== 'selftest',
    (r) => r.online,
    () => true,
  ];
  let pool = [];
  for (const ok of tiers) {
    pool = [];
    sortedRunners().forEach((r) => (r.agents || []).forEach((a, i) => { if (ok(r, a)) pool.push({ r, a, i }); }));
    if (pool.length) break;
  }
  if (!pool.length) return {};
  const count = (key) => { const m = {}; taken.forEach((t) => { m[key(t)] = (m[key(t)] || 0) + 1; }); return m; };
  const onMachine = count((t) => t.runner); const ofAgent = count((t) => t.agent); const seat = count((t) => `${t.runner}\n${t.agent}`);
  const score = ({ r, a, i }) => [
    seat[`${r.name}\n${a.name}`] || 0,                               // the same seat again only when nothing else is left
    (onMachine[r.name] || 0) + busyOf(r) >= slotsOf(r) ? 1 : 0,       // a machine with a free slot
    ofAgent[a.name] || 0,                                             // a different agent (often a different model)
    onMachine[r.name] || 0,                                           // a machine used less here
    busyOf(r) / slotsOf(r),                                           // an idle machine
    i,                                                                // the machine's own agent order
  ];
  const cmp = (x, y) => { const a = score(x); const b = score(y); for (let k = 0; k < a.length; k += 1) if (a[k] !== b[k]) return a[k] - b[k]; return 0; };
  const best = pool.reduce((m, c) => (cmp(c, m) < 0 ? c : m));  // the pool is in machine order, so ties go to the first machine
  return { runner: best.r.name, agent: best.a.name };
}

function suggestSeats(n, taken = []) {
  const picks = [];
  for (let i = 0; i < n; i += 1) picks.push(suggestSeat([...taken, ...picks]));
  return picks;
}

function noMachines() {
  view.innerHTML = `<h1>No machines yet</h1><div class="card empty">Install and start the runner on at least one machine first.
    It shows up on the Overview page when it checks in.<br><button type="button" id="nm-add" style="margin-top:12px">Add a machine</button></div>`;
  document.getElementById('nm-add').addEventListener('click', () => openAddMachine());
}

// --------------------------------------------------------------- new task
async function renderTask() {
  stopTimer();
  await loadState();
  if (!state.runners.length) return noMachines();
  let prefill = null;
  try { prefill = JSON.parse(sessionStorage.getItem('prefill') || 'null'); sessionStorage.removeItem('prefill'); } catch (e) { prefill = null; }
  view.innerHTML = `
    <h1>New task</h1>
    <form class="card" id="task-form">
      <label style="margin-top:0">Where and who <span class="muted">a blank model means the agent's default</span></label>
      ${machineFinder()}
      ${seatHtml('seat-task', null)}
      <label>What the agent may do</label>
      <div id="task-autonomy"></div>
      <div id="task-live-box"></div>
      <div id="task-delegate-box"></div>
      <label class="check"><input type="checkbox" id="capture-diff"> Show me a diff of what changed afterwards (git projects)</label>
      <label for="task-title">Title <span class="muted">optional; lists and notifications show it instead of the prompt's first line</span></label>
      <input type="text" id="task-title" maxlength="120" autocomplete="off">
      <label for="prompt">Prompt</label>
      <textarea id="prompt" required placeholder="What should the agent do? For chat-style questions use the 'chat' project."></textarea>
      <div class="row" style="margin-top:6px">
        <label style="margin:0">Time limit (minutes) <input type="number" id="timeout" min="1" max="1440" value="60" style="width:100px"></label>
      </div>
      <p class="error" id="task-error" hidden></p>
      <div class="row" style="margin-top:14px"><button type="submit">Send</button></div>
    </form>`;
  const form = document.getElementById('task-form');
  wireMachineFinder(form);
  const seat = document.getElementById('seat-task');
  wireSeat(seat, prefill || { ...suggestSeats(1)[0], project: 'chat' });
  if (prefill) document.getElementById('prompt').value = prefill.prompt || '';
  // The choices that depend on the agent: 'ask', keeping the session open and handing out
  // subtasks. What you picked is kept while you try other agents, and comes back when you pick
  // one that can do it again.
  const want = { autonomy: (prefill && prefill.autonomy) || 'readonly', live: !!(prefill && prefill.live), idle: (prefill && prefill.idle_min) || 15,
    delegate: delegateWant(prefill || {}) };
  const delegateCtx = () => {
    const s = readSeat(seat);
    return { agent: seatAgent(s), runner: s.runner, ownKey: seatOwnKey(s), project: s.project || 'chat' };
  };
  const redrawDelegate = wireDelegate(document.getElementById('task-delegate-box'), 'task-delegate', want.delegate, delegateCtx);
  const drawChoices = () => {
    const s = readSeat(seat);
    const agent = seatAgent(s);
    document.getElementById('task-autonomy').innerHTML = autonomyRadios('autonomy', want.autonomy, agent);
    const idle = document.getElementById('task-live-idle');
    if (idle) want.idle = idle.value;
    document.getElementById('task-live-box').innerHTML = hasFeature(agent, 'live')
      ? liveOptionHtml('task-live', want.live, want.idle, 'The time limit below still applies.') : '';
    redrawDelegate();
  };
  drawChoices();
  // Any change of machine (or tag), agent, project or account; the model doesn't matter here.
  seat.addEventListener('change', (ev) => { if (ev.target.dataset.part && ev.target.dataset.part !== 'model') drawChoices(); });
  form.addEventListener('change', (ev) => {
    if (ev.target.name === 'autonomy') want.autonomy = ev.target.value;
    if (ev.target.id === 'task-live') want.live = ev.target.checked;
    syncChoiceHints(form, 'autonomy');
  });
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const err = document.getElementById('task-error'); err.hidden = true;
    const s = readSeat(seat);
    const autonomy = form.querySelector('input[name=autonomy]:checked')?.value || 'readonly';
    const title = document.getElementById('task-title').value.trim();
    try {
      const res = await api('/api/tasks', { method: 'POST', body: {
        ...(s.tag ? { target_tag: s.tag, ...(s.account ? { account: s.account } : {}) } : { runner: s.runner }),
        agent: s.agent, project: s.project || 'chat', autonomy, model: s.model,
        prompt: document.getElementById('prompt').value,
        capture_diff: document.getElementById('capture-diff').checked,
        timeout_min: Number(document.getElementById('timeout').value) || 60,
        ...liveChoice('task-live'),
        ...delegateChoice(document.getElementById('task-delegate-box')),
        ...(title ? { title } : {}),
      } });
      location.hash = `#/job/${res.id}`;
    } catch (e) { err.textContent = e.message; err.hidden = false; }
  });
}

// ----------------------------------------------------------------- council
let seatCounter = 0;

// A growing list of seats (debate participants, reviewers) between `min` and `max` long.
function seatList({ box, addBtn, note, min, max, tag, full, anyProject, others }) {
  const seats = () => [...box.querySelectorAll(':scope > .seat')];
  const refresh = () => {
    const all = seats();
    all.forEach((el, i) => {
      el.querySelector('.tag').textContent = tag(i);
      el.querySelector('[data-remove]').disabled = all.length <= min;
    });
    addBtn.disabled = all.length >= max;
    note.textContent = all.length >= max ? full : '';
  };
  const add = (preset) => {
    if (seats().length >= max) return;
    const id = `seat-${box.id}-${seatCounter += 1}`;
    box.insertAdjacentHTML('beforeend', seatHtml(id, '', { removable: true, anyProject }));
    const el = document.getElementById(id);
    wireSeat(el, { ...(preset || suggestSeat([...others(), ...seats().filter((s) => s !== el).map(readSeat)])), project: '' });
    el.querySelector('[data-remove]').addEventListener('click', () => { if (seats().length > min) { el.remove(); refresh(); } });
    refresh();
  };
  addBtn.addEventListener('click', () => add());
  return { add, seats };
}

async function renderCouncil() {
  stopTimer();
  await loadState();
  if (!state.runners.length) return noMachines();
  const lim = { debate_participants: 12, reviewers: 10, ...(state.limits || {}) };
  view.innerHTML = `
    <h1>Council</h1>
    <p class="muted">Make agents work on the same thing and check each other. Answers are anonymised as “Response A/B…” so nobody can tell which model wrote what.</p>
    <form class="card" id="council-form">
      <div class="radio-row">
        <label><input type="radio" name="mode" value="debate" checked> <span><strong>Debate</strong>: each answers, each attacks the others, a judge rules</span></label>
        <label><input type="radio" name="mode" value="review"> <span><strong>Build + review</strong>: one does the work, others hunt for problems</span></label>
      </div>
      ${machineFinder()}

      <div id="debate-part">
        <label>Participants <span class="muted">2 to ${lim.debate_participants}; mixing different models works best</span></label>
        <div id="participants" class="seat-list"></div>
        <div class="row"><button type="button" class="ghost small" id="add-participant">+ Add participant</button><span class="muted small" id="participants-note"></span></div>
        <label>Judge <span class="muted">reads everything and writes the final answer</span></label>
        ${seatHtml('seat-judge', 'Judge', { anyProject: true })}
        <label class="check"><input type="checkbox" id="revise"> Add a revise round (each fixes its own answer after the critiques, before the judge)</label>
      </div>

      <div id="review-part" hidden>
        <label>Builder <span class="muted">does the work in a project folder</span></label>
        ${seatHtml('seat-builder', 'Builder')}
        <div id="b-autonomy-box"></div>
        <label>Reviewers <span class="muted">1 to ${lim.reviewers}; they see the builder's diff, and on the same machine they can also open the files</span></label>
        <div id="reviewers" class="seat-list"></div>
        <div class="row"><button type="button" class="ghost small" id="add-reviewer">+ Add reviewer</button><span class="muted small" id="reviewers-note"></span></div>
        <label class="check"><input type="checkbox" id="fix" checked> Let the builder check each finding and fix the valid ones</label>
      </div>

      <label for="c-title">Title <span class="muted">optional; lists and notifications show it instead of the request's first line</span></label>
      <input type="text" id="c-title" maxlength="120" autocomplete="off">
      <label for="c-prompt">The request</label>
      <textarea id="c-prompt" required></textarea>
      <div class="row" style="margin-top:6px">
        <label style="margin:0">Time limit per step (minutes) <input type="number" id="c-timeout" min="1" max="1440" value="60" style="width:100px"></label>
      </div>
      <p class="error" id="c-error" hidden></p>
      <div class="row" style="margin-top:14px"><button type="submit">Start council</button></div>
    </form>`;

  const form = document.getElementById('council-form');
  wireMachineFinder(form);
  const judgeEl = document.getElementById('seat-judge');
  const builderEl = document.getElementById('seat-builder');
  const participants = seatList({
    box: document.getElementById('participants'), addBtn: document.getElementById('add-participant'),
    note: document.getElementById('participants-note'), min: 2, max: lim.debate_participants,
    tag: (i) => `Response ${String.fromCharCode(65 + i)}`, anyProject: true, others: () => [],
    full: `${lim.debate_participants} participants is the most a debate can have.`,
  });
  const reviewers = seatList({
    box: document.getElementById('reviewers'), addBtn: document.getElementById('add-reviewer'),
    note: document.getElementById('reviewers-note'), min: 1, max: lim.reviewers,
    tag: (i) => `Reviewer ${i + 1}`, anyProject: true, others: () => [readSeat(builderEl)],
    full: `${lim.reviewers} reviewers is the most a review can have.`,
  });
  // Spread the starting seats over machines and agents: two participants and a judge, or a
  // builder and a reviewer.
  const debate = suggestSeats(2);
  debate.forEach((s) => participants.add(s));
  wireSeat(judgeEl, { ...suggestSeat(debate), project: '' });
  const builder = suggestSeats(1)[0];
  wireSeat(builderEl, builder);
  reviewers.add(suggestSeat([builder]));
  // What the builder may do depends on the builder's agent ('ask' needs one that can ask).
  let builderWants = 'edits';
  const drawBuilderLevels = () => {
    const s = readSeat(builderEl);
    document.getElementById('b-autonomy-box').innerHTML = autonomyRadios('b-autonomy', builderWants, seatAgent(s), BUILDER_LEVELS);
  };
  drawBuilderLevels();
  builderEl.addEventListener('change', (ev) => { if (ev.target.dataset.part && ev.target.dataset.part !== 'model') drawBuilderLevels(); });
  document.getElementById('b-autonomy-box').addEventListener('change', (ev) => {
    if (ev.target.name === 'b-autonomy') builderWants = ev.target.value;
    syncChoiceHints(document.getElementById('b-autonomy-box'), 'b-autonomy');
  });

  const modeSwitch = () => {
    const mode = view.querySelector('input[name=mode]:checked').value;
    document.getElementById('debate-part').hidden = mode !== 'debate';
    document.getElementById('review-part').hidden = mode !== 'review';
    document.getElementById('c-prompt').placeholder = mode === 'debate'
      ? 'A question or decision you want stress-tested, e.g. "Which backup strategy should I use for a small home server, and why?"'
      : 'The work to do, e.g. "Add input validation to the import script and a test for it."';
  };
  view.querySelectorAll('input[name=mode]').forEach((r) => r.addEventListener('change', modeSwitch));
  modeSwitch();

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const err = document.getElementById('c-error'); err.hidden = true;
    const mode = view.querySelector('input[name=mode]:checked').value;
    const body = { mode, prompt: document.getElementById('c-prompt').value, timeout_min: Number(document.getElementById('c-timeout').value) || 60 };
    if (mode === 'debate') {
      body.participants = participants.seats().map(readSeat);
      body.judge = readSeat(judgeEl);
      body.revise = document.getElementById('revise').checked;
    } else {
      body.builder = readSeat(builderEl);
      body.reviewers = reviewers.seats().map(readSeat);
      body.autonomy = view.querySelector('input[name=b-autonomy]:checked')?.value || 'edits';
      body.fix = document.getElementById('fix').checked;
    }
    const title = document.getElementById('c-title').value.trim();
    if (title) body.title = title;
    try {
      const res = await api('/api/councils', { method: 'POST', body });
      location.hash = `#/job/${res.id}`;
    } catch (e) { err.textContent = e.message; err.hidden = false; }
  });
}

// ------------------------------------------------------------------ job view
const STAGES = { debate: ['answer', 'critique', 'revise', 'judge'], review: ['build', 'review', 'fix'] };

function stageBar(meta, status) {
  const order = (STAGES[meta.mode] || []).filter((s) => (s !== 'revise' || meta.revise) && (s !== 'fix' || meta.fix));
  const cur = order.indexOf(meta.stage);
  return `<div class="stages">${order.map((s, i) => {
    let cls = i < cur || (status === 'done') ? 'past' : (i === cur ? 'current' : '');
    if (status !== 'running' && status !== 'done' && i === cur) cls = 'current';
    return `<span class="stage ${cls}">${i + 1}. ${esc(s)}</span>`;
  }).join('')}</div>`;
}

function stepCard(s) {
  const body = s.status === 'done' ? `<div class="md">${md(s.result)}</div>`
    : s.status === 'error' || s.status === 'cancelled' ? `<p class="error">${esc(s.error || s.status)}</p>`
    : `<p class="muted">${s.status === 'queued' ? 'Waiting for the machine to pick this up…' : 'Working…'} <a href="#/job/${s.id}">watch the live log</a></p>`;
  return `<details ${s.status !== 'done' ? 'open' : ''}><summary>${esc(s.label)} · <code>${esc(s.agent)}</code> on ${whereRunner(s)}${creditsShort(s)} ${statusChip(s.status)} <span class="muted small">${duration(s)}</span></summary>
    ${body}
    <p class="small"><a href="#/job/${s.id}">open step #${s.id}</a></p></details>`;
}

// The job page is a shell drawn once per job, with regions that each refresh on their own (see
// setRegion), so the 2-second refresh never wipes what you are typing or moves a button you are
// about to press. The log is only ever added to.

// What the agent may do. 'ask' needs approvals on the agent, the hub's job tools and a dashboard
// password; without them it is shown but can't be picked.
const AUTONOMY_LEVELS = [
  { value: 'readonly', label: 'Read only (answer, review, explain)' },
  { value: 'edits', label: 'Edit files in the project' },
  { value: 'ask', label: 'Edit files, ask me before commands' },
  { value: 'full', label: 'Full auto (edits and commands)' },
];
const ASK_NEEDS = "Needs a Claude agent on an up-to-date runner, the hub's MCP support and a dashboard password";
const ASK_HINT = 'File commands inside the project (mkdir, mv, cp, rm, sed) and read-only commands run without asking.';

// The council's builder: the same levels, said of the builder, without read only.
const BUILDER_LEVELS = [
  { value: 'edits', label: 'Builder may edit files' },
  { value: 'ask', label: 'Builder may edit files and run commands you approve' },
  { value: 'full', label: 'Builder may edit and run commands' },
];

function agentInfo(runnerName, agentName) {
  const r = state.runners.find((x) => x.name === runnerName) || {};
  return (r.agents || []).find((a) => a.name === agentName) || null;
}
const hasFeature = (agent, f) => !!agent && Array.isArray(agent.features) && agent.features.includes(f);
const askReady = (agent) => hasFeature(agent, 'approvals') && !!state.job_tools && !!state.approvals_possible;

// Why 'ask' can't be picked for this agent here, in a sentence; '' when it can.
function askBlocker(agent) {
  if (!state.job_tools) return "Asking before commands needs the hub's MCP support (the mcp package; see the install guide).";
  if (!state.approvals_possible) return 'Asking before commands needs a dashboard password: set HUB_UI_PASSWORD on the hub.';
  if (!agent) return 'Asking before commands needs a Claude agent on an up-to-date runner.';
  const flags = Array.isArray(agent.permission_flags) ? agent.permission_flags.filter((f) => typeof f === 'string') : [];
  if (!hasFeature(agent, 'approvals') && flags.length) {
    return `${agent.name} can't ask before commands: its entry in runner.json has permission flags outside its levels (${flags.join(', ')}), which would win over asking.`;
  }
  if (!hasFeature(agent, 'approvals')) {
    return `Asking before commands needs a Claude agent on an up-to-date runner (${agent.name} can't).`;
  }
  return '';
}

// Radios for what the agent may do. A level that can't be picked here falls back to the one below
// it ('ask' to 'edits'), never to more. When 'ask' can't be picked, a line says why.
function autonomyRadios(name, selected, agent, levels = AUTONOMY_LEVELS) {
  const blocker = askBlocker(agent);
  let pick = levels.some((l) => l.value === selected) ? selected : levels[0].value;
  if (pick === 'ask' && blocker) pick = 'edits';
  return `<div class="radio-row">${levels.map((l) => {
    const off = l.value === 'ask' && !!blocker;
    return `<label${off ? ` class="off" title="${esc(ASK_NEEDS)}"` : ''}><input type="radio" name="${esc(name)}" id="${esc(name)}-${l.value}" value="${l.value}"${l.value === pick ? ' checked' : ''}${off ? ' disabled' : ''}> ${esc(l.label)}</label>`;
  }).join('')}</div>
    <p class="muted small" data-ask-hint ${pick === 'ask' ? '' : 'hidden'}>${esc(ASK_HINT)}</p>
    ${blocker && levels.some((l) => l.value === 'ask') ? `<p class="muted small" data-ask-off>${esc(blocker)}</p>` : ''}`;
}

// Show the 'ask' hint only while 'ask' is picked, and the idle time only while the session is kept open.
function syncChoiceHints(form, radioName) {
  const hint = form.querySelector('[data-ask-hint]');
  if (hint) hint.hidden = form.querySelector(`input[name="${radioName}"]:checked`)?.value !== 'ask';
  form.querySelectorAll('[data-live-opt]').forEach((box) => {
    const idle = box.querySelector('.live-idle');
    if (idle) idle.hidden = !box.querySelector('input[type=checkbox]').checked;
  });
}

// "Keep the session open for messages" (agents with the 'live' feature: Claude agents). `id` is
// the checkbox's id; the minutes field is `${id}-idle`.
function liveOptionHtml(id, on, idle, limitNote) {
  return `<div class="live-opt" data-live-opt>
    <label class="check"><input type="checkbox" id="${esc(id)}"${on ? ' checked' : ''}> Keep the session open for messages</label>
    <div class="live-idle row"${on ? '' : ' hidden'}><label for="${esc(id)}-idle">End it after</label>
      <input type="number" id="${esc(id)}-idle" min="1" max="240" value="${Number(idle) || 15}"><span>minutes without a message</span></div>
    <p class="muted small">You can type to the agent on the job's page while it works. ${esc(limitNote)}</p></div>`;
}

// The body fields for that option: {} when it isn't offered or isn't ticked.
function liveChoice(id) {
  const box = document.getElementById(id);
  if (!box || !box.checked) return {};
  const idle = Math.round(Number(document.getElementById(`${id}-idle`).value) || 15);
  return { live: true, idle_min: Math.min(240, Math.max(1, idle)) };
}

// ---------------------------------------------------------------- subtasks
// "May hand subtasks to other machines" and the owner's scope for those subtasks. By default a
// subtask may use any of your agents on the machines that take subtasks, whatever its account, in
// a project named like this task's (or chat); each list narrows that. The hub checks it all again.
const takesSubtasks = (r) => !!(r && r.info && r.info.accept_delegated_tasks === true);

// The choices, kept while you try other machines and agents. For accounts and agents, `only`
// false means "all but those in set" (the default: all), true means "only those in set".
function delegateWant(o = {}) {
  const picks = (list) => (Array.isArray(list) ? { only: true, set: new Set(list) } : { only: false, set: new Set() });
  return { on: !!o.delegate, to: new Set(Array.isArray(o.delegate_to) ? o.delegate_to : []),
    projects: o.delegate_projects === 'any' ? 'any' : 'same', accounts: picks(o.delegate_accounts), agents: picks(o.delegate_agents) };
}
const picked = (p, key) => (p.only ? p.set.has(key) : !p.set.has(key));
function pick(p, key, on) { if (p.only === on) p.set.add(key); else p.set.delete(key); }

// The machines subtasks may go to: those with their own runner token that take subtasks, narrowed
// to the machines and tags ticked under "Subtasks may go to" (none ticked: all of them).
function subtaskMachines(want) {
  const all = sortedRunners().filter((r) => takesSubtasks(r) && r.auth === 'machine');
  const listed = new Set([...all.map((r) => `machine:${r.name}`), ...all.flatMap((r) => (r.accepted_tags || []).map((t) => `tag:${t}`))]);
  const to = [...want.to].filter((e) => listed.has(e));
  const inScope = to.length ? all.filter((r) => to.includes(`machine:${r.name}`) || (r.accepted_tags || []).some((t) => to.includes(`tag:${t}`))) : all;
  return { all, inScope };
}

const DELEGATE_MAX_WORDS = { edits: 'edit files', ask: 'edit files and run commands you approve' };
function delegateHint() {
  const d = state.delegation || {};
  const most = d.max_autonomy || 'readonly';
  const level = most === 'readonly' ? ', and only reads unless your hub allows more'
    : own(DELEGATE_MAX_WORDS, most) ? `, and on this hub no more than ${own(DELEGATE_MAX_WORDS, most)}` : '';
  const counts = d.max_children ? ` A task can hand out at most ${plural(Number(d.max_children), 'subtask')}, ${Number(d.max_active) || 1} at a time.` : '';
  return `The agent can see these machines and start subtasks on them. Each subtask may do at most what this task may do${level}.${counts}
    Subtasks can read any file in the projects they reach. While this task runs, don't run Full-auto tasks on untrusted content on
    the same machine: they could use its subtask powers.`;
}

const OVERFLOW_WARNING = "Usage limits aren't a reason to switch accounts. Handing work to another Claude account because this one is close to "
  + 'its limit is the overflow pattern guide 1 warns about. After a usage-limit error, new subtasks may only use the account that hit it.';

// The checkbox and, when ticked, the scope. `id` names the checkbox; ctx: {agent, runner (a machine
// by name, or '' for a tag), ownKey (this task's account key, null while it isn't chosen), project}.
function delegateHtml(id, want, ctx) {
  if (!hasFeature(ctx.agent, 'job_mcp') || !state.job_tools || !state.approvals_possible) return '';
  const head = (attrs) => `<label class="check"><input type="checkbox" id="${esc(id)}" data-dg="on" data-dg-focus="on"${attrs}> May hand subtasks to other machines</label>`;
  const machine = ctx.runner ? state.runners.find((r) => r.name === ctx.runner) : null;
  if (machine && machine.auth !== 'machine') {
    return `<div class="dg-opt">${head(' disabled')}
      <p class="muted small">${esc(ctx.runner)} still uses the shared runner token, and a task that hands out subtasks needs a machine with
      its own token: use Make a setup command on its page (guide 5).</p></div>`;
  }
  if (!want.on) return `<div class="dg-opt">${head('')}</div>`;
  const { all, inScope } = subtaskMachines(want);
  const shared = sortedRunners().filter((r) => takesSubtasks(r) && r.auth !== 'machine');
  const sharedNote = shared.length ? `<p class="muted small">${shared.map((r) => esc(r.name)).join(', ')} ${shared.length === 1 ? 'takes' : 'take'}
    subtasks but ${shared.length === 1 ? 'uses' : 'use'} the shared runner token, so none go there (Make a setup command on ${shared.length === 1 ? 'its page' : 'their pages'}).</p>` : '';
  if (!all.length) {
    return `<div class="dg-opt">${head(' checked')}<div class="dg-scope">
      <p class="small flush">No machine takes subtasks yet. On each machine that should, run <code>runner.py init</code> and answer yes to
      “Let agents on your other machines hand subtasks to this machine”, or set <code>accept_delegated_tasks</code> to true in its runner.json (guide 5).
      The agent can look again while it works.</p>${sharedNote}</div></div>`;
  }
  const box = (kind, value, on, label) => `<label class="check"><input type="checkbox" data-dg="${kind}" data-dg-focus="${esc(`${kind}:${value}`)}" value="${esc(value)}"${on ? ' checked' : ''}> <span>${label}</span></label>`;
  const tags = [...new Set(all.flatMap((r) => (r.accepted_tags || []).filter((t) => typeof t === 'string')))].sort();
  const toBoxes = all.map((r) => box('to', `machine:${r.name}`, want.to.has(`machine:${r.name}`), esc(r.name)))
    .concat(tags.map((t) => box('to', `tag:${t}`, want.to.has(`tag:${t}`), `any machine tagged ${esc(t)}`)));
  // Agents that can take subtasks report 'levels' (the hub can check what they may do).
  const able = inScope.flatMap((r) => (r.agents || []).filter((a) => hasFeature(a, 'levels')).map((a) => ({ r, a })));
  const keys = new Map();
  for (const { r, a } of able) {
    const k = accountOf(r, a.name);
    if (k !== ctx.ownKey) keys.set(k, [...(keys.get(k) || []), `${a.name} on ${r.name}`]);
  }
  const accountBoxes = [...keys].sort((x, y) => x[0].localeCompare(y[0]))
    .map(([k, where]) => box('account', k, picked(want.accounts, k), `${esc(k)} <span class="muted">(${esc(where.join(', '))})</span>`));
  const agentBoxes = [...new Set(able.map(({ a }) => a.name))].sort().map((n) => box('agent', n, picked(want.agents, n), esc(n)));
  const project = ctx.project || 'chat';
  const radio = (value, on, label) => `<label><input type="radio" name="${esc(id)}-projects" data-dg="projects" data-dg-focus="projects:${value}" value="${value}"${on ? ' checked' : ''}> ${label}</label>`;
  return `<div class="dg-opt">${head(' checked')}<div class="dg-scope">
    <div class="dg-part"><div class="dg-head">Subtasks may go to</div><div class="dg-checks">${toBoxes.join('')}</div>
      <p class="muted small flush">None ticked means any machine that takes subtasks.</p>${sharedNote}</div>
    <div class="dg-part"><div class="dg-head">Projects</div><div class="radio-row">
      ${radio('same', want.projects !== 'any', project === 'chat' ? 'only chat' : `only ${esc(project)} (and chat)`)}${radio('any', want.projects === 'any', 'any project')}</div></div>
    <div class="dg-part"><div class="dg-head">Accounts</div>
      <div class="small">This task's account: ${ctx.ownKey ? `<strong>${esc(ctx.ownKey)}</strong>` : '<span class="muted">choose the account above</span>'}</div>
      ${accountBoxes.length ? `<div class="small">Subtasks may also use:</div><div class="dg-checks">${accountBoxes.join('')}</div>`
        : '<div class="small muted">No other account on these machines.</div>'}
      <p class="dg-warn small">${esc(OVERFLOW_WARNING)}</p></div>
    <div class="dg-part"><div class="dg-head">Agents</div>${agentBoxes.length ? `<div class="dg-checks">${agentBoxes.join('')}</div>`
      : '<div class="small muted">No agent on these machines can take subtasks: it needs an up-to-date runner and a Claude, Gemini or self-test agent with its usual levels.</div>'}</div>
    <p class="muted small flush-bottom">${delegateHint()}</p></div></div>`;
}

// Draw the option into `box` and keep `want` up to date as you tick. `ctx()` gives the current
// context (see delegateHtml); call the returned function when the context changes.
function wireDelegate(box, id, want, ctx) {
  const draw = (focusKey) => {
    box.innerHTML = delegateHtml(id, want, ctx());
    if (focusKey) [...box.querySelectorAll('[data-dg-focus]')].find((x) => x.dataset.dgFocus === focusKey)?.focus();
  };
  box.addEventListener('change', (ev) => {
    const t = ev.target; const kind = t.dataset.dg;
    if (!kind) return;
    if (kind === 'on') want.on = t.checked;
    else if (kind === 'to') { if (t.checked) want.to.add(t.value); else want.to.delete(t.value); }
    else if (kind === 'projects') want.projects = t.value;
    else if (kind === 'account') pick(want.accounts, t.value, t.checked);
    else if (kind === 'agent') pick(want.agents, t.value, t.checked);
    // The accounts and agents listed depend on where subtasks may go.
    if (kind === 'on' || kind === 'to') draw(t.dataset.dgFocus);
  });
  draw();
  return () => draw();
}

// The body fields for the option: {} when it isn't offered or isn't ticked. A list where every
// box is ticked is sent as null ("any"), so accounts and agents that join later count too.
function delegateChoice(box) {
  const on = box && box.querySelector('input[data-dg="on"]');
  if (!on || !on.checked || on.disabled) return {};
  const boxes = (kind) => [...box.querySelectorAll(`input[data-dg="${kind}"]`)];
  const listOrAny = (kind) => (boxes(kind).every((b) => b.checked) ? null : boxes(kind).filter((b) => b.checked).map((b) => b.value));
  return { delegate: true, delegate_to: boxes('to').filter((b) => b.checked).map((b) => b.value),
    delegate_projects: box.querySelector('input[data-dg="projects"]:checked')?.value || 'same',
    delegate_accounts: listOrAny('account'), delegate_agents: listOrAny('agent') };
}

// Replace a region of the page only when what it shows has changed, and not while you are typing
// in it (it is redrawn on a later refresh instead). Typed text, the cursor and which <details> are
// open are kept.
const regionHtml = new WeakMap();
function setRegion(el, html, force = false) {
  if (!el || regionHtml.get(el) === html) return;
  const focused = document.activeElement;
  if (!force && focused && focused !== document.body && el.contains(focused)
      && focused.matches('input, textarea, select, [contenteditable]')) return;
  const open = [...el.querySelectorAll('details')].map((d) => d.open);
  const fields = [...el.querySelectorAll('input[id], textarea[id], select[id]')].map((f) => {
    let start = null; let end = null;
    try { start = f.selectionStart; end = f.selectionEnd; } catch (e) { /* not a text field */ }
    return { id: f.id, value: f.value, checked: f.checked, start, end };
  });
  el.innerHTML = html;
  regionHtml.set(el, html);
  el.querySelectorAll('details').forEach((d, i) => { if (open[i] !== undefined) d.open = open[i]; });
  for (const f of fields) {
    const n = document.getElementById(f.id);
    if (!n || !el.contains(n)) continue;
    if (n.type === 'checkbox' || n.type === 'radio') { n.checked = f.checked; continue; }
    n.value = f.value;
    try { if (f.start !== null) n.setSelectionRange(f.start, f.end); } catch (e) { /* not a text field */ }
  }
}

const jobIsLive = (j) => j.status === 'queued' || j.status === 'running';
// A follow-up continues a finished task's session, whatever way it ended (council steps too).
const canFollowUp = (j) => j.kind === 'task' && !jobIsLive(j) && !!j.session_id;

function jobShell(j) {
  if (j.kind === 'council') {
    return `<div id="job-page"><div class="row spread"><h1 class="j-title">#${Number(j.id)} · ${esc(j.title)}</h1><div class="row" id="job-actions"></div></div>
      <div id="job-body"></div></div>`;
  }
  return `<div id="job-page"><div class="row spread"><h1 class="j-title">#${Number(j.id)} · ${esc(j.title)}</h1><div class="row" id="job-actions"></div></div>
    <div id="job-meta" class="muted"></div>
    <div id="job-approvals"><div id="job-approvals-pending"></div><div id="job-approvals-past"></div></div>
    <div id="job-notices"></div>
    <div id="job-messages"></div>
    <div id="job-live"></div>
    <div id="job-live-note"></div>
    <div id="job-result"></div>
    <div id="job-usage"></div>
    <div id="job-subtasks"></div>
    <div id="job-continue"></div>
    <div id="job-follow"></div>
    <div id="job-thread"></div>
    ${j.kind !== 'admin' ? `<details><summary>Prompt</summary><pre>${esc(j.prompt)}</pre></details>` : ''}
    <details ${jobIsLive(j) || j.status === 'error' ? 'open' : ''}><summary>Live log</summary><pre class="log" id="job-log"><span class="muted">(nothing yet)</span></pre></details></div>`;
}

function jobActionsHtml(j) {
  let end = '';
  if (isLiveSession(j) && j.status === 'running' && !j.cancel_requested) {
    end = j.end_requested ? '<button class="ghost small" disabled>Ending…</button>'
      : '<button class="ghost small" id="end-session" title="The agent answers the messages it already has, then the job finishes">End session</button>';
  }
  return `${end}${jobIsLive(j) ? '<button class="danger small" id="cancel">Cancel</button>' : ''}
    ${j.kind === 'task' ? '<button class="ghost small" id="rerun">Run again…</button>' : ''}`;
}

function jobMetaHtml(j) {
  const opts = j.options && typeof j.options === 'object' ? j.options : {};
  // A job aimed at a tag: "any machine tagged gpu" until a machine takes it, then that machine.
  const where = j.runner ? `<strong>${esc(j.runner)}</strong>${j.target_tag ? ` <span class="small">(any machine tagged ${esc(j.target_tag)})</span>` : ''}`
    : `any machine tagged <strong>${esc(j.target_tag || '')}</strong>`;
  const account = (j.target_tag || j.delegated_by) && typeof opts.account === 'string' ? ` · account ${esc(opts.account)}` : '';
  const who = j.kind === 'admin'
    ? `Machine admin on <a href="#/machine/${encodeURIComponent(j.runner)}"><strong>${esc(j.runner)}</strong></a>`
    : `${esc(j.agent)}${j.model ? ` (${esc(j.model)})` : ''} on ${where}${account} · project ${esc(j.project)} · ${esc(j.autonomy)}`;
  return `${who} · ${statusChip(j.status)} · ${duration(j)}
    ${j.parent_id ? ` · part of <a href="#/job/${Number(j.parent_id)}">council #${Number(j.parent_id)}</a>` : ''}
    ${j.delegated_by ? ` · subtask of <a href="#/job/${Number(j.delegated_by)}">#${Number(j.delegated_by)}</a>` : ''}
    ${j.kind === 'task' && opts.delegate && !j.delegated_by ? ' · may hand out subtasks' : ''}
    ${j.follow_up_of ? ` · follow-up to <a href="#/job/${Number(j.follow_up_of)}">#${Number(j.follow_up_of)}</a>` : ''}
    ${own(ERROR_KINDS, j.error_kind) ? ` · ${errorKindChip(j.error_kind, true)}` : ''}${creditsMetaHtml(j)}`;
}

// A task that stopped at a usage limit: its retry on platform credits, or the button for one.
function creditsRetryHtml(j, data) {
  const r = data.retried_as;
  if (r && r.id) return `<div class="notice">Retried on platform credits (${esc(r.agent)}) as <a href="#/job/${Number(r.id)}">#${Number(r.id)}</a> · ${statusChip(r.status)}</div>`;
  const c = data.credits_retry;
  if (!c || j.status !== 'error') return '';
  if (c.agent) {
    return `<div class="notice row spread"><span>${esc(j.agent)} stopped at its usage limit. This machine's credits agent, ${esc(c.agent)}, can run it again, paid by ${esc(c.org)}'s platform credits.
      ${j.autonomy !== 'readonly' ? ' Check first what the first run changed in the project.' : ''}</span>
      <button type="button" class="small" id="retry-credits">Retry on credits</button></div>`;
  }
  return '';
}

// What the job itself used, as the agent reported it. Claude Code also gives a cost estimate.
function jobUsageHtml(j) {
  const u = j.usage;
  if (!u || typeof u !== 'object') return '';
  // A job that stopped before the agent did anything (at a usage limit, say) has nothing to show.
  if (!tokensIn(u) && !Number(u.output_tokens) && !Number(u.cost_usd)) return '';
  let line = `Tokens: <span title="${esc(tokensInTitle(u))}">${count(tokensIn(u))} in (${count(u.cache_read_tokens)} from cache)</span> · ${count(u.output_tokens)} out`;
  if (u.source !== 'gemini' && u.cost_usd !== null && u.cost_usd !== undefined) {
    line += ` · <span class="hint" title="${esc(COST_TITLE)}">API-equivalent estimate ${estimate(u.cost_usd)}</span>`;
  }
  if (u.includes_earlier) line += ' <span class="muted">(includes earlier turns of this conversation)</span>';
  else if (u.approximate) line += " <span class=\"muted\">(approximate: the session's earlier totals weren't saved)</span>";
  return `<div class="usage-line small">${line}</div>`;
}

function jobNoticesHtml(p, j) {
  const runner = j.runner ? state.runners.find((r) => r.name === j.runner) : null;
  const offline = j.status === 'queued' && runner && !runner.online
    ? `<div class="notice">${esc(j.runner)} is offline. The job will start when its runner checks in.</div>` : '';
  return offline + tagWaitingHtml(j, p.tagCandidates) + (p.creditsData ? creditsRetryHtml(j, p.creditsData) : '')
    + (p.actionError ? `<p class="error">${esc(p.actionError)}</p>` : '');
}

// A queued job aimed at a tag: what it waits for, and how many machines could take it.
function tagWaitingHtml(j, cand) {
  if (j.status !== 'queued' || j.runner || !j.target_tag || !cand) return '';
  const account = j.options && typeof j.options.account === 'string' ? ` (${esc(j.options.account)})` : '';
  const n = Number(cand.match) || 0; const m = Number(cand.online) || 0;
  const counts = `${plural(n, 'machine')} ${n === 1 ? 'matches' : 'match'}; ${m} ${m === 1 ? 'is' : 'are'} online.`;
  const none = n ? '' : " Accept the tag on a machine's page, or check that the machine has this agent and project and an up-to-date runner.";
  return `<div class="notice tag-wait">Waiting for a free machine tagged <strong>${esc(j.target_tag)}</strong> with ${esc(j.agent)}${account}
    and ${esc(j.project)}. ${counts}${none}</div>`;
}

// The subtasks this job handed out, oldest first, each linked to its own page.
function subtasksHtml(j, list) {
  if (!Array.isArray(list) || !list.length) return '';
  const prefix = `Subtask of #${Number(j.id)}: `;
  const rows = list.map((t) => {
    const title = String(t.title || '').startsWith(prefix) ? String(t.title).slice(prefix.length) : String(t.title || '');
    return `<tr class="link" data-job="${Number(t.id)}">
      <td class="st-id small muted">${Number(t.id)}</td>
      <td class="st-title"><a href="#/job/${Number(t.id)}"><span class="st-num">#${Number(t.id)} </span>${esc(title)}</a></td>
      <td class="st-where small"><span class="phone-only muted">on </span>${whereRunner(t)}</td>
      <td class="st-agent small"><span class="phone-only muted">agent </span>${esc(t.agent)}${t.named_agent ? ` <span class="muted">(on credits instead of ${esc(t.named_agent)})</span>` : ''}</td>
      <td class="st-account small">${t.account ? `<span class="phone-only muted">account </span>${esc(t.account)}` : ''}</td>
      <td class="st-status">${statusChip(t.status)}${own(ERROR_KINDS, t.error_kind) ? ` ${errorKindChip(t.error_kind, false)}` : ''}</td></tr>`;
  }).join('');
  return `<div class="card subtasks"><h3>Subtasks (${list.length})</h3>
    <div class="table-wrap"><table class="stack subtasks"><thead><tr><th>#</th><th>Title</th><th>Machine</th><th>Agent</th><th>Account</th><th>Status</th></tr></thead>
    <tbody>${rows}</tbody></table></div>
    <p class="muted small flush-bottom">The agent handed these out. Each one runs as a job of its own.</p></div>`;
}

function jobResultHtml(j, p) {
  let html = '';
  // A live session's result is its last answer, already at the end of the conversation above.
  const lastAnswer = [...(p.messages || [])].reverse().find((m) => m.role === 'agent');
  const shown = isLiveSession(j) && lastAnswer && lastAnswer.text === j.result;
  if (j.status === 'done' && !shown) html += `<div class="card" style="margin-top:12px"><div class="md">${md(j.result)}</div></div>`;
  if (j.error) html += `<p class="error">${esc(j.error)}</p>`;
  if (j.diff) html += `<details><summary>Changes made (diff)</summary><pre>${esc(j.diff)}</pre></details>`;
  return html;
}

function continueHtml(j, command) {
  if (!command) return '';
  return `<div class="card continue"><h3>Continue in a terminal</h3>
    <div class="muted small">On <strong>${esc(j.runner)}</strong>, in a terminal:</div>
    <div class="cmd"><code>${esc(command)}</code><button type="button" class="ghost small" data-copy-continue>Copy</button></div></div>`;
}

// ------------------------------------------------------------ conversations
// Your turns on one side, the agent's on the other. Prompts and your messages are shown as you
// typed them; only the agent's answers go through md().
const youBubble = (text, note = '') => `<div class="me"><div class="who">You</div><div class="plain">${esc(text)}</div>${note ? `<div class="muted small">${note}</div>` : ''}</div>`;
const agentBubble = (name, inner) => `<div class="agent"><div class="who">${esc(name)}</div>${inner}</div>`;

function messageBubbles(messages, agentName) {
  return (messages || []).map((m) => {
    if (m.role === 'user') return youBubble(m.text, m.undelivered ? 'not delivered: the session had ended' : '');
    return agentBubble(agentName, m.is_error ? `<p class="error">${esc(m.text)}</p>` : `<div class="md">${md(m.text)}</div>`);
  }).join('');
}

function turnHtml(t, current) {
  const id = Number(t.job_id);
  const head = `<div class="turn">${id === current ? `#${id} (this job)` : `<a href="#/job/${id}">#${id}</a>`}
    · ${esc(t.agent)}${t.model ? ` (${esc(t.model)})` : ''} · ${esc(t.autonomy)} · ${statusChip(t.status)}
    <span title="${esc(fullDate(t.created_at))}">${ago(t.created_at)}</span></div>`;
  const msgs = t.messages || [];
  const lastAgent = [...msgs].reverse().find((m) => m.role === 'agent');
  let answer = '';
  if (t.status === 'done' && t.result && !(lastAgent && lastAgent.text === t.result)) {
    answer = agentBubble(t.agent, `<div class="md">${md(t.result)}</div>`);
  } else if (t.error) {
    answer = agentBubble(t.agent, `<p class="error">${esc(t.error)}</p>`);
  } else if (t.status === 'queued' || t.status === 'running') {
    answer = agentBubble(t.agent, `<p class="muted">${t.status === 'queued' ? 'Waiting for the machine to pick this up…' : 'Working…'}</p>`);
  } else if (t.status === 'cancelled') {
    answer = agentBubble(t.agent, '<p class="muted">Cancelled.</p>');
  }
  return head + youBubble(t.prompt) + messageBubbles(msgs, t.agent) + answer;
}

function threadHtml(p) {
  const n = Number(p.threadLen) || 0;
  if (n <= 1 && !p.thread) return '';
  const label = p.thread ? 'Hide the conversation' : `Show as a conversation (${n} turns)`;
  let html = `<div class="thread-bar"><button type="button" class="linkish" data-thread aria-expanded="${p.thread ? 'true' : 'false'}">${label}</button></div>`;
  if (p.threadError) html += `<p class="error">${esc(p.threadError)}</p>`;
  else if (p.threadLoading && !p.thread) html += '<p class="muted small">Loading the conversation…</p>';
  if (p.thread) html += `<div class="card"><div class="chat">${(p.thread.turns || []).map((t) => turnHtml(t, Number(p.thread.current))).join('')}</div></div>`;
  return html;
}

async function loadThread(p) {
  p.threadLoading = true; p.threadError = '';
  setRegion(document.getElementById('job-thread'), threadHtml(p), true);
  try {
    p.thread = await api(`/api/jobs/${p.id}/thread`);
  } catch (e) {
    if (e instanceof StaleNav) return;
    p.threadError = e.message;
  } finally { p.threadLoading = false; }
  if (jobPage === p) setRegion(document.getElementById('job-thread'), threadHtml(p), true);
}

// ------------------------------------------------------------ live sessions
// A task started with "Keep the session open for messages": you type to the agent on its page,
// it answers each message in turn, until End session, the idle time or the time limit.
const isLiveSession = (j) => j.kind === 'task' && !!(j.options && j.options.live);
const sessionOpen = (j) => isLiveSession(j) && jobIsLive(j) && !j.end_requested && !j.cancel_requested;
const MAX_LOG_LINES = 5000;  // the hub keeps at most this many log lines per job

// The conversation in the order the agent had it: the prompt and its answer, then each of your
// messages and its answer (agent turn n+1 answers your message n). Messages it hasn't answered
// yet come last, the one it is on followed by "Working…".
function liveChatHtml(p, j) {
  const answers = new Map();
  const extra = [];
  for (const m of p.messages) {
    if (m.role !== 'agent') continue;
    const n = Number(m.turn_no);
    if (n >= 1 && !answers.has(n)) answers.set(n, m); else extra.push(m);
  }
  const users = p.messages.filter((m) => m.role === 'user');
  const working = j.status === 'running' && !j.cancel_requested;
  const answer = (m) => agentBubble(j.agent, m.is_error ? `<p class="error">${esc(m.text)}</p>` : `<div class="md">${md(m.text)}</div>`);
  const busy = agentBubble(j.agent, '<p class="muted">Working…</p>');
  const parts = [youBubble(j.prompt)];
  let waitingFor = answers.has(1) ? 0 : 1;  // the turn the agent is on, once found
  if (answers.has(1)) parts.push(answer(answers.get(1)));
  else if (working) parts.push(busy);
  users.forEach((m, i) => {
    const turn = i + 2;
    let note = '';
    if (m.undelivered) note = 'not delivered: the session had ended';
    else if (!answers.has(turn) && jobIsLive(j)) {
      if (!waitingFor && working) waitingFor = turn; else note = 'waiting for the agent';
    }
    parts.push(youBubble(m.text, note));
    if (answers.has(turn)) parts.push(answer(answers.get(turn)));
    else if (waitingFor === turn && working && !m.undelivered) parts.push(busy);
  });
  // Answers beyond your messages (none normally) still show, at the end.
  for (const [n, m] of answers) if (n > users.length + 1) extra.push(m);
  extra.forEach((m) => parts.push(answer(m)));
  return `<div class="chat live-chat">${parts.join('')}</div>`;
}

// The message form, built once per page and only shown or hidden afterwards, so a half-typed
// message is never lost.
function liveFormHtml(j) {
  const idle = Number((j.options || {}).idle_min) || 15;
  return `<form class="card live-form" id="live-form">
    <label for="live-text" style="margin-top:0">Message</label>
    <textarea id="live-text" maxlength="20000" required></textarea>
    <p class="error" id="live-error" hidden></p>
    <div class="row spread live-send"><span class="muted small">Ctrl+Enter sends</span><button type="submit">Send</button></div>
    <p class="muted small flush-bottom">The session stays open while you write. It ends after ${plural(idle, 'minute')} without a message,
      at the time limit, or when you press End session. Messages starting with / can't be sent here.</p></form>`;
}

// Under the conversation: how the session is ending or why it ended.
function liveNoteHtml(p, j) {
  if (!isLiveSession(j)) return '';
  let html = '';
  if (jobIsLive(j) && j.end_requested && !j.cancel_requested) {
    html += '<div class="notice">The session is ending: the agent answers the messages it already has, then the job finishes.</div>';
  } else if (!sessionOpen(j) && p.sessionEnd) {
    html += `<div class="notice">${esc(p.sessionEnd)}</div>`;
  }
  if (p.lastSeq >= MAX_LOG_LINES) html += '<p class="muted small">The log is cut; the conversation above is complete.</p>';
  return html;
}

function wireLiveForm(p) {
  const form = document.getElementById('live-form');
  const text = document.getElementById('live-text');
  const err = document.getElementById('live-error');
  text.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); form.requestSubmit(); }
  });
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    err.hidden = true;
    const btn = form.querySelector('button[type=submit]'); btn.disabled = true;
    try {
      await api(`/api/jobs/${p.id}/messages`, { method: 'POST', body: { text: text.value } });
      text.value = '';
      try { await drawJob(p); } catch (e) { /* the next refresh shows it */ }
    } catch (e) { err.textContent = e.message; err.hidden = false; } finally { btn.disabled = false; }
  });
}

// --------------------------------------------------------------- approvals
// An 'ask' job's question: the tool and its whole input, exactly as the agent would run it (hidden
// characters made visible), then what the agent says it is for. The card shows all of the input,
// however long, and Allow comes after it: nothing in the card scrolls on its own, because phones show
// no scroll bar until you scroll, so a box could hide the end of a command. Each card is built once
// and stays until the question is answered, denied in time or the job ends.
const PATH_FIELDS = ['file_path', 'notebook_path', 'path', 'url'];

// A line with nothing on it but characters that draw as empty space (and that visible() doesn't
// turn into codes) looks blank.
const BLANK_LINE_RE = /^[\t \u00a0\u1680\u2000-\u200a\u202f\u205f\u2800\u3000]*$/;
const lineCount = (text) => String(text).split('\n').length;

// Text in lines that wrap, never scroll sideways: each line of the text starts at the left edge
// and its wrapped part is indented, so a second line (a second command) can't pass for a wrap.
// Two or more blank lines in a row are drawn as one highlighted line that counts them, so blank
// lines can't pass for the end of the input; `seen.blank` then becomes true.
function wrappedLines(text, seen, cls, prefix = '') {
  const lines = String(text).split('\n');
  const out = [];
  for (let i = 0; i < lines.length; i += 1) {
    let end = i;
    while (end < lines.length && BLANK_LINE_RE.test(lines[end])) end += 1;
    if (end - i >= 2) {
      out.push(`<span class="ln">${i === 0 ? prefix : ''}<span class="hc blank">${end - i} blank lines</span></span>`);
      if (seen) seen.blank = true;
      i = end - 1;
    } else {
      out.push(`<span class="ln">${i === 0 ? prefix : ''}${visible(lines[i], seen)}</span>`);
    }
  }
  return `<pre class="${cls} a-lines">${out.join('')}</pre>`;
}

// A field's name, and how many lines it has when it has more than one.
const fieldKey = (name, lines) => `<div class="a-key">${name}${lines > 1 ? ` · ${lines} lines` : ''}</div>`;

function approvalFieldsHtml(input, seen) {
  const v = (s) => visible(s, seen);
  if (input === null || typeof input !== 'object' || Array.isArray(input)) {
    const text = typeof input === 'string' ? input : JSON.stringify(input, null, 2);
    const lines = lineCount(text);
    return `${lines > 1 ? `<div class="a-key">${lines} lines</div>` : ''}${wrappedLines(text, seen, 'a-text')}`;
  }
  // Every field, the command first; the agent's own description comes after the input.
  const keys = Object.keys(input).filter((k) => !(k === 'description' && typeof input[k] === 'string'));
  keys.sort((a, b) => (a === 'command' ? -1 : 0) - (b === 'command' ? -1 : 0));
  if (!keys.length) return '<p class="muted small">(no input)</p>';
  return keys.map((k) => {
    const val = input[k];
    // A path on one line is shown as code; one with line breaks, line by line like any other text.
    const text = typeof val === 'string' ? val : JSON.stringify(val, null, 2);
    let body;
    if (k === 'command' && typeof val === 'string') body = wrappedLines(val, seen, 'a-cmd', '$ ');
    else if (PATH_FIELDS.includes(k) && typeof val === 'string' && !val.includes('\n')) body = `<code class="a-path">${v(val)}</code>`;
    else if (typeof val === 'string') body = val ? wrappedLines(val, seen, 'a-text') : '<span class="muted small">(empty)</span>';
    else body = wrappedLines(text, seen, 'a-text');
    return `<div class="a-field">${fieldKey(v(k), lineCount(text))}${body}</div>`;
  }).join('');
}

const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

function approvalCardHtml(j, a, byAgent) {
  const seen = { hidden: false, blank: false };
  const id = Number(a.id);
  const tool = visible(a.tool_name, seen);
  const fields = approvalFieldsHtml(a.tool_input, seen);
  const input = a.tool_input && typeof a.tool_input === 'object' ? a.tool_input : {};
  const said = typeof input.description === 'string' && input.description
    ? `<p class="muted a-said">The agent says: ${visible(input.description, seen)}</p>` : '';
  let origin = '';
  if (byAgent && j.delegated_by) {
    origin = `<p class="small a-origin">Subtask of <a href="#/job/${Number(j.delegated_by)}">#${Number(j.delegated_by)}</a>: its prompt was written by an agent.</p>`;
  } else if (byAgent && j.parent_id) {
    origin = `<p class="small a-origin">Step of <a href="#/job/${Number(j.parent_id)}">council #${Number(j.parent_id)}</a>: its prompt may include text other agents wrote.</p>`;
  }
  const expires = Number(a.expires_at) || 0;
  const flags = (seen.hidden ? '<span class="status bad">hidden characters</span>' : '')
    + (seen.blank ? '<span class="status warn">blank lines</span>' : '');
  return `<div class="card approval" data-approval="${id}">
    <div class="a-head"><h3>${esc(j.agent)} wants to use ${tool}</h3>${flags ? `<span class="a-flags">${flags}</span>` : ''}</div>
    ${origin}
    ${seen.hidden ? '<p class="small">Some characters here don\'t show on screen. They are written as highlighted codes, such as <span class="hc">\\x1b</span> or <span class="hc">\\u202e</span>.</p>' : ''}
    ${seen.blank ? '<p class="small">Blank lines in a row are drawn as one highlighted line that counts them, such as <span class="hc blank">40 blank lines</span>. Read what comes after it.</p>' : ''}
    <div class="a-input">${fields}</div>
    ${said}
    <p class="small a-when">Denied automatically at ${esc(clock(expires))} if you don't answer <span class="muted" data-countdown="${expires}"></span></p>
    <label for="a-reason-${id}">Reason <span class="muted">(sent to the agent if you deny)</span></label>
    <input type="text" id="a-reason-${id}" maxlength="2000" autocomplete="off">
    <div class="row a-buttons"><button type="button" data-allow="${id}">Allow</button><button type="button" class="danger" data-deny="${id}">Deny</button></div>
    <p class="error small" data-a-error hidden></p></div>`;
}

// "(in 9:41)", counting down to when the hub denies the question.
function countdownText(expires) {
  const left = Math.round(expires - Date.now() / 1000);
  if (left <= 0) return '(time is up)';
  const h = Math.floor(left / 3600); const m = Math.floor((left % 3600) / 60); const s = left % 60;
  return `(in ${h ? `${h}:${String(m).padStart(2, '0')}` : m}:${String(s).padStart(2, '0')})`;
}

function tickCountdowns() {
  document.querySelectorAll('#job-approvals-pending [data-countdown]').forEach((el) => {
    const expires = Number(el.dataset.countdown) || 0;
    el.textContent = countdownText(expires);
    if (expires && expires <= Date.now() / 1000) {
      el.closest('.approval')?.querySelectorAll('[data-allow], [data-deny]').forEach((b) => { b.disabled = true; });
    }
  });
}

// Add a card for each new question and drop the cards of questions that are no longer waiting.
function drawApprovals(p, j, data) {
  const box = document.getElementById('job-approvals-pending');
  const list = Array.isArray(data.approvals) ? data.approvals : [];
  const pending = list.filter((a) => a.status === 'pending');
  const ids = new Set(pending.map((a) => String(a.id)));
  box.querySelectorAll('[data-approval]').forEach((card) => { if (!ids.has(card.dataset.approval)) card.remove(); });
  for (const a of pending) {
    if (!box.querySelector(`[data-approval="${Number(a.id)}"]`)) box.insertAdjacentHTML('beforeend', approvalCardHtml(j, a, !!data.asked_by_agent_prompt));
  }
  tickCountdowns();
  setRegion(document.getElementById('job-approvals-past'), pastApprovalsHtml(list.filter((a) => a.status !== 'pending')));
}

const APPROVAL_STATUS = { allowed: 'allowed', denied: 'denied', expired: 'no answer', cancelled: 'cancelled' };

// Questions already answered (the 20 newest). Their input is shown masked once the job has ended.
function pastApprovalsHtml(list) {
  if (!list.length) return '';
  return `<details class="a-past"><summary>Approvals (${list.length})</summary>${list.map((a) => {
    const input = a.tool_input && typeof a.tool_input === 'object' && !Array.isArray(a.tool_input) ? a.tool_input : null;
    const shown = input && typeof input.command === 'string' ? `$ ${input.command}`
      : typeof a.tool_input === 'string' ? a.tool_input : JSON.stringify(a.tool_input, null, 2);
    const word = own(APPROVAL_STATUS, a.status) || a.status;
    const why = a.message ? `<div class="small${a.status === 'denied' ? '' : ' muted'}">${a.status === 'denied' ? 'Reason: ' : ''}${visible(a.message)}</div>` : '';
    return `<div class="a-old"><div class="small"><span class="status ${esc(a.status)}">${esc(word)}</span> <strong>${visible(a.tool_name)}</strong>
      <span class="muted" title="${esc(fullDate(a.decided_at || a.created_at))}">${ago(a.decided_at || a.created_at)}</span></div>
      ${why}<pre class="a-text">${visible(shown ?? '')}</pre></div>`;
  }).join('')}</details>`;
}

async function answerApproval(p, b, decision) {
  const card = b.closest('.approval');
  const err = card.querySelector('[data-a-error]'); err.hidden = true;
  const buttons = card.querySelectorAll('[data-allow], [data-deny]');
  buttons.forEach((x) => { x.disabled = true; });
  const message = decision === 'deny' ? card.querySelector('input[type=text]').value.trim() : '';
  try {
    await api(`/api/jobs/${p.id}/approvals/${Number(b.dataset[decision])}`, { method: 'POST', body: { decision, message } });
    card.remove();
  } catch (e) {
    err.textContent = e.message; err.hidden = false;
    buttons.forEach((x) => { x.disabled = false; });
  }
  try { await drawJob(p); } catch (e) { /* the next refresh shows it */ }
}

// --------------------------------------------------------------- follow up
function followHtml(j) {
  const agent = agentInfo(j.runner, j.agent);
  const models = ((agent || {}).models || []).map((m) => `<option value="${esc(m)}">`).join('');
  const keep = j.model || (agent || {}).default_model || '';
  return `<form class="card follow" id="follow">
    <label for="follow-text" style="margin-top:0">Follow up <span class="muted">continues the same conversation on ${esc(j.runner)}</span></label>
    <textarea id="follow-text" required></textarea>
    <label>What the agent may do</label>
    ${autonomyRadios('follow-autonomy', j.autonomy, agent)}
    ${hasFeature(agent, 'live') ? liveOptionHtml('follow-live', isLiveSession(j), (j.options || {}).idle_min || 15, 'The time limit of an hour still applies.') : ''}
    <div id="follow-delegate-box"></div>
    <label for="follow-model">Model <span class="muted">${j.model ? `blank keeps ${esc(j.model)}` : "blank means the agent's default"}</span></label>
    <input type="text" id="follow-model" list="follow-models" placeholder="${esc(keep || 'default')}" autocomplete="off">
    <datalist id="follow-models">${models}</datalist>
    <p class="error" id="follow-error" hidden></p>
    <div class="row" style="margin-top:10px"><button type="submit">Send follow-up</button></div></form>`;
}

function wireFollow(p, j) {
  const form = document.getElementById('follow');
  if (!form) return;
  form.addEventListener('change', () => syncChoiceHints(form, 'follow-autonomy'));
  // Subtasks as on New task, preset from this job (a subtask's follow-up is your own task, so it starts unticked).
  const opts = j.options || {};
  const runner = state.runners.find((r) => r.name === j.runner);
  wireDelegate(document.getElementById('follow-delegate-box'), 'follow-delegate', delegateWant(j.delegated_by ? {} : opts), () => ({
    agent: agentInfo(j.runner, j.agent), runner: j.runner, ownKey: runner ? accountOf(runner, j.agent) : null, project: j.project || 'chat' }));
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const err = document.getElementById('follow-error'); err.hidden = true;
    const btn = form.querySelector('button[type=submit]'); btn.disabled = true;
    try {
      const res = await api('/api/tasks', { method: 'POST', body: {
        follow_up_of: j.id, runner: j.runner, agent: j.agent, project: j.project || 'chat',
        autonomy: form.querySelector('input[name="follow-autonomy"]:checked')?.value || 'readonly',
        // A blank model keeps this job's model (the hub no longer copies it).
        model: document.getElementById('follow-model').value.trim() || j.model || null,
        prompt: document.getElementById('follow-text').value,
        ...liveChoice('follow-live'),
        ...delegateChoice(document.getElementById('follow-delegate-box')),
      } });
      location.hash = `#/job/${res.id}`;
    } catch (e) { err.textContent = e.message; err.hidden = false; } finally { btn.disabled = false; }
  });
}

// ------------------------------------------------------------------ councils
function councilBodyHtml(j, data) {
  const m = j.meta || {};
  let html = `<div class="muted">${m.mode === 'debate' ? 'Debate' : 'Build + review'} · ${statusChip(j.status)} · started ${ago(j.started_at)} · ${duration(j)}</div>`;
  html += stageBar(m, j.status);
  if (j.status === 'done') html += `<div class="card"><h2 style="margin-top:0">Result</h2><div class="md">${md(j.result)}</div></div>`;
  if (j.error) html += `<p class="error">${esc(j.error)}</p>`;
  html += `<details><summary>The request</summary><pre>${esc(j.prompt)}</pre></details>`;
  const steps = data.steps || [];
  for (const stage of STAGES[m.mode] || []) {
    const inStage = steps.filter((s) => s.stage === stage);
    if (inStage.length) html += `<h2>${esc(stage)}</h2>${inStage.map(stepCard).join('')}`;
  }
  return html;
}

// --------------------------------------------------------------- the page
let jobPage = null;  // the job page on screen: its id, what it has shown so far and its own state

function appendLog(lines) {
  const el = document.getElementById('job-log');
  if (!el || !lines.length) return;
  const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
  const first = el.dataset.lines === undefined;
  if (first) el.textContent = '';
  el.append(document.createTextNode((first ? '' : '\n') + lines.join('\n')));
  el.dataset.lines = String(Number(el.dataset.lines || 0) + lines.length);
  if (atBottom) el.scrollTop = el.scrollHeight;
}

// Fetch what changed and redraw the regions that need it. True while the job is still going.
async function drawJob(p) {
  const askedFrom = p.lastMsg;
  const data = await api(`/api/jobs/${p.id}?since=${p.lastSeq}&messages_since=${askedFrom}`);
  if (jobPage !== p || (p.shown && !document.getElementById('job-page'))) return false;  // you left the page
  const j = data.job;
  if (!p.shown) {
    view.innerHTML = jobShell(j);
    p.shown = true;
    wireJobPage(p);
  }
  p.job = j;
  // Two refreshes can overlap (Cancel redraws at once), so lines and messages already shown are skipped.
  const lines = [];
  for (const l of data.logs || []) {
    if (l.seq > p.lastSeq) {
      lines.push(l.line); p.lastSeq = l.seq;
      if (typeof l.line === 'string' && l.line.startsWith('⏹ ')) p.sessionEnd = l.line.slice(2).trim();  // why the session ended
    }
  }
  for (const m of data.messages || []) { if (Number(m.id) > p.lastMsg) { p.messages.push(m); p.lastMsg = Number(m.id); } }
  if (isLiveSession(j) && !jobIsLive(j) && !p.messagesFinal && askedFrom > 0) {
    // The hub marks the messages that never reached the agent when the job ends; read them all once more.
    p.messagesFinal = true;
    try {
      const again = await api(`/api/jobs/${p.id}?since=${p.lastSeq}&messages_since=0`);
      if (jobPage !== p) return false;
      p.messages = again.messages || p.messages;
      p.lastMsg = Math.max(p.lastMsg, ...p.messages.map((m) => Number(m.id) || 0));
    } catch (e) { if (e instanceof StaleNav) return false; }
  }
  setRegion(document.getElementById('job-actions'), jobActionsHtml(j));
  if (j.kind === 'council') {
    setRegion(document.getElementById('job-body'), councilBodyHtml(j, data));
    return jobIsLive(j);
  }
  setRegion(document.getElementById('job-meta'), jobMetaHtml(j));
  drawApprovals(p, j, data);
  p.tagCandidates = data.tag_candidates || null;
  p.creditsData = { retried_as: data.retried_as || null, credits_retry: data.credits_retry || null };
  setRegion(document.getElementById('job-notices'), jobNoticesHtml(p, j));
  setRegion(document.getElementById('job-messages'), isLiveSession(j) ? liveChatHtml(p, j)
    : p.messages.length ? `<div class="chat">${messageBubbles(p.messages, j.agent)}</div>` : '');
  if (sessionOpen(j) && !p.liveShown) {
    document.getElementById('job-live').innerHTML = liveFormHtml(j);
    p.liveShown = true;
    wireLiveForm(p);
  }
  const liveForm = document.getElementById('live-form');
  if (liveForm) liveForm.hidden = !sessionOpen(j);
  setRegion(document.getElementById('job-live-note'), liveNoteHtml(p, j));
  setRegion(document.getElementById('job-result'), jobResultHtml(j, p));
  setRegion(document.getElementById('job-usage'), jobUsageHtml(j));
  const subtasks = Array.isArray(data.subtasks) ? data.subtasks : [];
  setRegion(document.getElementById('job-subtasks'), subtasksHtml(j, subtasks));
  p.continueCommand = data.continue_command || '';
  setRegion(document.getElementById('job-continue'), continueHtml(j, p.continueCommand));
  if (!p.followShown && canFollowUp(j)) {
    // Built once and never redrawn, so nothing typed into it is lost.
    document.getElementById('job-follow').innerHTML = followHtml(j);
    p.followShown = true;
    wireFollow(p, j);
  }
  const reload = p.thread && (p.threadLen !== data.thread_len || p.threadStatus !== j.status);
  p.threadLen = data.thread_len; p.threadStatus = j.status;
  setRegion(document.getElementById('job-thread'), threadHtml(p));
  if (reload) loadThread(p);
  appendLog(lines);
  // Subtasks can still run after the job that handed them out has finished.
  return jobIsLive(j) || subtasks.some(jobIsLive);
}

function wireJobPage(p) {
  const subtasksBox = document.getElementById('job-subtasks');
  if (subtasksBox) openJobsOnClick(subtasksBox);
  document.getElementById('job-page').addEventListener('click', async (ev) => {
    const b = ev.target.closest('button');
    if (!b) return;
    const j = p.job;
    if (b.id === 'cancel') {
      b.disabled = true;
      try { await api(`/api/jobs/${p.id}/cancel`, { method: 'POST' }); p.actionError = ''; } catch (e) { p.actionError = e.message; b.disabled = false; }
      try { await drawJob(p); } catch (e) { /* the next refresh shows it */ }
    } else if (b.id === 'retry-credits') {
      b.disabled = true;
      try {
        const res = await api(`/api/jobs/${p.id}/retry-on-credits`, { method: 'POST', body: {} });
        location.hash = `#/job/${Number(res.id)}`;
        return;
      } catch (e) { p.actionError = e.message; b.disabled = false; }
      try { await drawJob(p); } catch (e) { /* the next refresh shows it */ }
    } else if (b.id === 'end-session') {
      b.disabled = true;
      try { await api(`/api/jobs/${p.id}/end`, { method: 'POST', body: {} }); p.actionError = ''; } catch (e) { p.actionError = e.message; b.disabled = false; }
      try { await drawJob(p); } catch (e) { /* the next refresh shows it */ }
    } else if (b.dataset.allow !== undefined) {
      await answerApproval(p, b, 'allow');
    } else if (b.dataset.deny !== undefined) {
      await answerApproval(p, b, 'deny');
    } else if (b.id === 'rerun') {
      const opts = j.options || {};
      // A job aimed at a tag runs again for the tag (and its account), not on the machine that took it.
      const where = j.target_tag ? { tag: j.target_tag, account: typeof opts.account === 'string' ? opts.account : null } : { runner: j.runner };
      const scope = opts.delegate && !j.delegated_by ? { delegate: true, delegate_to: opts.delegate_to, delegate_projects: opts.delegate_projects,
        delegate_accounts: opts.delegate_accounts, delegate_agents: opts.delegate_agents } : {};
      sessionStorage.setItem('prefill', JSON.stringify({ ...where, agent: j.agent, project: j.project, model: j.model, prompt: j.prompt,
        autonomy: j.autonomy, live: !!opts.live, idle_min: opts.idle_min, ...scope }));
      location.hash = '#/task';
    } else if (b.dataset.copyContinue !== undefined) {
      copyText(p.continueCommand, b);
    } else if (b.dataset.thread !== undefined) {
      if (p.thread) { p.thread = null; p.threadError = ''; setRegion(document.getElementById('job-thread'), threadHtml(p), true); } else loadThread(p);
    }
  });
}

async function renderJob(id) {
  stopTimer();
  await loadState();
  const p = { id, lastSeq: 0, lastMsg: 0, messages: [], shown: false, job: null, continueCommand: '',
    followShown: false, thread: null, threadLen: 0, threadStatus: '', threadLoading: false, threadError: '', actionError: '',
    liveShown: false, sessionEnd: '', messagesFinal: false };
  jobPage = p;
  const keepGoing = await drawJob(p);
  // The countdowns on approval cards tick every second, while this page is on screen.
  const ticker = setInterval(() => {
    if (jobPage !== p || !document.getElementById('job-page')) { clearInterval(ticker); return; }
    tickCountdowns();
  }, 1000);
  if (keepGoing) {
    every(2000, async () => {
      if (location.hash !== `#/job/${id}` || jobPage !== p) return stopTimer();
      const still = await drawJob(p);
      if (!still) stopTimer();  // the last refresh already shows the finished job
    });
  }
}

// ----------------------------------------------------------------- machine
function authCard(r) {
  if (r.auth === 'machine') {
    return `<div class="small" style="margin-top:8px"><span class="chip ok">own token</span> It signs in to the hub with its own token.
      Forget (at the bottom of this page) revokes it, and no other machine is affected.</div>`;
  }
  if (r.auth === 'shared') {
    return `<div class="small" style="margin-top:8px"><span class="chip warn">shared token</span> It signs in with the hub's shared runner token.
      If a machine on the shared token is lost, the token has to change on all of them.
      To give this one its own token, make a setup command for it and run that command on this machine (it keeps its name and projects).
      <button type="button" class="ghost small" id="re-enroll">Make a setup command for ${esc(r.name)}</button></div>`;
  }
  return '';
}

// The Account column: the label the machine reports for the agent (shown, not used to route
// work until you accept it), or "(no label)".
function agentAccountHtml(r, a) {
  const label = typeof a.account === 'string' ? a.account : '';
  if (!label) return '<span class="muted small">(no label)</span>';
  const waiting = r.to_accept && r.to_accept.accounts && Object.prototype.hasOwnProperty.call(r.to_accept.accounts, a.name);
  return `${esc(label)}${waiting ? ' <span class="muted small">not accepted</span>' : ''}`;
}

// The Limits column: "5h 31% · week 47%" and how old the figures are. Blank for agents that
// report no plan limits (API keys, other agents).
function agentLimitsHtml(a, now) {
  if (!hasFeature(a, 'limits')) return '';
  const lim = a.limits && typeof a.limits === 'object' ? a.limits : {};
  const windows = lim.windows && typeof lim.windows === 'object' ? lim.windows : {};
  const ids = Object.keys(windows).sort(byWindow);
  const list = ids.map((id) => windows[id]);
  if (!ids.length && !lim.status) {
    return hasFeature(a, 'usage_check') ? '<span class="muted small">no reading yet</span>'
      : '<span class="muted small" title="Check limits needs a full claude.ai login, so only its jobs bring figures">no reading yet: run a job</span>';
  }
  const parts = [];
  let level = '';
  const raise = (l) => { if (l === 'bad' || (l === 'warn' && level !== 'bad')) level = l; };
  for (const id of ids) {
    const w = windows[id];
    if (isExpired(w, now) || w.used === null || w.used === undefined) continue;
    parts.push(`${windowShort(id)} ${percent(w.used)}%`);
    raise(windowLevel(w));
  }
  if (limitReachedUntil(lim.status, list, now) !== undefined) { parts.push('limit reached'); raise('bad'); }
  const newest = Math.max(0, ...list.map((w) => Number(w.observed_at) || 0), Number((lim.status || {}).observed_at) || 0);
  const figures = parts.length ? `<span class="chip ${level}">${esc(parts.join(' · '))}</span>` : '<span class="muted small">reset since the last reading</span>';
  return `${figures} <span class="muted small" title="${esc(fullDate(newest))}">${ago(newest)}</span>`;
}

// A machine's credits agent: which organization pays, and its state (or why it can't be used).
function machineCreditHtml(m) {
  if (!m.org) return `<div class="small"><span class="chip warn">credits agent</span> <span class="muted">not used: ${esc(m.problem || '')}</span></div>`;
  return `<div class="small"><span class="chip credits" title="The hub may run background work on it, paid by platform credits">credits agent</span>
    ${esc(m.org)} · ${creditStateChip(m.state)}</div>`;
}

// --------------------------------------------------------- Remote Control
// Anthropic's own Remote Control (claude remote-control), started and stopped on a machine from
// the dashboard. The machine reports its servers in info.remote_control.
function rcEntries(r) {
  const list = ((r && r.info) || {}).remote_control;
  return Array.isArray(list) ? list.filter((e) => e && typeof e === 'object') : [];
}
const rcKey = (runner, agent, project) => `${runner}\n${agent}\n${project}`;
const rcTime = (ts) => (Number(ts) ? whenLocal(Number(ts)) : '—');
const CLAUDE_URL_RE = /^https:\/\/claude\.ai\//;

// Queue Start or Stop Remote Control on a machine; the admin job's id.
async function rcAction(runner, action, agent, project) {
  const res = await api(`/api/runners/${encodeURIComponent(runner)}/actions`, { method: 'POST', body: { action, params: { agent, project } } });
  return Number(res.id);
}

// The command that starts the agent's CLI on its machine, for "<command> auth login" (as the hub says it).
function agentCommand(a) {
  if (a.preset === 'claude') return /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(a.name) ? a.name : 'claude';
  return a.preset === 'gemini' || a.preset === 'selftest' ? a.preset : a.name;
}

// Why a Claude agent here can't run Remote Control, or '' (it can, or it isn't a Claude agent).
function rcWhyNot(r, a) {
  if (hasFeature(a, 'remote_control') || (a.preset !== 'claude' && a.preset !== 'selftest')) return '';
  if (a.auth === 'api_key') return `${a.name} signs in with an API key, and Remote Control works only with a Claude subscription login.`;
  if (a.auth === 'token') return `${a.name} signs in with a long-lived token (claude setup-token). Remote Control needs a full login: run "${agentCommand(a)} auth login" on ${r.name}.`;
  if (a.auth === 'other') return `${a.name} uses another service than Anthropic's own (Bedrock, Vertex, Foundry or another API address in ANTHROPIC_BASE_URL), and Remote Control works only with Anthropic's own service.`;
  return '';
}

const RC_STATE_WORDS = { starting: 'starting', running: 'running', failed: 'failed', stopped: 'stopped' };

function rcTableHtml(r) {
  const list = rcEntries(r);
  if (!list.length) return '<p class="muted small">Not running on this machine.</p>';
  return `<div class="table-wrap"><table class="stack rc-table"><thead><tr><th>Agent</th><th>Project</th><th>State</th><th>Since</th><th>Stops at</th><th></th></tr></thead><tbody>${list.map((e) => {
    const state = own(RC_STATE_WORDS, e.state) || 'unknown';
    const on = e.state === 'starting' || e.state === 'running';
    const open = on && typeof e.url === 'string' && CLAUDE_URL_RE.test(e.url)
      ? `<a class="button ghost small" href="${esc(e.url)}" target="_blank" rel="noopener noreferrer">Open</a>` : '';
    const stop = on ? `<button type="button" class="ghost small" data-rc-stop data-agent="${esc(e.agent)}" data-project="${esc(e.project)}">Stop</button>` : '';
    let detail = '';
    if (e.state === 'failed' && e.error) {
      // The first line says what went wrong; the rest (what to run, its last lines) folds away.
      const [first, ...rest] = String(e.error).split('\n');
      detail = `<div class="error small rc-detail">${esc(first)}</div>${rest.join('').trim()
        ? `<details class="rc-more"><summary class="small">More</summary><div class="small rc-detail">${esc(rest.join('\n').trim())}</div></details>` : ''}`;
    }
    else if (e.note) detail = `<div class="muted small rc-detail">${esc(e.note)}</div>`;
    return `<tr><td><strong>${esc(e.agent)}</strong></td><td><span class="phone-only muted small">in </span>${esc(e.project)}</td>
      <td><span class="status ${esc(state)}">${esc(state)}</span>${detail}</td>
      <td class="small"><span class="phone-only muted">since </span><span title="${esc(fullDate(e.since))}">${ago(e.since)}</span></td>
      <td class="small${on ? '' : ' rc-none'}"><span class="phone-only muted">stops at </span>${on ? esc(rcTime(e.stops_at)) : '—'}</td>
      <td><div class="row rc-act">${open}${stop}</div></td></tr>`;
  }).join('')}</tbody></table></div>`;
}

// The machine page's Remote Control section: shown when an agent here can run it, or is a Claude
// agent that can't (to say why).
function rcSectionHtml(r) {
  const agents = r.agents || [];
  const able = agents.filter((a) => hasFeature(a, 'remote_control'));
  const why = agents.map((a) => rcWhyNot(r, a)).filter(Boolean);
  if (!able.length && !why.length) return '';
  const info = r.info || {};
  const hours = Number(info.remote_control_max_hours) || 8;
  let start = '';
  if (able.length && info.allow_remote_control !== true) {
    start = `<p class="small">This machine doesn't allow Remote Control from the dashboard. To change that, set
      <code>allow_remote_control</code> to <code>true</code> in its runner.json (guide 5).</p>`;
  } else if (able.length) {
    start = `<form id="rc-form" class="rc-form">
      <label class="part"><span>Agent</span><select id="rc-agent">${able.map((a) => `<option value="${esc(a.name)}">${esc(a.label || a.name)}</option>`).join('')}</select></label>
      <label class="part"><span>Project</span><select id="rc-project">${(r.projects || []).map((p) => `<option value="${esc(p)}">${esc(p)}</option>`).join('')}</select></label>
      <button type="submit" ${r.online ? '' : 'disabled title="The machine is offline"'}>Start Remote Control</button></form>
      <p class="muted small">${r.online ? '' : 'The machine is offline, so Remote Control can\'t start now. '}Each server stops by itself after ${plural(hours, 'hour')}
        (<code>remote_control_max_hours</code> in its runner.json), when you press Stop, or when the runner stops.</p>`;
  }
  return `<h2>Remote Control</h2>
    <div class="card rc" id="rc-card">
      <p class="small flush">Drive Claude on this machine from claude.ai/code or the Claude app. This is Anthropic's own Remote Control;
        it works with a full subscription login only. While it runs, anyone signed in to that Claude account can use Claude in the
        project folder here and approve its commands, and none of that shows in the hub.</p>
      <div id="rc-table">${rcTableHtml(r)}</div>
      <div id="rc-msg"></div>
      ${start}
      ${why.length ? `<ul class="small rc-why">${why.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}
      ${able.length ? `<p class="muted small">One session works in the project folder itself; new sessions get their own git worktree.</p>
      <p class="muted small flush-bottom">The first time on each machine, Claude Code asks two questions that need a terminal. If Start reports that,
        run the command it shows once, then press Start again.</p>` : ''}
    </div>`;
}

// What happened to a Start or Stop: waiting, then the runner's answer in full (an error says
// what to do).
function rcMsgHtml(m) {
  if (!m) return '';
  const link = m.id ? `<a href="#/job/${Number(m.id)}">job #${Number(m.id)}</a>` : '';
  if (m.error) return `<div class="notice bad"><div class="plain">${esc(m.error)}</div>${link ? `<div class="small">${link}</div>` : ''}</div>`;
  if (m.result) return `<div class="notice ok"><div class="plain">${esc(m.result)}</div>${link ? `<div class="small">${link}</div>` : ''}</div>`;
  return `<div class="notice">${esc(m.waiting)} ${link}</div>`;
}

// Start and Stop stay on the machine page: the section shows "Starting…", then what the machine
// answered, and its table follows the machine's check-ins.
let machinePage = null;
function wireRemoteControl(name) {
  const mp = { name, job: null, msg: null };
  machinePage = mp;
  const msgBox = document.getElementById('rc-msg');
  const show = () => setRegion(msgBox, rcMsgHtml(mp.msg), true);
  const run = async (action, agent, project, waiting) => {
    mp.msg = { waiting: 'Sending…' }; show();
    try {
      const id = await rcAction(name, action, agent, project);
      mp.job = id; mp.msg = { id, waiting };
    } catch (e) { mp.job = null; mp.msg = { error: e.message }; }
    show();
  };
  document.getElementById('rc-card').addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-rc-stop]');
    if (!b) return;
    b.disabled = true;
    run('rc.stop', b.dataset.agent, b.dataset.project, `Stopping Remote Control for ${b.dataset.agent} in ${b.dataset.project}…`);
  });
  document.getElementById('rc-form')?.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const btn = ev.currentTarget.querySelector('button[type=submit]'); btn.disabled = true;
    const agent = document.getElementById('rc-agent').value; const project = document.getElementById('rc-project').value;
    await run('rc.start', agent, project, `Starting Remote Control for ${agent} in ${project}… This takes up to 15 seconds once the machine picks it up.`);
    btn.disabled = false;
  });
  every(3000, async () => {
    if (machinePage !== mp || !document.getElementById('rc-card')) { stopTimer(); return; }
    await loadState();
    const r = state.runners.find((x) => x.name === name);
    if (r) setRegion(document.getElementById('rc-table'), rcTableHtml(r));
    if (mp.job) {
      const id = mp.job;
      const data = await api(`/api/jobs/${id}`);
      if (mp.job !== id || jobIsLive(data.job)) return;
      const j = data.job;
      mp.job = null;
      mp.msg = j.status === 'done' ? { id, result: j.result || 'Done.' } : { id, error: j.error || (j.status === 'cancelled' ? 'Cancelled.' : 'It didn\'t work.') };
      show();
    }
  });
}

// ------------------------------------------------------- tags and labels
// What a machine reports about itself counts for tag jobs and subtasks only once you accept it
// (platform tags such as linux count at once).
const toAcceptOf = (r) => (r && r.to_accept && typeof r.to_accept === 'object' ? r.to_accept : {});
const hasToAccept = (r) => {
  const w = toAcceptOf(r);
  return (Array.isArray(w.tags) && w.tags.length > 0) || (!!w.accounts && typeof w.accounts === 'object' && Object.keys(w.accounts).length > 0);
};
function acceptedOf(r) {
  const acc = r && r.accepted && typeof r.accepted === 'object' ? r.accepted : {};
  return { tags: Array.isArray(acc.tags) ? acc.tags.filter((t) => typeof t === 'string') : [],
    accounts: acc.accounts && typeof acc.accounts === 'object' && !Array.isArray(acc.accounts)
      ? Object.fromEntries(Object.entries(acc.accounts).filter(([, v]) => typeof v === 'string')) : {} };
}

// " · tags: linux gpu build · not accepted" for the machine page's header line (tagChips).
function machineTagsHtml(r) {
  const chips = tagChips(r);
  return chips ? ` · <span class="m-tags">tags: ${chips}</span>` : '';
}

function acceptSectionHtml(r) {
  const waiting = toAcceptOf(r);
  const wTags = Array.isArray(waiting.tags) ? waiting.tags.filter((t) => typeof t === 'string') : [];
  const wAccounts = waiting.accounts && typeof waiting.accounts === 'object' ? Object.entries(waiting.accounts).filter(([, v]) => typeof v === 'string') : [];
  const acc = acceptedOf(r);
  const aAccounts = Object.entries(acc.accounts);
  if (!wTags.length && !wAccounts.length && !acc.tags.length && !aAccounts.length) return '';
  const reported = Array.isArray(r.tags) ? r.tags : [];
  const sharedNote = r.auth !== 'machine' ? `<p class="muted small">${esc(r.name)} still uses the shared runner token. Tasks for a tag and subtasks only go to
    machines with their own token, so they don't come here until you give it one (Make a setup command, above).</p>` : '';
  let html = '<h2>Tags and account labels</h2>';
  if (wTags.length || wAccounts.length) {
    const tagBoxes = wTags.map((t) => `<label class="check"><input type="checkbox" data-accept-tag value="${esc(t)}" checked> <span>tag <span class="chip tag">${esc(t)}</span></span></label>`);
    const labelBoxes = wAccounts.map(([agent, label]) => {
      const before = own(acc.accounts, agent);
      return `<label class="check"><input type="checkbox" data-accept-agent="${esc(agent)}" value="${esc(label)}" checked> <span>${esc(agent)} → ${esc(label)}${before ? ` <span class="muted small">(instead of ${esc(before)})</span>` : ''}</span></label>`;
    });
    html += `<div class="card to-accept"><h3>To accept</h3>
      <p class="small flush">This machine reports tags or account labels you haven't accepted. Tasks for a tag and subtasks use only accepted ones.</p>
      <div class="dg-checks">${tagBoxes.concat(labelBoxes).join('')}</div>
      <p class="muted small">Accept an account label only when it is that very account: every machine with the same label counts as one account.</p>
      ${sharedNote}
      <div class="row"><button type="button" class="small" id="accept-btn">Accept</button></div></div>`;
  }
  if (acc.tags.length || aAccounts.length) {
    const x = (attr, value, what) => `<button type="button" class="chip-x" ${attr}="${esc(value)}" aria-label="Withdraw ${esc(what)}" title="Withdraw ${esc(what)}">×</button>`;
    const tags = acc.tags.map((t) => `<span class="chip tag">${esc(t)}${reported.includes(t) ? '' : ' · not reported now'}${x('data-withdraw-tag', t, `the tag ${t}`)}</span>`);
    const labels = aAccounts.map(([agent, label]) => `<span class="chip">${esc(agent)} → ${esc(label)}${x('data-withdraw-agent', agent, `the label ${label} of ${agent}`)}</span>`);
    html += `<div class="card accepted"><h3>Accepted</h3>
      ${tags.length ? `<div class="small">Tags: ${tags.join('')}</div>` : ''}
      ${labels.length ? `<div class="small">Account labels: ${labels.join('')}</div>` : ''}
      <p class="muted small flush-bottom">Platform tags (linux, macos, windows) count without accepting. × withdraws one: tasks for a withdrawn tag
        stop coming here, and an agent whose label you withdraw counts as its own account (${esc(r.name)}/&lt;agent&gt;).</p>
      ${wTags.length || wAccounts.length ? '' : sharedNote}</div>`;
  }
  return `${html}<p class="error" id="accept-error" hidden></p>`;
}

// Accept ticked tags and labels, or withdraw one: the hub takes exactly what is posted.
function wireAccept(r) {
  const err = document.getElementById('accept-error');
  if (!err) return;
  const post = async (tags, accounts, b) => {
    err.hidden = true;
    if (b) b.disabled = true;
    try {
      await api(`/api/runners/${encodeURIComponent(r.name)}/accept`, { method: 'POST', body: { tags, accounts } });
      if (location.hash === `#/machine/${encodeURIComponent(r.name)}`) await renderMachine(r.name);
    } catch (e) {
      err.textContent = e.message; err.hidden = false;
      if (b) b.disabled = false;
    }
  };
  document.getElementById('accept-btn')?.addEventListener('click', (ev) => {
    const acc = acceptedOf(r);
    const tags = new Set(acc.tags);
    const accounts = new Map(Object.entries(acc.accounts));
    view.querySelectorAll('input[data-accept-tag]:checked').forEach((c) => tags.add(c.value));
    view.querySelectorAll('input[data-accept-agent]:checked').forEach((c) => accounts.set(c.dataset.acceptAgent, c.value));
    post([...tags].sort(), Object.fromEntries(accounts), ev.currentTarget);
  });
  view.querySelector('.card.accepted')?.addEventListener('click', (ev) => {
    const b = ev.target.closest('button.chip-x');
    if (!b) return;
    const acc = acceptedOf(r);
    const accounts = Object.entries(acc.accounts).filter(([agent]) => agent !== b.dataset.withdrawAgent);
    post(acc.tags.filter((t) => t !== b.dataset.withdrawTag), Object.fromEntries(accounts), b);
  });
}

async function renderMachine(name) {
  stopTimer();
  await loadState();
  const r = state.runners.find((x) => x.name === name);
  if (!r) { view.innerHTML = `<h1>${esc(name)}</h1><div class="card empty">No machine with that name has checked in.</div>`; return; }
  const info = r.info || {};
  const paths = info.project_paths || {};
  const remote = !!info.remote_projects;
  const clash = clashOf(r);
  const projectRows = (r.projects || []).map((p) => `<tr><td><strong>${esc(p)}</strong></td><td class="small"><code class="path">${esc(paths[p] || '')}</code></td>
    <td>${remote && p !== 'chat' ? `<div class="row"><button class="ghost small" data-act="project.pull" data-name="${esc(p)}">Pull</button>
    <button class="ghost small" data-act="project.remove" data-name="${esc(p)}">Remove</button></div>` : ''}</td></tr>`).join('');
  const now = Date.now() / 1000;
  const agents = r.agents || [];
  const anyLimits = agents.some((a) => hasFeature(a, 'limits'));
  const canCheck = agents.some((a) => hasFeature(a, 'usage_check'));
  const credit = ((state.credits || {}).machines || []).find((m) => m.runner === r.name) || null;
  const agentRows = agents.map((a) => `<tr><td><strong>${esc(a.name)}</strong><div class="muted small">${esc(a.label || '')}</div>${credit && credit.agent === a.name ? machineCreditHtml(credit) : ''}</td>
    <td>${a.ready ? '<span class="chip ok">ready</span>' : '<span class="chip warn">not ready</span>'} <span class="muted small">${esc(a.note || '')}</span></td>
    <td class="m-account"><span class="phone-only muted small">Account: </span>${agentAccountHtml(r, a)}</td>
    <td class="m-limits">${hasFeature(a, 'limits') ? `<span class="phone-only muted small">Limits: </span>${agentLimitsHtml(a, now)}` : ''}</td>
    <td class="small">${a.default_model ? `default <code>${esc(a.default_model)}</code>` : 'CLI default'}${(a.models || []).length ? ` · suggestions: ${(a.models || []).map((m) => `<code>${esc(m)}</code>`).join(' ')}` : ''}</td></tr>`).join('');
  view.innerHTML = `
    <div class="row spread"><h1 class="m-title"><span class="dot ${r.online ? 'on' : ''}"></span> ${esc(r.name)}</h1>
      <span class="muted">${r.online ? 'online' : 'offline · last seen ' + ago(r.last_seen)}</span></div>
    <div class="muted m-line">${esc(r.os)} · ${esc(r.hostname)} · ${busyOf(r)}/${slotsOf(r)} busy${machineTagsHtml(r)}${takesSubtasks(r)
      ? ' · <span class="chip" title="Agents on your other machines may hand it subtasks, in tasks where you allow that (accept_delegated_tasks)">takes subtasks</span>' : ''}</div>
    ${clash ? `<div class="notice"><strong>Two computers check in as “${esc(r.name)}”:</strong> ${esc(r.hostname || '?')} and ${esc(clash)}.
      This page shows whichever checked in last, and each can pick up jobs meant for the other.
      Give one of them another name: click Add a machine, type a new name, and run the command it shows on that
      computer. (Editing the name in its runner.json only works for a computer on the shared token.)</div>` : ''}

    <h2>Runner</h2>
    <div class="card">
      <div class="row spread">
        <div>Version <strong>${esc(r.version)}</strong>${state.runner_latest ? ` · hub has <strong>${esc(state.runner_latest)}</strong>` : ''}
          ${info.restart_pending ? ' · <span class="chip warn">restart pending</span>' : ''}</div>
        ${info.remote_update ? `<button class="small" id="update" ${r.online ? '' : 'disabled'}>${outdated(r) ? 'Update now' : 'Reinstall'}</button>`
          : '<span class="muted small">Remote updates are off for this machine.</span>'}
      </div>
      ${authCard(r)}
    </div>
    ${acceptSectionHtml(r)}

    <h2>Shared context</h2>
    <div class="card">
      <div class="row spread"><div>${info.manage_agent_settings === false ? '<span class="chip warn">off on this machine</span> Shared notes, skills and MCP servers aren\'t added to its agents (manage_agent_settings).'
        : info.context_synced === state.context_token ? '<span class="chip ok">up to date</span> Pinned notes, /remember, add-ons and MCP servers are in place.'
        : '<span class="chip warn">behind</span> It picks up the latest changes within about 15 seconds of checking in.'}</div>
        <button class="ghost small" id="sync" ${r.online ? '' : 'disabled'}>Sync now</button></div>
      ${(info.sync_notes || []).length ? `<ul class="small muted">${info.sync_notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul>` : ''}
    </div>

    <div class="row spread"><h2>Agents</h2>${canCheck ? `<button class="ghost small" id="check-limits" ${r.online ? 'title="Run Claude Code\'s /usage for each Claude agent here that can read its plan limits"' : 'disabled title="The machine is offline"'}>Check limits</button>` : ''}</div>
    <div class="card table-wrap"><table class="stack agents"><thead><tr><th>Agent</th><th>Status</th><th>Account</th><th>Limits</th><th>Models</th></tr></thead><tbody>${agentRows || '<tr><td colspan="5" class="muted">none</td></tr>'}</tbody></table>
      <p class="muted small">Agents, logins, account labels and model suggestions are set on the machine itself (<code>runner.py init</code> or <code>runner.json</code>).${anyLimits ? ' Limits shows how much of each Claude plan window is used, from the last job or Check limits; the <a href="#/usage">Usage page</a> shows them per account.' : ''}</p></div>

    <h2>Projects</h2>
    <div class="card table-wrap"><table class="stack"><thead><tr><th>Name</th><th>Folder</th><th></th></tr></thead><tbody>${projectRows}</tbody></table>
      ${remote ? `<p class="muted small">New projects go in <code>${esc(info.projects_root || '')}</code>. Removing a project only stops offering it; its files stay on the machine.</p>`
        : '<p class="muted small">Managing projects from the dashboard is off for this machine (<code>allow_remote_projects</code> in its runner.json).</p>'}
    </div>
    ${remote ? `<div class="grid">
      <form class="card" id="clone-form"><h3>Clone a git repository</h3>
        <label>Repository address</label><input type="text" id="clone-url" required placeholder="https://github.com/you/repo.git">
        <label>Project name</label><input type="text" id="clone-name" required placeholder="repo">
        <p class="muted small">Private repositories need git credentials on that machine (see the troubleshooting guide).</p>
        <button type="submit">Clone</button></form>
      <form class="card" id="create-form"><h3>New empty project</h3>
        <label>Project name</label><input type="text" id="create-name" required placeholder="my-idea">
        <label class="check"><input type="checkbox" id="create-git" checked> Start a git repository in it</label>
        <button type="submit">Create</button></form>
    </div>` : ''}
    <p class="error" id="m-error" hidden></p>
    ${rcSectionHtml(r)}

    <div class="row spread"><h2>Recent work here</h2><a class="small" href="#/history?runner=${encodeURIComponent(r.name)}">All work on this machine</a></div>
    <div id="m-jobs"><p class="muted">Loading…</p></div>

    <h2>Remove</h2>
    <div class="card row spread"><span class="small">${r.auth === 'machine'
      ? 'Forget removes this machine from the hub and revokes its token. To add it back, make a new setup command.'
      : 'Forget removes this machine from the list. While its runner still runs with the shared token, it comes back at its next check-in.'}</span>
      <button class="danger small" id="forget">Forget this machine</button></div>`;

  const act = async (action, params) => {
    const err = document.getElementById('m-error'); err.hidden = true;
    try {
      const res = await api(`/api/runners/${encodeURIComponent(name)}/actions`, { method: 'POST', body: { action, params } });
      location.hash = `#/job/${res.id}`;
    } catch (e) { err.textContent = e.message; err.hidden = false; err.scrollIntoView({ block: 'nearest' }); }
  };
  document.getElementById('update')?.addEventListener('click', () => act('runner.update', {}));
  document.getElementById('sync')?.addEventListener('click', () => act('sync', {}));
  document.getElementById('check-limits')?.addEventListener('click', () => act('usage.check', {}));
  document.getElementById('re-enroll')?.addEventListener('click', () => openAddMachine(r.name));
  wireAccept(r);
  document.getElementById('forget').addEventListener('click', async () => {
    try { if (await forgetMachine(r.name)) location.hash = '#/'; } catch (e) { const err = document.getElementById('m-error'); err.textContent = e.message; err.hidden = false; }
  });
  view.querySelectorAll('[data-act]').forEach((b) => b.addEventListener('click', () => {
    if (b.dataset.act === 'project.remove' && !confirm(`Stop offering ${b.dataset.name}? Its files stay on the machine.`)) return;
    act(b.dataset.act, { name: b.dataset.name });
  }));
  const urlIn = document.getElementById('clone-url'); const nameIn = document.getElementById('clone-name');
  urlIn?.addEventListener('input', () => {
    const guess = urlIn.value.trim().replace(/\/+$/, '').split(/[/:]/).pop().replace(/\.git$/, '');
    if (!nameIn.dataset.touched) nameIn.value = guess.replace(/[^A-Za-z0-9._-]/g, '-');
  });
  nameIn?.addEventListener('input', () => { nameIn.dataset.touched = '1'; });
  document.getElementById('clone-form')?.addEventListener('submit', (ev) => { ev.preventDefault(); act('project.clone', { url: urlIn.value, name: nameIn.value }); });
  document.getElementById('create-form')?.addEventListener('submit', (ev) => {
    ev.preventDefault();
    act('project.create', { name: document.getElementById('create-name').value, git: document.getElementById('create-git').checked ? 'yes' : 'no' });
  });
  if (document.getElementById('rc-card')) wireRemoteControl(name);
  const jobsBox = document.getElementById('m-jobs');
  openJobsOnClick(jobsBox);
  try {
    const data = await api(`/api/jobs?limit=8&runner=${encodeURIComponent(r.name)}`);
    if (document.getElementById('m-jobs') === jobsBox) jobsBox.innerHTML = jobTable(data.jobs, 'No work on this machine yet.');
  } catch (e) { jobsBox.innerHTML = `<p class="error small">${esc(e.message)}</p>`; }
}

// ----------------------------------------------------------------- history
// Every top-level job, newest first, a page at a time. The filters live in the address
// (#/history?runner=build-box&status=error), so a filtered list can be bookmarked or linked to.
const HISTORY_PAGE = 50;
const KIND_NAMES = { task: 'Tasks', council: 'Councils', admin: 'Machine admin' };
const STATUS_NAMES = ['queued', 'running', 'done', 'error', 'cancelled'];

async function renderHistory(query) {
  stopTimer();
  await loadState();
  const params = new URLSearchParams(query || '');
  const f = { runner: params.get('runner') || '', agent: params.get('agent') || '', kind: params.get('kind') || '', status: params.get('status') || '', q: params.get('q') || '' };
  const opt = (value, label, cur) => `<option value="${esc(value)}" ${value === cur ? 'selected' : ''}>${esc(label)}</option>`;
  view.innerHTML = `
    <h1>History</h1>
    <p class="muted">Every task, council and machine admin job, newest first. A council's steps are inside the council.</p>
    <form class="card filters" id="h-form">
      <label>Machine <select id="h-runner"></select></label>
      <label>Agent <select id="h-agent"></select></label>
      <label>Kind <select id="h-kind">${opt('', 'Everything', f.kind)}${Object.entries(KIND_NAMES).map(([k, l]) => opt(k, l, f.kind)).join('')}</select></label>
      <label>Status <select id="h-status">${opt('', 'Any', f.status)}${STATUS_NAMES.map((s) => opt(s, s, f.status)).join('')}</select></label>
      <label class="wide">Title contains <input type="search" id="h-q" value="${esc(f.q)}" autocomplete="off"></label>
    </form>
    <div id="h-list"><p class="muted">Loading…</p></div>
    <div class="row spread pager"><button type="button" class="ghost small" id="h-newer" disabled>← Newer</button>
      <span class="muted small" id="h-page"></span>
      <button type="button" class="ghost small" id="h-older" disabled>Older →</button></div>`;

  const cursors = [null];  // before_id of each page seen so far; the last one is the page shown
  let facets = null;
  const listBox = document.getElementById('h-list');
  openJobsOnClick(listBox);

  const fillChoices = () => {
    const runners = new Set([...(facets?.runners || []), ...state.runners.map((r) => r.name)]);
    const agents = new Set([...(facets?.agents || []), ...state.runners.flatMap((r) => (r.agents || []).map((a) => a.name))]);
    if (f.runner) runners.add(f.runner);
    if (f.agent) agents.add(f.agent);
    document.getElementById('h-runner').innerHTML = opt('', 'All machines', f.runner) + [...runners].sort().map((n) => opt(n, n, f.runner)).join('');
    document.getElementById('h-agent').innerHTML = opt('', 'All agents', f.agent) + [...agents].sort().map((n) => opt(n, n, f.agent)).join('');
  };
  fillChoices();

  let loadSeq = 0;  // only the newest request's answer is shown (filters, paging and refreshes overlap)
  const load = async () => {
    const mine = ++loadSeq;
    const before = cursors[cursors.length - 1];
    const qs = new URLSearchParams({ limit: String(HISTORY_PAGE) });
    Object.entries(f).forEach(([k, v]) => { if (v) qs.set(k, v); });
    if (before) qs.set('before_id', String(before));
    const data = await api(`/api/jobs?${qs}`);
    if (mine !== loadSeq || !listBox.isConnected) return;  // a newer request, or another page, took over
    if (data.runners && !facets) { facets = data; fillChoices(); }
    const filtered = Object.values(f).some(Boolean);
    listBox.innerHTML = jobTable(data.jobs, filtered ? 'No jobs match these filters.' : 'Nothing yet.');
    const page = cursors.length;
    document.getElementById('h-page').textContent = data.jobs.length
      ? `Page ${page} · #${data.jobs[0].id} to #${data.jobs[data.jobs.length - 1].id}` : (page > 1 ? `Page ${page}` : '');
    document.getElementById('h-newer').disabled = page <= 1;
    const older = document.getElementById('h-older');
    older.disabled = !data.has_more;
    older.dataset.next = data.next_before_id || '';
  };
  const apply = () => {
    const qs = new URLSearchParams();
    Object.entries(f).forEach(([k, v]) => { if (v) qs.set(k, v); });
    window.history.replaceState(null, '', `#/history${qs.toString() ? `?${qs}` : ''}`);
    cursors.length = 1;
    load().catch((e) => { if (!(e instanceof StaleNav)) listBox.innerHTML = `<p class="error">${esc(e.message)}</p>`; });
  };
  [['h-runner', 'runner'], ['h-agent', 'agent'], ['h-kind', 'kind'], ['h-status', 'status']].forEach(([id, key]) => {
    document.getElementById(id).addEventListener('change', (ev) => { f[key] = ev.target.value; apply(); });
  });
  let typing = null;
  document.getElementById('h-q').addEventListener('input', (ev) => { clearTimeout(typing); typing = setTimeout(() => { f.q = ev.target.value.trim(); apply(); }, 300); });
  document.getElementById('h-form').addEventListener('submit', (ev) => ev.preventDefault());
  document.getElementById('h-older').addEventListener('click', (ev) => {
    if (!ev.currentTarget.dataset.next) return;
    cursors.push(Number(ev.currentTarget.dataset.next)); load(); window.scrollTo(0, 0);
  });
  document.getElementById('h-newer').addEventListener('click', () => { if (cursors.length > 1) { cursors.pop(); load(); window.scrollTo(0, 0); } });
  await load();
  // Keep the first page current while jobs run; older pages don't change.
  every(10000, async () => {
    if (!location.hash.startsWith('#/history')) { stopTimer(); return; }
    if (cursors.length === 1) await load();
  });
}

// ------------------------------------------------------------------ memory
function scopeOptions(selected) {
  const agents = new Set(); const projects = new Set();
  for (const r of state.runners) { (r.agents || []).forEach((a) => agents.add(a.name)); (r.projects || []).forEach((p) => projects.add(p)); }
  const opts = [['global', 'Everyone (global)']]
    .concat([...agents].sort().map((a) => [`agent:${a}`, `Only agent ${a}`]))
    .concat([...projects].sort().filter((p) => p !== 'chat').map((p) => [`project:${p}`, `Project ${p}`]));
  if (selected && !opts.some(([v]) => v === selected)) opts.push([selected, selected]);
  return opts.map(([v, l]) => `<option value="${esc(v)}" ${v === selected ? 'selected' : ''}>${esc(l)}</option>`).join('');
}

function noteCard(n) {
  return `<div class="card note" data-id="${n.id}">
    <div class="row spread"><div class="row">
      ${n.pinned ? '<span class="chip ok" title="Always in the context of the agents in its scope">📌 pinned</span>' : ''}
      ${!n.pinned && (n.tags || '').split(',').includes('pin-requested') ? '<span class="chip warn" title="An agent asked for this to be pinned. Pin it if you agree.">pin requested</span>' : ''}
      <span class="chip">${esc(n.scope)}</span>${n.tags ? n.tags.split(',').map((t) => `<span class="chip">#${esc(t)}</span>`).join('') : ''}</div>
      <span class="muted small">${esc(n.source || '')} · ${ago(n.updated_at)}</span></div>
    <div class="md note-body">${md(n.text)}</div>
    <div class="row">
      <button class="ghost small" data-pin="${n.pinned ? 0 : 1}">${n.pinned ? 'Unpin' : 'Pin'}</button>
      <button class="ghost small" data-edit>Edit</button>
      <button class="danger small" data-del>Delete</button></div>
  </div>`;
}

async function renderMemory() {
  stopTimer();
  await loadState();
  view.innerHTML = `
    <h1>Memory</h1>
    <p class="muted">Notes shared by every agent and account on every machine. <strong>Pinned</strong> global and agent notes are
      loaded into every session's context automatically (keep them short; only you can pin). The rest are found when an
      agent uses <code>recall</code>, or here. Agents add notes with <code>/remember</code> and can ask you to pin one.</p>
    <form class="card" id="note-form">
      <label for="note-text" style="margin-top:0">New note</label>
      <textarea id="note-text" style="min-height:90px" required placeholder="e.g. build-box has the CUDA toolkit; its projects live in D:\\src. Prefer PowerShell 7 there."></textarea>
      <div class="row">
        <label style="margin:0">For <select id="note-scope">${scopeOptions('global')}</select></label>
        <label style="margin:0">Tags <input type="text" id="note-tags" placeholder="windows, gpu" style="width:180px"></label>
        <label class="check" style="margin:0"><input type="checkbox" id="note-pin"> Pin (always in context)</label>
        <button type="submit">Save</button>
      </div>
      <p class="error" id="note-error" hidden></p>
    </form>
    <div class="row" style="margin:14px 0 6px">
      <input type="text" id="note-q" placeholder="Search notes…" style="flex:1;min-width:180px">
      <select id="note-filter" style="width:auto"><option value="">All scopes</option>${scopeOptions('')}</select>
    </div>
    <div id="pinned-meter" class="muted small"></div>
    <div id="notes"></div>
    <details id="ctx-preview"><summary>What agents see (the pinned-notes files)</summary><div id="ctx-files" class="muted">Loading…</div></details>`;

  const load = async () => {
    const q = document.getElementById('note-q').value; const scope = document.getElementById('note-filter').value;
    const data = await api(`/api/notes?q=${encodeURIComponent(q)}&scope=${encodeURIComponent(scope)}`);
    const over = data.pinned_chars > data.pinned_warn;
    document.getElementById('pinned-meter').innerHTML = `Pinned notes: ${data.pinned_chars.toLocaleString()} characters${over ? ` <span class="chip warn">over ${data.pinned_warn.toLocaleString()}: every session pays for these, consider unpinning some</span>` : ''}`;
    document.getElementById('notes').innerHTML = data.notes.length ? data.notes.map(noteCard).join('') : '<div class="card empty">No notes yet.</div>';
    document.querySelectorAll('.note').forEach((card) => {
      const id = card.dataset.id;
      const report = (e) => { const err = document.getElementById('note-error'); err.textContent = e.message; err.hidden = false; window.scrollTo(0, 0); };
      card.querySelector('[data-pin]').addEventListener('click', async (ev) => {
        try { await api(`/api/notes/${id}`, { method: 'PUT', body: { pinned: ev.target.dataset.pin === '1' } }); load(); } catch (e) { report(e); }
      });
      card.querySelector('[data-del]').addEventListener('click', async () => {
        if (!confirm('Delete this note?')) return;
        try { await api(`/api/notes/${id}`, { method: 'DELETE' }); load(); } catch (e) { report(e); }
      });
      card.querySelector('[data-edit]').addEventListener('click', () => {
        const n = data.notes.find((x) => String(x.id) === id);
        card.innerHTML = `<textarea style="min-height:90px">${esc(n.text)}</textarea>
          <div class="row" style="margin-top:6px"><select>${scopeOptions(n.scope)}</select>
          <input type="text" value="${esc(n.tags)}" placeholder="tags" style="width:180px">
          <button class="small" data-save>Save</button><button class="ghost small" data-cancel>Cancel</button></div>`;
        card.querySelector('[data-cancel]').addEventListener('click', load);
        card.querySelector('[data-save]').addEventListener('click', async () => {
          try {
            await api(`/api/notes/${id}`, { method: 'PUT', body: { text: card.querySelector('textarea').value, scope: card.querySelector('select').value, tags: card.querySelector('input').value } });
            load();
          } catch (e) { report(e); }
        });
      });
    });
  };
  let t = null;
  document.getElementById('note-q').addEventListener('input', () => { clearTimeout(t); t = setTimeout(load, 250); });
  document.getElementById('note-filter').addEventListener('change', load);
  document.getElementById('note-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const err = document.getElementById('note-error'); err.hidden = true;
    try {
      await api('/api/notes', { method: 'POST', body: { text: document.getElementById('note-text').value, scope: document.getElementById('note-scope').value, tags: document.getElementById('note-tags').value, pinned: document.getElementById('note-pin').checked } });
      document.getElementById('note-text').value = ''; document.getElementById('note-tags').value = '';
      load();
    } catch (e) { err.textContent = e.message; err.hidden = false; }
  });
  document.getElementById('ctx-preview').addEventListener('toggle', async (ev) => {
    if (!ev.target.open) return;
    const c = await api('/api/notes/context');
    document.getElementById('ctx-files').innerHTML = Object.entries(c.files).map(([f, body]) => `<p><strong>${esc(f)}</strong></p><pre>${esc(body)}</pre>`).join('');
  });
  await load();
}

// --------------------------------------------------------------------- mcp
const kvToText = (obj, sep) => Object.entries(obj || {}).map(([k, v]) => `${k}${sep}${v}`).join('\n');
function textToKv(text, sep) {
  const out = {};
  for (const line of text.split('\n')) {
    const i = line.indexOf(sep);
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + sep.length).trim();
  }
  return out;
}

function mcpForm(srv, publicUrl) {
  const s = srv || { transport: 'http', route: 'hub', auth: 'headers', targets: ['claude', 'gemini', 'antigravity'], platforms: ['linux', 'macos', 'windows'], enabled: true, headers: {}, env: {}, args: [] };
  const chk = (list, v) => (list || []).includes(v) ? 'checked' : '';
  return `<form class="card" id="mcp-form">
    <h3>${srv ? `Edit ${esc(s.name)}` : 'Add an MCP server'}</h3>
    <div class="row">
      <label style="margin:0">Name <input type="text" id="m-name" value="${esc(s.name || '')}" placeholder="github" style="width:180px" required></label>
      <label style="margin:0">Type <select id="m-transport"><option value="http" ${s.transport === 'http' ? 'selected' : ''}>Remote (HTTP)</option><option value="stdio" ${s.transport === 'stdio' ? 'selected' : ''}>Local program (stdio)</option></select></label>
      <label class="check" style="margin:0"><input type="checkbox" id="m-enabled" ${s.enabled ? 'checked' : ''}> Enabled</label>
    </div>
    <div id="m-http">
      <label>URL</label><input type="text" id="m-url" value="${esc(s.url || '')}" placeholder="https://mcp.example.com/mcp">
      <label>How agents reach it</label>
      <div class="radio-row">
        <label><input type="radio" name="m-route" value="hub" ${s.route !== 'direct' ? 'checked' : ''}> <span><strong>Through the hub</strong> (recommended): the URL, secrets and sign-ins stay on the hub; agents only get the hub's address</span></label>
        <label><input type="radio" name="m-route" value="direct" ${s.route === 'direct' ? 'checked' : ''}> <span>Directly from each machine (the URL and headers are copied to every agent)</span></label>
      </div>
      <label>Sign-in</label>
      <div class="radio-row">
        <label><input type="radio" name="m-auth" value="headers" ${s.auth !== 'oauth' ? 'checked' : ''}> <span>API key or token in a header (or none)</span></label>
        <label id="m-auth-oauth-label"><input type="radio" name="m-auth" value="oauth" ${s.auth === 'oauth' ? 'checked' : ''}> <span>OAuth sign-in, done once on the hub</span></label>
      </div>
      <div id="m-headers-part">
        <label>Headers <span class="muted">one per line, <code>Name: value</code> (e.g. <code>Authorization: Bearer …</code>)</span></label>
        <textarea id="m-headers" style="min-height:70px">${esc(kvToText(s.headers, ': '))}</textarea>
      </div>
      <div id="m-oauth-part">
        <p class="muted small">Leave these empty if the service registers clients automatically (most do). For services where you
          create an OAuth app yourself (e.g. GitHub, Slack), enter its client ID and secret, and register this callback address with it:
          <code>${esc((publicUrl || location.origin) + '/oauth/callback')}</code></p>
        <div class="row">
          <label style="margin:0">Client ID <input type="text" id="m-client-id" value="${esc(s.oauth_client_id || '')}" style="width:220px"></label>
          <label style="margin:0">Client secret <input type="password" id="m-client-secret" value="${esc(s.oauth_client_secret || '')}" style="width:220px" autocomplete="off"></label>
          <label style="margin:0">Scopes <input type="text" id="m-scopes" value="${esc(s.oauth_scopes || '')}" style="width:220px" placeholder="optional"></label>
          <label style="margin:0">Send the secret <select id="m-auth-method">
            <option value="basic" ${s.oauth_auth_method !== 'post' ? 'selected' : ''}>as HTTP Basic (standard)</option>
            <option value="post" ${s.oauth_auth_method === 'post' ? 'selected' : ''}>in the request body</option></select></label>
        </div>
      </div>
    </div>
    <div id="m-stdio">
      <label>Command</label><input type="text" id="m-command" value="${esc(s.command || '')}" placeholder="npx">
      <label>Arguments <span class="muted">one per line</span></label>
      <textarea id="m-args" style="min-height:70px">${esc((s.args || []).join('\n'))}</textarea>
      <label>Environment <span class="muted">one per line, <code>NAME=value</code></span></label>
      <textarea id="m-env" style="min-height:60px">${esc(kvToText(s.env, '='))}</textarea>
      <p class="muted small">The program must be installed on every machine that gets it. On Windows, npx/npm commands are wrapped in <code>cmd /c</code> for Claude Code automatically.</p>
    </div>
    <div class="row" style="margin-top:8px">
      <span class="muted small">Give it to:</span>
      <label class="check"><input type="checkbox" class="m-target" value="claude" ${chk(s.targets, 'claude')}> Claude Code (every account)</label>
      <label class="check"><input type="checkbox" class="m-target" value="gemini" ${chk(s.targets, 'gemini')}> Gemini CLI</label>
      <label class="check"><input type="checkbox" class="m-target" value="antigravity" ${chk(s.targets, 'antigravity')}> Antigravity</label>
    </div>
    <div class="row">
      <span class="muted small">On:</span>
      <label class="check"><input type="checkbox" class="m-platform" value="linux" ${chk(s.platforms, 'linux')}> Linux</label>
      <label class="check"><input type="checkbox" class="m-platform" value="macos" ${chk(s.platforms, 'macos')}> macOS</label>
      <label class="check"><input type="checkbox" class="m-platform" value="windows" ${chk(s.platforms, 'windows')}> Windows</label>
    </div>
    <label>Note <span class="muted">for you</span></label><input type="text" id="m-note" value="${esc(s.note || '')}">
    <p class="error" id="m-error" hidden></p>
    <div class="row" style="margin-top:10px"><button type="submit">${srv ? 'Save' : 'Add'}</button>
      ${srv ? '<button type="button" class="ghost" id="m-cancel">Cancel</button>' : ''}</div>
  </form>`;
}

// What the hub knows about a server's sign-in and last contact (shown in the list, refreshed while signing in).
function mcpStatus(s) {
  const parts = [];
  if (s.transport === 'http') parts.push(s.route === 'hub' ? '<span class="chip">through the hub</span>' : '<span class="chip">direct from each machine</span>');
  if (s.oauth) {
    const o = s.oauth;
    const chip = o.status === 'connected' ? `<span class="chip ok">${o.signed_in ? 'signed in' : 'no sign-in needed'}</span>`
      : o.status === 'signing_in' ? '<span class="chip">signing in…</span>' : '<span class="chip warn">needs sign-in</span>';
    parts.push(`${chip}${o.detail ? ` <span class="muted">${esc(o.detail)}</span>` : ''}`);
  }
  if (s.health) {
    const h = s.health;
    if (h.error) parts.push(`<div class="error">${esc(h.error)} <span class="muted">(${h.error_ago}s ago)</span></div>`);
    if (h.tools !== null && h.tools !== undefined) parts.push(`<div class="muted">${h.tools} tools (checked ${h.checked_ago}s ago)${h.note ? `; ${esc(h.note)}` : ''}</div>`);
  }
  return parts.join(' ');
}

function mcpRow(s) {
  const o = s.oauth;
  const actions = [
    o ? `<button class="small" data-signin="${s.id}">${o.signed_in ? 'Sign in again' : 'Sign in'}</button>` : '',
    o && o.signed_in ? `<button class="ghost small" data-signout="${s.id}">Sign out</button>` : '',
    s.transport === 'http' ? `<button class="ghost small" data-test="${s.id}">Test</button>` : '',
    `<button class="ghost small" data-edit="${s.id}">Edit</button>`,
    `<button class="danger small" data-del="${s.id}">Delete</button>`,
  ].join('');
  return `<tr data-row="${s.id}"><td><strong>${esc(s.name)}</strong>${s.enabled ? '' : ' <span class="chip warn">off</span>'}<div class="muted small">${esc(s.note || '')}</div></td>
    <td class="small">${s.transport === 'http' ? `<code>${esc(s.url)}</code>${Object.keys(s.headers).length ? `<div class="muted">headers: ${Object.keys(s.headers).map(esc).join(', ')}</div>` : ''}` : `<code>${esc([s.command, ...s.args].join(' '))}</code>`}
      <div data-status="${s.id}" style="margin-top:4px">${mcpStatus(s)}</div><div class="small" data-result="${s.id}" hidden></div></td>
    <td class="small">${s.targets.map(esc).join(', ')}<div class="muted">${s.platforms.map(esc).join(', ')}</div></td>
    <td><div class="row">${actions}</div></td></tr>`;
}

async function renderMcp(editId) {
  stopTimer();
  await loadState();
  const data = await api('/api/mcp');
  const editing = editId ? await api(`/api/mcp/${editId}`) : null;
  const behind = state.runners.filter((r) => r.online && (r.info || {}).context_synced !== state.context_token);
  let publicHint = '';
  try {
    if (data.hub.public_url && new URL(data.hub.public_url).origin !== location.origin) {
      publicHint = `<p class="small">OAuth sign-ins finish at <a href="${esc(data.hub.public_url)}">${esc(data.hub.public_url)}</a> (HUB_PUBLIC_URL), so open the hub there to press <strong>Sign in</strong>.</p>`;
    }
  } catch (e) { /* ignore a malformed value: the hub reports it below */ }
  view.innerHTML = `
    <h1>MCP servers</h1>
    <p class="muted">Set up an MCP server once here and every machine adds it to every agent that can use it (Claude Code, every account;
      Gemini CLI; Antigravity), in their own settings, so it works in your terminal sessions too. Servers <strong>through the hub</strong>
      keep their address, keys and OAuth sign-in on the hub: you sign in once, here, and every agent uses it.</p>
    ${data.hub.proxy ? '' : '<p class="error small">This hub can\'t route servers through itself (the mcp package is missing). Only “Directly from each machine” servers can be added.</p>'}
    ${data.hub.public_url_problem ? `<p class="error small">${esc(data.hub.public_url_problem)}</p>` : ''}
    ${publicHint}
    <div class="card">
      <div class="row spread"><h3 style="margin:0">bullpen: the hub's shared memory</h3>
        ${data.hub.enabled ? '<span class="chip ok">on</span>' : '<span class="chip warn">off: install the mcp package</span>'}</div>
      <p class="small">Tools <code>remember</code>, <code>recall</code>, <code>list_notes</code> at <code>/mcp</code> on this hub. Added to every agent automatically.
        Token <code id="tok">${esc(data.hub.token_hint)}</code> <button class="ghost small" id="reveal">Show</button> <button class="ghost small" id="rotate">Rotate</button></p>
      ${data.hub.trust_access ? '<p class="muted small">Requests through Cloudflare Access are accepted without the token (HUB_MCP_TRUST_ACCESS=1), for a claude.ai connector. They reach the shared memory only.</p>' : ''}
    </div>
    <h2>Central list</h2>
    <div class="card table-wrap" id="mcp-list"></div>
    <div class="row spread" style="margin-top:10px">
      <span class="muted small" id="sync-note">${behind.length ? `${behind.length} online machine(s) haven't picked up the latest changes yet: ${behind.slice(0, 5).map((r) => esc(r.name)).join(', ')}${behind.length > 5 ? ` and ${behind.length - 5} more` : ''}` : 'All online machines are up to date.'}</span>
      <button class="ghost small" id="sync-all">Sync all machines now</button>
    </div>
    ${mcpForm(editing, data.hub.public_url)}`;

  const fail = (e) => { if (!(e instanceof StaleNav)) alert(e.message); };
  const list = document.getElementById('mcp-list');
  const showResult = (id, html) => { const el = list.querySelector(`[data-result="${id}"]`); if (el) { el.innerHTML = html; el.hidden = !html; } };

  // The list redraws on its own, so a finished sign-in never wipes what is typed in the form below.
  const drawList = (servers) => {
    list.innerHTML = servers.length
      ? `<table class="stack"><thead><tr><th>Name</th><th>Connection</th><th>Agents</th><th></th></tr></thead><tbody>${servers.map(mcpRow).join('')}</tbody></table>`
      : '<p class="muted">Nothing yet. Add your first server below.</p>';
    drawListHandlers(list);
  };
  function drawListHandlers(root) {
    root.querySelectorAll('[data-edit]').forEach((b) => b.addEventListener('click', () => renderMcp(b.dataset.edit).catch(fail)));
    root.querySelectorAll('[data-test]').forEach((b) => b.addEventListener('click', () => test(b)));
    root.querySelectorAll('[data-signin]').forEach((b) => b.addEventListener('click', () => signIn(b)));
    root.querySelectorAll('[data-signout]').forEach((b) => b.addEventListener('click', async () => {
      if (!confirm('Sign the hub out of this server? Agents lose access to it until you sign in again.')) return;
      try { await api(`/api/mcp/${b.dataset.signout}/signout`, { method: 'POST', body: {} }); await refreshList(); } catch (e) { fail(e); }
    }));
    root.querySelectorAll('[data-del]').forEach((b) => b.addEventListener('click', async () => {
      if (!confirm('Delete this server? Machines remove it from their agents on the next sync.')) return;
      try { await api(`/api/mcp/${b.dataset.del}`, { method: 'DELETE' }); renderMcp(); } catch (e) { fail(e); }
    }));
  }
  // Redraw only what changed: a row whose buttons changed is replaced, otherwise only its status
  // line is, so focus, text selection and clicks in progress elsewhere in the list survive.
  const refreshList = async () => {
    const fresh = await api('/api/mcp');
    const rows = list.querySelectorAll('tr[data-row]');
    const sameSet = rows.length === fresh.servers.length && fresh.servers.every((s, i) => rows[i].dataset.row === String(s.id));
    if (!sameSet) {
      const results = {};
      list.querySelectorAll('[data-result]').forEach((el) => { if (!el.hidden) results[el.dataset.result] = el.innerHTML; });
      drawList(fresh.servers);
      Object.entries(results).forEach(([id, html]) => showResult(id, html));
      return fresh;
    }
    fresh.servers.forEach((s) => {
      const row = list.querySelector(`tr[data-row="${s.id}"]`);
      const tmp = document.createElement('tbody');
      tmp.innerHTML = mcpRow(s);
      const next = tmp.firstElementChild;
      const buttons = (r) => [...r.querySelectorAll('button')].map((b) => b.outerHTML).join('');
      if (buttons(row) !== buttons(next)) {
        const result = row.querySelector('[data-result]');
        const keep = result && !result.hidden ? result.innerHTML : '';
        row.replaceWith(next);
        drawListHandlers(next);
        if (keep) showResult(s.id, keep);
      } else {
        const status = row.querySelector('[data-status]');
        const html = mcpStatus(s);
        if (status.innerHTML !== html) status.innerHTML = html;
      }
    });
    return fresh;
  };
  drawList(data.servers);

  // While a sign-in is open in another tab, keep the list current; stop once none is pending.
  // (Whether the tab is still open can't be told reliably: many providers' login pages cut the link
  // to the page that opened them. So the hint below stays until the sign-in ends either way.)
  const watchStatus = () => every(2000, async () => {
    const fresh = await refreshList();
    fresh.servers.forEach((s) => {
      if (s.oauth && s.oauth.status !== 'signing_in') {
        const el = list.querySelector(`[data-result="${s.id}"]`);
        if (el && el.dataset.hint) { delete el.dataset.hint; showResult(s.id, ''); }
      }
    });
    if (!fresh.servers.some((s) => s.oauth && s.oauth.status === 'signing_in')) stopTimer();
  });

  async function test(b) {
    const id = b.dataset.test;
    b.disabled = true; showResult(id, '<span class="muted">Asking it for its tools…</span>');
    let html;
    try {
      const r = await api(`/api/mcp/${id}/test`, { method: 'POST', body: {} });
      html = r.ok
        ? `<span class="chip ok">works from the hub</span> ${r.tools.length} tools: ${r.tools.map((t) => `<code title="${esc(t.description)}">${esc(t.name)}</code>`).join(' ')}`
        : `<span class="error">${esc(r.error)}</span>`;
    } catch (e) { html = `<span class="error">${esc(e.message)}</span>`; }
    await refreshList().catch(() => {});
    showResult(id, html);
  }

  async function signIn(b) {
    const id = b.dataset.signin;
    // Open the tab now, while this is still a click: browsers block windows opened after an await.
    const win = window.open('', '_blank');
    if (win) { win.opener = null; win.document.write('<p style="font:16px system-ui,sans-serif">Contacting the provider…</p>'); }
    b.disabled = true;
    try {
      const r = await api(`/api/mcp/${id}/signin`, { method: 'POST', body: { origin: location.origin } });
      if (r.authorization_url) {
        if (win) win.location.href = r.authorization_url;
        await refreshList();
        showResult(id, win
          ? '<span class="muted">Finish signing in in the new tab. If you closed it, press <strong>Sign in</strong> again to continue.</span>'
          : `Your browser blocked the sign-in tab. <a href="${esc(r.authorization_url)}" target="_blank" rel="noopener">Open the sign-in page</a>`);
        const el = list.querySelector(`[data-result="${id}"]`);
        if (el) el.dataset.hint = '1';
        watchStatus();
      } else {
        if (win) win.close();
        await refreshList();
        if (r.detail) showResult(id, `<span class="muted">${esc(r.detail)}</span>`);
      }
    } catch (e) {
      if (win) win.close();
      b.disabled = false;
      showResult(id, `<span class="error">${esc(e.message)}</span>`);
    }
  }

  const tSel = document.getElementById('m-transport');
  const radio = (n) => (view.querySelector(`input[name="${n}"]:checked`) || {}).value;
  const showParts = () => {
    document.getElementById('m-http').hidden = tSel.value !== 'http';
    document.getElementById('m-stdio').hidden = tSel.value !== 'stdio';
    const viaHub = radio('m-route') === 'hub';
    if (!viaHub && radio('m-auth') === 'oauth') view.querySelector('input[name="m-auth"][value="headers"]').checked = true;
    document.getElementById('m-auth-oauth-label').hidden = !viaHub;
    document.getElementById('m-headers-part').hidden = radio('m-auth') === 'oauth';
    document.getElementById('m-oauth-part').hidden = radio('m-auth') !== 'oauth';
  };
  tSel.addEventListener('change', showParts);
  view.querySelectorAll('input[name="m-route"], input[name="m-auth"]').forEach((r) => r.addEventListener('change', showParts));
  showParts();
  if (editing) document.getElementById('mcp-form').scrollIntoView({ behavior: 'smooth', block: 'start' });
  document.getElementById('m-cancel')?.addEventListener('click', () => renderMcp());
  document.getElementById('reveal').addEventListener('click', async () => {
    try { document.getElementById('tok').textContent = (await api('/api/mcp/token')).token; } catch (e) { fail(e); }
  });
  document.getElementById('rotate').addEventListener('click', async () => {
    if (!confirm('Make a new token? Machines pick it up on their next sync; anything else using the old one stops working.')) return;
    try { await api('/api/mcp/token/rotate', { method: 'POST', body: {} }); renderMcp(); } catch (e) { fail(e); }
  });
  document.getElementById('sync-all').addEventListener('click', async (ev) => {
    ev.target.disabled = true;
    try {
      const r = await api('/api/runners-sync-all', { method: 'POST', body: {} });
      const skipped = (r.skipped || []).length;
      document.getElementById('sync-note').textContent = `Asked ${r.queued.length} machine(s) to sync now.${skipped ? ` ${skipped} skipped (already syncing, offline or too old).` : ''}`;
      ev.target.disabled = false;
    }
    catch (e) { ev.target.disabled = false; fail(e); }
  });
  if (data.servers.some((s) => s.oauth && s.oauth.status === 'signing_in')) watchStatus();
  document.getElementById('mcp-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const err = document.getElementById('m-error'); err.hidden = true;
    const body = {
      name: document.getElementById('m-name').value.trim(), transport: tSel.value, enabled: document.getElementById('m-enabled').checked,
      url: document.getElementById('m-url').value.trim(), headers: textToKv(document.getElementById('m-headers').value, ':'),
      route: tSel.value === 'http' ? radio('m-route') : 'direct', auth: tSel.value === 'http' ? radio('m-auth') : 'headers',
      oauth_client_id: document.getElementById('m-client-id').value.trim(), oauth_client_secret: document.getElementById('m-client-secret').value,
      oauth_scopes: document.getElementById('m-scopes').value.trim(), oauth_auth_method: document.getElementById('m-auth-method').value,
      command: document.getElementById('m-command').value.trim(), args: document.getElementById('m-args').value.split('\n').map((x) => x.trim()).filter(Boolean),
      env: textToKv(document.getElementById('m-env').value, '='), note: document.getElementById('m-note').value,
      targets: [...document.querySelectorAll('.m-target:checked')].map((c) => c.value), platforms: [...document.querySelectorAll('.m-platform:checked')].map((c) => c.value),
    };
    try {
      await api(editing ? `/api/mcp/${editing.id}` : '/api/mcp', { method: editing ? 'PUT' : 'POST', body });
      renderMcp();
    } catch (e) { err.textContent = e.message; err.hidden = false; }
  });
}

// ------------------------------------------------------------------- usage
// Claude plan limits per account, tokens per agent, Gemini, API spend and links. Each part loads
// on its own, so a slow cost API never holds up the limits. Display only: nothing here (or in
// the hub) moves work because of these figures.
const usagePage = { fastUntil: 0, ticks: 0 };
const SOURCE_WORDS = { job: 'from a job', check: 'from Check limits' };

// The newest figure on an account's card: where it came from and when.
function newestReading(acc) {
  let best = null;
  for (const w of acc.windows || []) {
    const at = Number(w.used_at ?? w.observed_at) || 0;
    if (w.runner && (!best || at > best.at)) best = { at, runner: w.runner, agent: w.agent, source: w.source };
  }
  const ov = acc.overage;
  if (!best && ov && ov.runner) best = { at: Number(ov.observed_at) || 0, runner: ov.runner, agent: ov.agent, source: null };
  return best;
}

// One window: label, how much is used, a bar, when it resets. A figure that came from another
// machine or agent than the card's newest reading, or is much older, says so.
function limitWindowHtml(w, ref, now) {
  const label = esc(w.label || windowShort(w.id));
  if (isExpired(w, now)) {
    return `<div class="win"><div class="w-head"><span class="w-label">${label}</span></div>
      <div class="muted small">Reset at ${esc(whenLocal(w.resets_at))}; no reading since.</div></div>`;
  }
  const level = windowLevel(w);
  const has = w.used !== null && w.used !== undefined;
  const pct = has ? percent(w.used) : 0;
  const usedText = !has ? '<span class="muted">no figure</span>' : Number(w.used) > 1 ? `${pct}% used, over the limit` : `${pct}% used`;
  const width = Math.min(Math.max(pct, 0), 100);
  const meter = has ? `<div class="meter ${level}" role="progressbar" aria-label="${label}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${width}"><span style="width:${width}%"></span></div>` : '';
  const resets = w.resets_at !== null && w.resets_at !== undefined ? `resets ${esc(whenLocal(w.resets_at))}`
    : w.resets_text ? `resets ${esc(w.resets_text)}` : '';
  const at = Number(w.used_at ?? w.observed_at) || 0;
  const other = ref && w.runner && (w.runner !== ref.runner || w.agent !== ref.agent || ref.at - at > 120);
  const from = other ? `<span title="${esc(fullDate(at))}">as of ${ago(at)}, ${esc(w.runner)} · ${esc(w.agent)}</span>` : '';
  const close = level === 'warn' ? '<div class="w-note warn small">Getting close to this limit.</div>' : '';
  return `<div class="win"><div class="w-head"><span class="w-label">${label}</span><span class="w-used ${level}">${usedText}</span></div>
    ${meter}${resets || from ? `<div class="muted small">${[resets, from].filter(Boolean).join(' · ')}</div>` : ''}${close}</div>`;
}

function accountCardHtml(acc, now) {
  const first = (acc.agents || [])[0] || {};
  const head = acc.labelled ? `<h3>${esc(acc.account)}</h3>`
    : `<h3>${esc(first.runner)} / ${esc(first.agent)} <span class="muted small">no account label</span></h3>`;
  const ref = newestReading(acc);
  const sub = ref ? `<div class="muted small">${esc(ref.runner)} · ${esc(ref.agent)} · <span title="${esc(fullDate(ref.at))}">as of ${ago(ref.at)}</span>${own(SOURCE_WORDS, ref.source) ? ` · ${SOURCE_WORDS[ref.source]}` : ''}</div>` : '';
  const machines = acc.machines || [];
  const shared = acc.labelled && machines.length > 1
    ? `<p class="w-note small">This label is used on ${machines.length} machines: ${machines.map(esc).join(', ')}. Use one label only for the very same account.</p>` : '';
  const notes = [];
  const until = limitReachedUntil(acc.status, acc.windows, now);
  if (until !== undefined) {
    notes.push(['bad', until ? `Limit reached: jobs on this account fail until ${esc(whenLocal(until))}.` : 'Limit reached: jobs on this account fail until the limit resets.']);
  }
  const ov = acc.overage;
  if (ov && ov.using) notes.push(['warn', 'Using extra usage (billed separately).']);
  else if (until !== undefined && ov && (ov.status === 'rejected' || ov.disabled_reason)) {
    notes.push(['', `Extra usage isn't available${ov.disabled_reason ? ` (${esc(overageReason(ov.disabled_reason))})` : ''}.`]);
  }
  const windows = (acc.windows || []).map((w) => limitWindowHtml(w, ref, now)).join('');
  return `<div class="card account">${head}${sub}${shared}
    ${notes.map(([cls, text]) => `<p class="w-note small ${cls}">${text}</p>`).join('')}
    <div class="wins">${windows || '<p class="muted small">No figures for its windows yet.</p>'}</div></div>`;
}

function limitsHtml(data) {
  const now = Number(data.now) || Date.now() / 1000;
  const accounts = data.accounts || [];
  const waiting = data.waiting || [];
  if (!accounts.length && !waiting.length) {
    return `<div class="card muted">No Claude agent reports plan limits yet. They show here once a Claude job has run on a machine
      with an up-to-date runner, or after Check limits.</div>`;
  }
  return `${accounts.length ? `<div class="grid accounts">${accounts.map((a) => accountCardHtml(a, now)).join('')}</div>` : ''}
    ${waiting.length ? `<div class="card waiting"><ul class="small">${waiting.map((w) => `<li>No reading yet for ${esc(w.agent)} on ${esc(w.runner)}: ${w.can_check === false
      ? 'run a job. Check limits can\'t ask it: that needs a full claude.ai login.'
      : 'run a job or press Check limits now.'}</li>`).join('')}</ul></div>` : ''}`;
}

function agentUseHtml(data) {
  const rows = data.rows || [];
  if (!rows.length) return '<div class="card muted">No finished task with token figures in the last 30 days.</div>';
  const label = (text) => `<span class="phone-only muted">${text} </span>`;
  return `<div class="card table-wrap"><table class="stack use"><thead><tr><th>Machine</th><th>Agent</th><th>Account</th>
    <th class="num">Jobs</th><th class="num">Tokens in</th><th class="num">Tokens out</th><th class="num">API-equivalent</th></tr></thead><tbody>
    ${rows.map((r) => `<tr><td class="u-who"><a href="#/machine/${encodeURIComponent(r.runner)}">${esc(r.runner)}</a></td>
      <td class="u-who">${esc(r.agent)}</td>
      <td class="u-who">${r.account ? esc(r.account) : '<span class="muted">(no label)</span>'}</td>
      <td class="u-break"></td>
      <td class="num">${label('Jobs')}${Number(r.jobs) || 0}</td>
      <td class="num" title="${esc(tokensInTitle(r))}">${label('In')}${count(tokensIn(r))}</td>
      <td class="num">${label('Out')}${count(r.output_tokens)}</td>
      <td class="num"${r.cost_usd !== null && r.cost_usd !== undefined ? ` title="${esc(COST_TITLE)}">${estimate(r.cost_usd)}` : '><span class="muted">—</span>'}</td></tr>`).join('')}
    </tbody></table>
    <p class="muted small">API-equivalent is Claude Code's own estimate at list prices; Gemini CLI reports tokens only.</p></div>`;
}

function geminiHtml(data) {
  const hits = data ? Number(data.gemini_quota_hits) : NaN;
  return `<div class="card"><p class="flush">Gemini API keys have no readout of the quota left. Limits apply per Google Cloud project;
    see the <a href="${GEMINI_LIMITS_URL}" target="_blank" rel="noopener noreferrer">rate-limit page in AI Studio</a>.
    Jobs that hit the limit show <span class="chip bad" style="margin-right:0">Gemini quota hit</span>.</p>
    ${Number.isFinite(hits) ? `<p class="small flush-bottom">${plural(hits, 'job')} hit the Gemini quota in the last 30 days.</p>` : ''}</div>`;
}

function spendHtml(u) {
  const orgs = (u.orgs || []).map((o) => {
    if (!o.ok) return `<div class="card"><h3>${esc(o.name)}</h3><p class="error">${esc(o.error)}</p></div>`;
    const max = Math.max(0.0001, ...o.days.slice(-31).map((d) => d.cost));
    return `<div class="card"><h3>${esc(o.name)}</h3>
      <div style="font-size:1.6rem;font-weight:700">$${Number(o.total).toFixed(2)}</div>
      <div class="muted small">API spend, last 30 days</div>
      <div class="bars">${o.days.slice(-31).map((d) => `<span title="${esc(d.day)}: $${Number(d.cost).toFixed(2)}" style="height:${Math.round((d.cost / max) * 100)}%"></span>`).join('')}</div></div>`;
  }).join('');
  return `${orgs ? `<div class="grid">${orgs}</div>` : '<div class="card muted">To see API spend here, add an Admin API key for each Anthropic Console organization (<code>HUB_ANTHROPIC_ADMIN_KEYS</code> in hub.env; see the setup guide, “Usage panel”).</div>'}
    ${u.litellm_url ? `<p>Per-key spend for everything that goes through your gateway: <a href="${esc(u.litellm_url)}" target="_blank" rel="noopener">LiteLLM dashboard</a></p>` : ''}
    ${orgs ? `<p class="muted small">Checked <span title="${esc(fullDate(u.checked_at))}">${ago(u.checked_at)}</span> (refreshes every 15 minutes, or when you press Refresh).</p>` : ''}`;
}

function linksHtml(u) {
  return `<div class="card">
      <p class="flush">The hub shows Claude plan limits above, read from Claude Code itself. For everything else, check each service's own page. If you have more than one account with a service, a separate browser profile for each account opens each link signed in to the right one.</p>
      <ul>${(u.links || []).map((l) => `<li><a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(l.name)}</a></li>`).join('')}</ul>
      <p class="muted small flush-bottom">Add your own links with <code>HUB_LINKS</code> in hub.env.</p>
    </div>`;
}

// Platform credits per organization: an estimate from the amount you enter and the spend this cycle.
const money = (v) => `$${Number(v || 0).toFixed(2)}`;
function dayLocal(ts) { return new Date(ts * 1000).toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' }); }
function creditOrgHtml(o, machines) {
  const used = o.used === null || o.used === undefined ? null : Number(o.used);
  const pct = used === null ? 0 : Math.floor(used * 100 + 1e-6);
  const level = o.state === 'exhausted' ? 'bad' : o.state === 'reserve' ? 'warn' : '';
  const cycle = o.reset_day ? 'this billing cycle' : 'this calendar month';
  const lines = [];
  if (o.spend_ok) {
    lines.push(`Spent ${cycle}: <strong>${money(o.spend)}</strong> · about ${money(o.burn)} a day over the last ${plural(Number(o.burn_days) || 1, 'day')}`);
  } else lines.push('<span class="muted">No spend figures yet (see API spend below).</span>');
  if (o.left !== null && o.left !== undefined) {
    lines.push(`Left: <strong>${money(o.left)}</strong> of ${money(o.amount)}`);
    if (o.next_reset) lines.push(`Next reset: ${esc(dayLocal(o.next_reset))}`);
    if (o.lasts === false && !o.runs_out) lines.push('<span class="bad">Used up by the estimate.</span>');
    else if (o.runs_out) lines.push(`Runs out about <strong>${esc(dayLocal(o.runs_out))}</strong> at this rate`);
    else lines.push(o.reset_day ? 'Lasts until the reset at this rate' : 'Lasts until the end of the month at this rate');
  }
  if (o.refused) lines.push(`<span class="bad">The API refused a job for lack of credit</span> <span class="muted small" title="${esc(fullDate(o.refused_at))}">${ago(o.refused_at)}</span>; counted as used up until ${esc(whenLocal(o.refused_until))}.`);
  const meter = used !== null ? `<div class="meter ${level}" role="progressbar" aria-label="${esc(o.name)} platform credits used" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.min(pct, 100)}"><span style="width:${Math.min(pct, 100)}%"></span></div>
    <div class="muted small">${pct}% used (estimate)</div>` : '';
  const agents = machines.filter((m) => m.org === o.name);
  const id = `c-${encodeURIComponent(o.name).replace(/[^A-Za-z0-9]/g, '_')}`;
  return `<div class="card credit-org"><div class="row spread"><h3>${esc(o.name)}</h3>${creditStateChip(o.state)}</div>
    ${meter}<ul class="small credit-lines">${lines.map((l) => `<li>${l}</li>`).join('')}</ul>
    ${agents.length ? `<p class="small">Credits agents: ${agents.map((m) => `${esc(m.agent)} on <a href="#/machine/${encodeURIComponent(m.runner)}">${esc(m.runner)}</a>`).join(', ')}</p>` : '<p class="muted small">No machine names a credits agent for this organization yet (guide 5).</p>'}
    <form class="credit-form" data-org="${esc(o.name)}">
      <div class="row"><label class="credit-field">Monthly credits (USD)<input type="number" min="1" max="1000000" step="1" name="amount" id="${id}-amount" value="${o.amount ? esc(o.amount) : ''}" placeholder="e.g. 200"></label>
      <label class="credit-field">Reset day of the month<input type="number" min="1" max="31" step="1" name="reset_day" id="${id}-day" value="${o.reset_day ? esc(o.reset_day) : ''}" placeholder="1-31"></label>
      <button type="submit" class="small">Save</button></div>
    </form></div>`;
}

function creditsHtml(c) {
  const orgs = c.orgs || [];
  if (!orgs.length) {
    return `<div class="card muted">Some Claude plans include monthly API credits in a Console organization. To track them here,
      add an Admin API key for that organization (<code>HUB_ANTHROPIC_ADMIN_KEYS</code> in hub.env; guide 8).</div>`;
  }
  const machines = c.machines || [];
  const broken = machines.filter((m) => !m.org);
  return `<div class="card"><label class="check"><input type="checkbox" id="credits-first" ${c.first ? 'checked' : ''}>
      Use platform credits first for background work</label>
    <p class="muted small flush-bottom">Council steps and subtasks named for a Claude subscription agent run on the same machine's credits agent
      while its organization is <em>available</em> (under ${Math.round((Number(c.reserve_at) || 0.9) * 100)}% used). A task you start that stops at a
      usage limit is retried once on credits. Nothing ever moves from one subscription account to another (guide 1).</p></div>
    <div class="grid credits">${orgs.map((o) => creditOrgHtml(o, machines)).join('')}</div>
    ${broken.length ? `<div class="card waiting"><ul class="small">${broken.map((m) => `<li>${esc(m.agent)} on ${esc(m.runner)} isn't used: ${esc(m.problem)}.</li>`).join('')}</ul></div>` : ''}
    <p class="muted small">Estimates: the amount is what you enter, and the spend is everything in the organization this cycle from Anthropic's cost report,
      which doesn't say what credits paid for. Purchased credits are spent after the plan's credits.</p>`;
}

function wireCredits(box) {
  box.addEventListener('change', async (ev) => {
    if (ev.target.id !== 'credits-first') return;
    const cb = ev.target; cb.disabled = true;
    try { await api('/api/credits', { method: 'POST', body: { first: cb.checked } }); } catch (e) { cb.checked = !cb.checked; fleetMsg(esc(e.message), true, 'u-msg'); } finally { cb.disabled = false; }
  });
  box.addEventListener('submit', async (ev) => {
    const form = ev.target.closest('form.credit-form');
    if (!form) return;
    ev.preventDefault();
    const num = (name) => { const v = form.elements[name].value.trim(); return v === '' ? null : Number(v); };
    const body = { orgs: { [form.dataset.org]: { amount: num('amount'), reset_day: num('reset_day') } } };
    const b = form.querySelector('button'); b.disabled = true;
    try {
      await api('/api/credits', { method: 'POST', body });
      await fillPart('u-credits', '/api/credits', creditsHtml);
    } catch (e) { fleetMsg(esc(e.message), true, 'u-msg'); b.disabled = false; }
  });
}

// Fill one part of the page from one request. A part that fails says so and the rest still loads.
async function fillPart(id, path, draw, opts) {
  try {
    const data = await api(path, opts);
    const el = document.getElementById(id);
    if (el) el.innerHTML = draw(data);
    return data;
  } catch (e) {
    if (e instanceof StaleNav) return null;
    const el = document.getElementById(id);
    if (el) el.innerHTML = `<p class="error">${esc(e.message)}</p>`;
    return null;
  }
}

// API spend's part of the page; also fills in the links and shows Refresh when there is spend
// to refresh.
function spendPartHtml(u) {
  document.getElementById('u-links').innerHTML = linksHtml(u);
  document.getElementById('u-refresh').hidden = !(u.orgs || []).length;
  return spendHtml(u);
}

async function renderUsage() {
  stopTimer();
  await loadState();
  const canCheck = state.runners.some((r) => (r.agents || []).some((a) => hasFeature(a, 'usage_check')));
  view.innerHTML = `
    <h1>Usage</h1>
    <div class="row spread"><h2>Claude plan limits</h2>
      ${canCheck ? '<button type="button" class="small" id="u-check" title="Run Claude Code\'s /usage once per account, on one online machine signed in to it">Check limits now</button>' : ''}</div>
    <div id="u-msg" class="notice" hidden></div>
    <div id="u-limits"><p class="muted">Loading…</p></div>
    <p class="muted small">Read from Claude Code's own output when a job runs, or from its /usage command when you press Check limits now.
      Only the windows your plan has appear: some plans (Ultra, and some Team or Enterprise seats) have no 5-hour window.</p>
    <p class="muted small">These figures help you plan. The hub never picks or switches accounts because of them, and usage limits
      aren't a reason to switch accounts: wait for the reset (guide 1).</p>

    <h2>Agent use, last 30 days</h2>
    <div id="u-jobs"><p class="muted">Loading…</p></div>

    <h2>Gemini</h2>
    <div id="u-gemini">${geminiHtml(null)}</div>

    <h2>Platform credits</h2>
    <div id="u-credits"><p class="muted">Loading…</p></div>

    <div class="row spread"><h2>API spend</h2><button type="button" class="ghost small" id="u-refresh" hidden>Refresh</button></div>
    <p class="error" id="u-refresh-error" hidden></p>
    <div id="u-spend"><p class="muted">Loading…</p></div>

    <h2>Subscriptions and accounts</h2>
    <div id="u-links"></div>`;

  document.getElementById('u-msg').addEventListener('click', (ev) => { if (ev.target.closest('[data-close-msg]')) ev.currentTarget.hidden = true; });
  document.getElementById('u-check')?.addEventListener('click', async (ev) => {
    const b = ev.currentTarget; b.disabled = true;
    try {
      const r = await api('/api/usage/check', { method: 'POST', body: {} });
      const jobs = (r.queued || []).map((id) => `<a href="#/job/${Number(id)}">#${Number(id)}</a>`).join(', ');
      fleetMsg((r.queued || []).length
        ? `Checking now on ${(r.machines || []).map(esc).join(', ')} (${jobs}). The figures below update when it is done.${skippedText(r)}`
        : `Nothing was checked.${skippedText(r)}`, false, 'u-msg');
      usagePage.fastUntil = Date.now() + 2 * 60 * 1000;
    } catch (e) { fleetMsg(esc(e.message), true, 'u-msg'); } finally { b.disabled = false; }
  });
  document.getElementById('u-refresh').addEventListener('click', async (ev) => {
    const b = ev.currentTarget; b.disabled = true;
    const err = document.getElementById('u-refresh-error'); err.hidden = true;
    try {
      const u = await api('/api/usage/refresh', { method: 'POST', body: {} });
      const spend = document.getElementById('u-spend');
      if (spend) spend.innerHTML = spendPartHtml(u);
      await fillPart('u-credits', '/api/credits', creditsHtml);
    } catch (e) { err.textContent = e.message; err.hidden = false; } finally { b.disabled = false; }
  });

  usagePage.fastUntil = 0; usagePage.ticks = 0;
  wireCredits(document.getElementById('u-credits'));
  await Promise.all([
    fillPart('u-credits', '/api/credits', creditsHtml),
    fillPart('u-limits', '/api/usage/limits', limitsHtml),
    fillPart('u-jobs', '/api/usage/jobs?days=30', (data) => {
      const g = document.getElementById('u-gemini');
      if (g) g.innerHTML = geminiHtml(data);
      return agentUseHtml(data);
    }),
    fillPart('u-spend', '/api/usage', spendPartHtml),
  ]);
  // Limits change as jobs run: every 30 seconds, and every 5 for two minutes after Check limits now.
  every(5000, async () => {
    if (!location.hash.startsWith('#/usage')) { stopTimer(); return; }
    usagePage.ticks += 1;
    if (Date.now() < usagePage.fastUntil || usagePage.ticks % 6 === 0) await fillPart('u-limits', '/api/usage/limits', limitsHtml);
  });
}

// ------------------------------------------------------------------ router
async function route() {
  navGen += 1;
  const hash = location.hash || '#/';
  document.querySelectorAll('#nav a').forEach((a) => {
    const r = a.dataset.route;
    a.classList.toggle('active', (r === 'overview' && (hash === '#/' || hash === '')) || hash.startsWith(`#/${r}`));
  });
  try {
    let m;
    if ((m = hash.match(/^#\/job\/(\d+)/))) await renderJob(Number(m[1]));
    else if ((m = hash.match(/^#\/machine\/(.+)$/))) await renderMachine(decodeURIComponent(m[1]));
    else if (hash.startsWith('#/task')) await renderTask();
    else if (hash.startsWith('#/council')) await renderCouncil();
    else if (hash.startsWith('#/history')) await renderHistory(hash.split('?')[1] || '');
    else if (hash.startsWith('#/usage')) await renderUsage();
    else if (hash.startsWith('#/memory')) await renderMemory();
    else if (hash.startsWith('#/mcp')) await renderMcp();
    else await renderOverview();
  } catch (e) {
    if (e instanceof StaleNav) return;  // you went to another page meanwhile; that page draws itself
    if (e.message !== 'Not logged in') view.innerHTML = `<h1>Something went wrong</h1><p class="error">${esc(e.message)}</p>`;
  }
}

window.addEventListener('hashchange', route);
route();
