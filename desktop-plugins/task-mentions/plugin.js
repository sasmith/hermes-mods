/**
 * Task Mentions — kanban task ids (`t_1a2b3c4d`) anywhere in a chat become live references.
 *
 *   hover  → small preview card: title, status, assignee, priority, age, board
 *   click  → task popup: body, latest summary/result, parents/children (clickable),
 *            runs, comments, recent events; buttons to open the card (the board's
 *            drawer: edit + comment), the worker session, the board, copy the id, refresh. Esc / click outside closes.
 *
 * The chat DOM is React-owned, so this plugin never edits it. Ids are styled with the
 * CSS Custom Highlight API (`::highlight(task-mention)`, Ranges over existing text
 * nodes) and hit-tested with `caretPositionFromPoint`. Popups live in a body-level host.
 *
 * Data: the kanban plugin's REST router (`/api/plugins/kanban/tasks/<id>`). A ref written as
 * `@task:<board>@<gateway>/<id>` (session-mentions typeahead) is read on that gateway; any other
 * id is tried on the active gateway first, then on every other connected gateway. Within a
 * gateway an id missing from the named/current board is looked up on every other board.
 */

import { host as sdkHost } from '@hermes/plugin-sdk'

const PLUGIN_ID = 'task-mentions'
const HL_NAME = 'task-mention'
const ID_RE = /\bt_[0-9a-f]{8}\b/g
const SCOPE = '[data-slot="aui_thread-viewport"]'
const CACHE_MS = 15_000
const HOVER_DELAY_MS = 220
const LIVE_REFRESH_MS = 8_000
const LIVE_STATUSES = new Set(['running', 'ready', 'todo', 'triage', 'scheduled', 'review'])

// ── pure helpers (exported for tests) ────────────────────────────────────────

/** All task ids in `text` with offsets: [{id, start, end}]. */
export function findTaskIds(text) {
  const out = []
  if (!text || text.indexOf('t_') === -1) return out
  ID_RE.lastIndex = 0
  let m
  while ((m = ID_RE.exec(text))) out.push({ id: m[0], start: m.index, end: m.index + m[0].length })
  return out
}

/** Which session the "open session" button should land on. The run's own worker session
 *  (current run first, else the newest run that has one); falls back to the CREATING chat
 *  (`task.session_id`) with a matching label; null when there is nothing useful to open
 *  (no id, or it is the chat already in focus). */
export function sessionTarget(d, focused) {
  const runs = Array.isArray(d?.runs) ? d.runs : []
  const cur = d?.task?.current_run_id
  const run = runs.find(r => r.id === cur && r.worker_session_id) || [...runs].reverse().find(r => r.worker_session_id)
  const worker = run?.worker_session_id
  const target = worker ? { id: worker, label: 'Open worker session' } : d?.task?.session_id ? { id: d.task.session_id, label: 'Open creating chat' } : null
  return target && target.id !== focused ? target : null
}

export function ago(seconds, now = Date.now()) {
  if (!seconds) return ''
  const s = Math.max(0, Math.round(now / 1000 - Number(seconds)))
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

export function duration(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`
}

const STATUS_COLORS = {
  running: '#4a84fe',
  ready: '#a371f7',
  todo: '#8b949e',
  triage: '#d29922',
  scheduled: '#e3b341', // waiting on a timer
  blocked: '#f85149',
  review: '#db61a2',
  done: '#3fb950',
  archived: '#6e7681'
}
export const statusColor = status => STATUS_COLORS[String(status || '').toLowerCase()] || '#8b949e'

const truncate = (s, n) => {
  const t = String(s || '')
  return t.length > n ? `${t.slice(0, n)}…` : t
}

// ── data ─────────────────────────────────────────────────────────────────────

const enc = encodeURIComponent
const cache = new Map() // id -> {at, data, board, conn, error}
const inflight = new Map()
const boardOf = new Map() // id -> board slug where it was found
const connOf = new Map() // id -> connection id where it was found
const hintOf = new Map() // id -> {gateway, board} from an `@task:<board>@<gateway>/` prefix
let hostRef = null

function activeConnection() {
  try {
    return hostRef?.activeConnectionId?.() || hostRef?.state?.connectionId?.get?.() || 'local'
  } catch {
    return 'local'
  }
}

export const slug = text =>
  String(text || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')

let connCache = { at: 0, rows: [] }
async function connections() {
  if (Date.now() - connCache.at < 60_000 && connCache.rows.length) return connCache.rows
  let rows = []
  try {
    rows = typeof hostRef?.connections === 'function' ? await hostRef.connections() : []
  } catch {
    /* optional */
  }
  const list = (Array.isArray(rows) ? rows : []).filter(r => r && r.id).map(r => ({ id: String(r.id), label: String(r.label || r.name || r.id) }))
  connCache = { at: Date.now(), rows: list.length ? list : [{ id: activeConnection(), label: 'This device' }] }
  return connCache.rows
}

/** Connection id for a gateway name as written in a ref (slugged label or id). */
export function findConnection(conns, name) {
  const n = String(name || '').trim().toLowerCase()
  if (!n) return null
  return conns.find(c => c.id.toLowerCase() === n || slug(c.label) === slug(n))?.id || null
}

/** `@task:<board>[@<gateway>]/` immediately before an id: {board, gateway} or null. */
export function refQualifier(before) {
  const m = /@task:[`"']?([\w.-]*)(?:@([\w.-]+))?\/$/.exec(String(before || '').slice(-120))
  return m ? { board: m[1] || '', gateway: m[2] || '' } : null
}

function api(path, connectionId = activeConnection()) {
  const bridge = typeof window !== 'undefined' ? window.hermesDesktop?.api : null
  if (!bridge) return Promise.reject(new Error('Hermes desktop bridge unavailable'))
  return bridge({ path, connectionId, timeoutMs: 15_000 })
}

const is404 = e => /\b404\b|not found/i.test(String(e?.message || e))

/** {data, board} from one gateway, or null when no board there has the id. */
async function loadTaskOn(id, conn, preferBoard) {
  const tryBoard = board => api(`/api/plugins/kanban/tasks/${enc(id)}${board ? `?board=${enc(board)}` : ''}`, conn)
  const tried = new Set()
  for (const b of [preferBoard, undefined]) {
    if (tried.has(b || '')) continue
    tried.add(b || '')
    try {
      return { data: await tryBoard(b), board: b || '' }
    } catch (e) {
      if (!is404(e)) throw e
    }
  }
  let boards = []
  try {
    const res = await api('/api/plugins/kanban/boards', conn)
    boards = (Array.isArray(res) ? res : res?.boards || []).map(b => (typeof b === 'string' ? b : b?.slug)).filter(Boolean)
  } catch {
    /* older backend: single board */
  }
  for (const b of boards) {
    if (tried.has(b)) continue
    try {
      return { data: await tryBoard(b), board: b }
    } catch (e) {
      if (!is404(e)) throw e
    }
  }
  return null
}

async function loadTask(id) {
  const conns = await connections()
  const active = activeConnection()
  const hint = hintOf.get(id)
  const named = hint?.gateway ? findConnection(conns, hint.gateway) : null
  if (hint?.gateway && !named) throw new Error(`gateway "${hint.gateway}" is not connected`)
  const order = named ? [named] : [...new Set([connOf.get(id), active, ...conns.map(c => c.id)].filter(Boolean))]
  let lastError = null
  for (const conn of order) {
    try {
      const hit = await loadTaskOn(id, conn, boardOf.get(id) || hint?.board)
      if (hit) {
        boardOf.set(id, hit.board)
        connOf.set(id, conn)
        return { ...hit, conn, gateway: conns.find(c => c.id === conn)?.label || conn }
      }
    } catch (e) {
      lastError = e
    }
  }
  if (lastError && order.length === 1) throw lastError
  throw new Error(`task ${id} not found on ${named ? hint.gateway : 'any connected gateway'}`)
}

/** Explicit board slug for an entry: '' means the gateway's current board, which the Desktop board page
 *  may not have selected — resolve it so the drawer fetches the right board. */
async function resolveBoard(entry) {
  if (entry.board) return entry.board
  try {
    const res = await api('/api/plugins/kanban/boards', entry.conn)
    return (res && !Array.isArray(res) && typeof res.current === 'string' && res.current) || 'default'
  } catch {
    return 'default'
  }
}

/** Hook name core's kanban plugin installs (fork patch: kanban open-task hook). */
export const OPEN_TASK_HOOK = '__hermesKanbanOpenTask'

/** Open a task's card drawer on the board page. Falls back to the plain board on an older core. */
export async function openCard(id, entry, win = globalThis, nav = hostRef) {
  const board = await resolveBoard(entry)
  const hook = win[OPEN_TASK_HOOK]
  // Owning gateway only for a card on another gateway; the routed one needs no hint.
  const gateway = entry.conn && entry.conn !== activeConnection() ? entry.conn : null
  if (typeof hook === 'function' && hook(id, board, gateway) !== false) return true
  nav.navigate?.('/kanban')
  nav.notify?.({ kind: 'info', title: 'Opened the board', message: `This Hermes build can't open card ${id} directly yet; it can after the next Hermes update.` })
  return false
}

export function getTask(id, force = false) {
  const hit = cache.get(id)
  if (!force && hit && Date.now() - hit.at < CACHE_MS) return Promise.resolve(hit)
  if (inflight.has(id)) return inflight.get(id)
  const p = loadTask(id)
    .then(({ data, board, conn, gateway }) => {
      const entry = { at: Date.now(), data, board, conn, gateway }
      cache.set(id, entry)
      return entry
    })
    .catch(error => {
      const entry = { at: Date.now() - CACHE_MS + 5_000, error: String(error?.message || error) }
      cache.set(id, entry)
      return entry
    })
    .finally(() => inflight.delete(id))
  inflight.set(id, p)
  return p
}

// ── DOM helpers ──────────────────────────────────────────────────────────────

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag)
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue
    if (k === 'class') el.className = v
    else if (k === 'style') el.style.cssText = v
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v)
    else el.setAttribute(k, v === true ? '' : String(v))
  }
  // Text children of a flex row each get their own box, so `gap` separates them.
  const wrapText = el.classList.contains('tm-row')
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue
    if (kid instanceof Node) el.append(kid)
    else if (wrapText) el.append(h('span', null, String(kid)))
    else el.append(document.createTextNode(String(kid)))
  }
  return el
}

const pill = status =>
  h('span', { class: 'tm-pill', style: `--tm-c:${statusColor(status)}` }, String(status || 'unknown'))

/** Text with embedded task ids turned into links that open that task in the popup. */
function linkified(text, onOpen) {
  const frag = document.createDocumentFragment()
  const s = String(text || '')
  let last = 0
  for (const m of findTaskIds(s)) {
    frag.append(s.slice(last, m.start))
    frag.append(h('a', { class: 'tm-id', href: '#', onclick: e => (e.preventDefault(), onOpen(m.id)) }, m.id))
    last = m.end
  }
  frag.append(s.slice(last))
  return frag
}

const STYLE = `
::highlight(${HL_NAME}) {
  color: var(--dt-primary, #4a84fe);
  text-decoration: underline dotted;
  text-decoration-color: color-mix(in srgb, var(--dt-primary, #4a84fe) 70%, transparent);
  text-underline-offset: 3px;
}
html[data-tm-hover] ${SCOPE}, html[data-tm-hover] ${SCOPE} * { cursor: pointer !important; }
.tm-root { position: fixed; inset: 0; pointer-events: none; z-index: 2147483000;
  font-family: var(--font-sans, -apple-system, system-ui, sans-serif);
  color: var(--dt-popover-foreground, var(--dt-foreground, #e6edf3)); }
.tm-root * { box-sizing: border-box; }
.tm-tip, .tm-card { background-color: var(--dt-background, #0d1117);
  background-image: linear-gradient(var(--dt-popover, #161b22), var(--dt-popover, #161b22)); border: 1px solid var(--dt-border, #30363d);
  box-shadow: 0 12px 40px rgba(0,0,0,.45); }
.tm-tip { position: fixed; pointer-events: none; width: max-content; max-width: min(380px, calc(100vw - 16px)); padding: 9px 11px; border-radius: 10px;
  font-size: 12px; line-height: 1.4; opacity: 0; transform: translateY(2px); transition: opacity .12s, transform .12s; }
.tm-tip.tm-show { opacity: 1; transform: none; }
.tm-tip .tm-title { font-weight: 600; font-size: 13px; margin: 3px 0 5px; }
.tm-row { display: flex; flex-wrap: wrap; gap: 4px 12px; align-items: center; }
.tm-head .tm-row.tm-muted > span + span::before, .tm-tip .tm-row.tm-muted > span + span::before { content: '·'; margin-right: 12px; opacity: .6; }
.tm-muted { color: var(--dt-muted-foreground, #8b949e); }
.tm-mono { font-family: var(--font-mono, ui-monospace, monospace); font-size: 11.5px; }
.tm-pill { display: inline-flex; align-items: center; gap: 5px; padding: 1px 7px; border-radius: 999px; font-size: 11px;
  font-weight: 600; color: var(--tm-c); background: color-mix(in srgb, var(--tm-c) 16%, transparent);
  border: 1px solid color-mix(in srgb, var(--tm-c) 35%, transparent); text-transform: lowercase; }
.tm-pill::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--tm-c); }
.tm-overlay { position: fixed; inset: 0; pointer-events: auto; background: rgba(0,0,0,.42);
  display: flex; align-items: flex-start; justify-content: center; padding: 7vh 24px 24px; }
.tm-card { width: min(760px, 100%); max-height: 84vh; display: flex; flex-direction: column; border-radius: 14px; overflow: hidden; }
.tm-head { padding: 14px 18px 12px; border-bottom: 1px solid var(--dt-border, #30363d); }
.tm-head h2 { margin: 6px 0 8px; font-size: 16px; font-weight: 650; line-height: 1.35; }
.tm-body { padding: 4px 18px 16px; overflow: auto; font-size: 13px; line-height: 1.5; }
.tm-sec { margin-top: 14px; }
.tm-sec > h3 { margin: 0 0 6px; font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase;
  color: var(--dt-muted-foreground, #8b949e); }
.tm-pre { font: inherit; white-space: pre-wrap; word-break: break-word; margin: 0; padding: 9px 11px; border-radius: 8px;
  background: var(--dt-muted, rgba(255,255,255,.04)); border: 1px solid var(--dt-border, #30363d); max-height: 260px; overflow: auto; }
.tm-pre.tm-err { border-color: color-mix(in srgb, #f85149 50%, transparent); }
.tm-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 5px; }
.tm-list li { display: flex; gap: 8px; align-items: baseline; }
.tm-list li .tm-grow { flex: 1; min-width: 0; }
.tm-id { color: var(--dt-primary, #4a84fe); text-decoration: none; font-family: var(--font-mono, monospace); font-size: 12px; }
.tm-id:hover { text-decoration: underline; }
.tm-foot { display: flex; gap: 8px; padding: 10px 18px; border-top: 1px solid var(--dt-border, #30363d); flex-wrap: wrap; }
.tm-btn { appearance: none; border: 1px solid var(--dt-border, #30363d); background: transparent; color: inherit;
  font: inherit; font-size: 12.5px; padding: 5px 11px; border-radius: 8px; cursor: pointer; }
.tm-btn:hover { background: var(--dt-accent, rgba(255,255,255,.06)); }
.tm-btn.tm-primary { background: var(--dt-primary-solid, #3b6acb); border-color: transparent; color: var(--dt-primary-solid-foreground, #fff); }
.tm-x { margin-left: auto; border: 0; background: transparent; color: inherit; font-size: 18px; cursor: pointer; opacity: .6; line-height: 1; }
.tm-x:hover { opacity: 1; }
.tm-comment { padding: 7px 10px; border-radius: 8px; background: var(--dt-muted, rgba(255,255,255,.04)); }
.tm-comment .tm-pre { background: transparent; border: 0; padding: 4px 0 0; max-height: 180px; }
details.tm-sec > summary { cursor: pointer; font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase;
  color: var(--dt-muted-foreground, #8b949e); }
`

// ── highlighter ──────────────────────────────────────────────────────────────

function scanRanges() {
  const ranges = []
  for (const root of document.querySelectorAll(SCOPE)) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: n =>
        n.data.indexOf('t_') !== -1 && !n.parentElement?.closest('[contenteditable="true"], textarea, input')
          ? NodeFilter.FILTER_ACCEPT
          : NodeFilter.FILTER_REJECT
    })
    let n
    while ((n = walker.nextNode())) {
      for (const m of findTaskIds(n.data)) {
        const r = document.createRange()
        r.setStart(n, m.start)
        r.setEnd(n, m.end)
        ranges.push(r)
      }
    }
  }
  return ranges
}

/** The task id under viewport point (x, y), or null. */
function idAtPoint(x, y) {
  const target = document.elementFromPoint(x, y)
  if (!target || !target.closest?.(SCOPE) || target.closest('.tm-root')) return null
  let node, offset
  if (document.caretPositionFromPoint) {
    const p = document.caretPositionFromPoint(x, y)
    node = p?.offsetNode
    offset = p?.offset
  } else if (document.caretRangeFromPoint) {
    const r = document.caretRangeFromPoint(x, y)
    node = r?.startContainer
    offset = r?.startOffset
  }
  if (!node || node.nodeType !== 3) return null
  for (const m of findTaskIds(node.data)) {
    if (offset < m.start || offset > m.end) continue
    const q = refQualifier(node.data.slice(0, m.start))
    if (q && (q.gateway || q.board)) hintOf.set(m.id, q)
    // The caret snaps to the nearest character even past the end of a line: require the
    // pointer to be inside the id's own boxes.
    const r = document.createRange()
    r.setStart(node, m.start)
    r.setEnd(node, m.end)
    for (const b of r.getClientRects()) {
      if (x >= b.left - 1 && x <= b.right + 1 && y >= b.top - 1 && y <= b.bottom + 1) return { id: m.id, rect: b }
    }
  }
  return null
}

// ── UI ───────────────────────────────────────────────────────────────────────

function createUI(host) {
  const root = h('div', { class: 'tm-root', 'data-plugin': PLUGIN_ID })
  const style = h('style', { 'data-plugin': PLUGIN_ID }, STYLE)
  document.head.append(style)
  document.body.append(root)

  // tooltip
  let tip = null
  let tipFor = null
  const hideTip = () => {
    tipFor = null
    tip?.remove()
    tip = null
  }
  const placeTip = rect => {
    if (!tip) return
    const w = tip.offsetWidth
    const ht = tip.offsetHeight
    let left = Math.min(Math.max(8, rect.left), window.innerWidth - w - 8)
    let top = rect.bottom + 6
    if (top + ht > window.innerHeight - 8) top = rect.top - ht - 6
    tip.style.left = `${left}px`
    tip.style.top = `${top}px`
  }
  const tipContent = (id, entry) => {
    if (!entry) return [h('div', { class: 'tm-mono tm-muted' }, id), h('div', { class: 'tm-muted' }, 'Loading…')]
    if (entry.error) return [h('div', { class: 'tm-mono tm-muted' }, id), h('div', { class: 'tm-muted' }, entry.error)]
    const t = entry.data.task || {}
    const a = t.age || {}
    return [
      h('div', { class: 'tm-row' }, pill(t.status), h('span', { class: 'tm-mono tm-muted' }, t.id), entry.board ? h('span', { class: 'tm-muted' }, `· ${entry.board}`) : null, entry.conn && entry.conn !== activeConnection() ? h('span', { class: 'tm-muted' }, `· ${entry.gateway}`) : null),
      h('div', { class: 'tm-title' }, t.title || '(untitled)'),
      h(
        'div',
        { class: 'tm-row tm-muted' },
        t.assignee ? `@${t.assignee}` : 'unassigned',
        t.priority ? `p${t.priority}` : null,
        t.status === 'running' && t.started_at ? `running ${duration(a.started_age_seconds ?? Date.now() / 1000 - t.started_at)}` : null,
        t.completed_at ? `finished ${ago(t.completed_at)}` : `created ${ago(t.created_at)}`,
        (entry.data.comments || []).length ? `${entry.data.comments.length} comments` : null
      ),
      t.latest_summary || t.result ? h('div', { style: 'margin-top:5px' }, truncate(t.latest_summary || t.result, 220)) : null,
      t.status === 'blocked' ? h('div', { style: 'margin-top:5px;color:#f85149' }, truncate(`blocked${t.block_kind ? ` (${t.block_kind})` : ''}${t.last_failure_error ? `: ${t.last_failure_error}` : ''}`, 200)) : null,
      h('div', { class: 'tm-muted', style: 'margin-top:5px;font-size:11px' }, 'Click for details')
    ]
  }
  const showTip = (id, rect) => {
    if (tipFor === id && tip) return placeTip(rect)
    hideTip()
    tipFor = id
    tip = h('div', { class: 'tm-tip' }, tipContent(id, cache.get(id)?.data || cache.get(id)?.error ? cache.get(id) : null))
    root.append(tip)
    placeTip(rect)
    requestAnimationFrame(() => tip?.classList.add('tm-show'))
    void getTask(id).then(entry => {
      if (tipFor !== id || !tip) return
      tip.replaceChildren(...tipContent(id, entry).filter(Boolean))
      placeTip(rect)
    })
  }

  // modal
  let modal = null
  let modalId = null
  let history = []
  let liveTimer = null
  const closeModal = () => {
    modal?.remove()
    modal = null
    modalId = null
    history = []
    if (liveTimer) clearInterval(liveTimer)
    liveTimer = null
  }

  const section = (title, ...kids) => h('div', { class: 'tm-sec' }, h('h3', null, title), ...kids)

  const renderModal = (id, entry) => {
    const open = next => {
      if (next === modalId) return
      // Linked tasks live on the same gateway as this one: look there first.
      if (entry?.conn && !connOf.has(next)) connOf.set(next, entry.conn)
      history.push(modalId)
      void openModal(next, false)
    }
    if (!entry || entry.error) {
      return h(
        'div',
        { class: 'tm-card', onclick: e => e.stopPropagation() },
        h('div', { class: 'tm-head' }, h('div', { class: 'tm-row' }, h('span', { class: 'tm-mono tm-muted' }, id), h('button', { class: 'tm-x', title: 'Close', onclick: closeModal }, '×'))),
        h('div', { class: 'tm-body' }, h('p', { class: 'tm-muted' }, entry ? entry.error : 'Loading…'))
      )
    }
    const d = entry.data
    const t = d.task || {}
    const a = t.age || {}
    const remote = !!entry.conn && entry.conn !== activeConnection()
    const meta = h(
      'div',
      { class: 'tm-row tm-muted' },
      t.assignee ? `@${t.assignee}` : 'unassigned',
      t.priority ? `priority ${t.priority}` : null,
      t.created_by ? `by ${t.created_by}` : null,
      `created ${ago(t.created_at)}`,
      t.started_at ? `started ${ago(t.started_at)}` : null,
      t.completed_at ? `finished ${ago(t.completed_at)}` : null,
      a.time_to_complete_seconds ? `took ${duration(a.time_to_complete_seconds)}` : null,
      t.goal_mode ? 'goal mode' : null,
      t.model_override ? `model ${t.model_override}` : null
    )

    const parents = d.links?.parents || []
    const children = d.links?.children || []
    const linkInfo = new Map((d.link_tasks || []).map(x => [x.id, x]))
    for (const c of d.child_results || []) linkInfo.set(c.id, { ...linkInfo.get(c.id), ...c })
    const linkRow = lid => {
      const x = linkInfo.get(lid) || {}
      return h(
        'li',
        null,
        h('a', { class: 'tm-id', href: '#', onclick: e => (e.preventDefault(), open(lid)) }, lid),
        x.status ? pill(x.status) : null,
        h('span', { class: 'tm-grow' }, x.title || '')
      )
    }

    const runs = (d.runs || []).slice(-5).reverse()
    const comments = d.comments || []
    const events = (d.events || []).filter(e => e.kind !== 'heartbeat').slice(-15).reverse()
    const diags = Array.isArray(t.diagnostics) ? t.diagnostics : []

    const body = h(
      'div',
      { class: 'tm-body' },
      t.status === 'blocked' || t.last_failure_error
        ? section(t.status === 'blocked' ? `Blocked${t.block_kind ? ` · ${t.block_kind}` : ''}` : 'Last failure', h('pre', { class: 'tm-pre tm-err' }, t.last_failure_error || (events.find(e => e.kind === 'blocked')?.payload?.reason ?? 'blocked')))
        : null,
      Array.isArray(diags) && diags.length
        ? section('Diagnostics', h('ul', { class: 'tm-list' }, diags.map(x => h('li', null, h('span', { class: 'tm-grow' }, typeof x === 'string' ? x : truncate(x.title || x.message || x.kind || JSON.stringify(x), 300), x?.actions?.length ? h('div', { class: 'tm-muted' }, x.actions.map(a => a.label).join(' · ')) : null)))))
        : null,
      t.latest_summary || t.result ? section(t.result && !t.latest_summary ? 'Result' : 'Latest summary', h('pre', { class: 'tm-pre' }, linkified(t.latest_summary || t.result, open))) : null,
      t.body ? section('Description', h('pre', { class: 'tm-pre' }, linkified(t.body, open))) : null,
      parents.length ? section(`Parents (${parents.length})`, h('ul', { class: 'tm-list' }, parents.map(linkRow))) : null,
      children.length ? section(`Children (${children.length})`, h('ul', { class: 'tm-list' }, children.map(linkRow))) : null,
      runs.length
        ? section(
            `Runs (${(d.runs || []).length})`,
            h(
              'ul',
              { class: 'tm-list' },
              runs.map(r =>
                h(
                  'li',
                  null,
                  pill(r.outcome || r.status),
                  h(
                    'span',
                    { class: 'tm-grow' },
                    h('span', { class: 'tm-muted' }, `#${r.id} · ${r.profile || ''} · ${ago(r.started_at)}${r.ended_at ? ` · ${duration(r.ended_at - r.started_at)}` : ''}`),
                    r.summary || r.error ? h('div', null, truncate(r.summary || r.error, 400)) : null
                  )
                )
              )
            )
          )
        : null,
      comments.length
        ? section(
            `Comments (${comments.length})`,
            h(
              'div',
              { style: 'display:flex;flex-direction:column;gap:6px' },
              comments.slice(-6).map(c =>
                h('div', { class: 'tm-comment' }, h('div', { class: 'tm-muted', style: 'font-size:11.5px' }, `${c.author || 'unknown'} · ${ago(c.created_at)}`), h('pre', { class: 'tm-pre' }, linkified(c.body, open)))
              )
            )
          )
        : null,
      events.length
        ? h(
            'details',
            { class: 'tm-sec' },
            h('summary', null, `Events (${events.length} recent)`),
            h(
              'ul',
              { class: 'tm-list', style: 'margin-top:6px' },
              events.map(e =>
                h(
                  'li',
                  null,
                  h('span', { class: 'tm-muted', style: 'min-width:64px' }, ago(e.created_at)),
                  h('span', { class: 'tm-mono' }, e.kind),
                  e.payload ? h('span', { class: 'tm-grow tm-muted' }, truncate(typeof e.payload === 'string' ? e.payload : JSON.stringify(e.payload), 160)) : null
                )
              )
            )
          )
        : null,
      t.workspace_path ? h('div', { class: 'tm-sec tm-muted tm-mono' }, `${t.workspace_kind || 'workspace'}: ${t.workspace_path}`) : null
    )

    const copy = (text, btn) => {
      void navigator.clipboard?.writeText(text).then(() => {
        const old = btn.textContent
        btn.textContent = 'Copied'
        setTimeout(() => (btn.textContent = old), 1000)
      })
    }
    const sess = sessionTarget(d, host.state?.focusedSessionId?.get?.())
    const foot = h(
      'div',
      { class: 'tm-foot' },
      // Open card: the board page's drawer for this task (edit fields, comment), on its own gateway + board.
      typeof host.navigate === 'function'
        ? h('button', { class: 'tm-btn tm-primary', title: 'Open this card on the board to edit it or comment', onclick: () => (closeModal(), void openCard(t.id, entry)) }, 'Open card')
        : null,
      // Worker session + board live on the task's gateway; both buttons open the ACTIVE one.
      !remote && sess && typeof host.openSession === 'function'
        ? h('button', { class: 'tm-btn', onclick: () => (closeModal(), void host.openSession(sess.id).catch(e => host.notifyError?.(e, `Could not ${sess.label.toLowerCase()}`))) }, sess.label)
        : null,
      !remote && typeof host.navigate === 'function' ? h('button', { class: 'tm-btn', onclick: () => (closeModal(), host.navigate('/kanban')) }, 'Open board') : null,
      h('button', { class: 'tm-btn', onclick: e => copy(t.id, e.currentTarget) }, 'Copy id'),
      h('button', { class: 'tm-btn', onclick: () => void openModal(id, false, true) }, 'Refresh'),
      history.length ? h('button', { class: 'tm-btn', onclick: () => void openModal(history.pop(), false) }, '← Back') : null
    )

    return h(
      'div',
      { class: 'tm-card', role: 'dialog', 'aria-label': `Task ${t.id}`, onclick: e => e.stopPropagation() },
      h(
        'div',
        { class: 'tm-head' },
        h('div', { class: 'tm-row' }, pill(t.status), h('span', { class: 'tm-mono tm-muted' }, t.id), entry.board ? h('span', { class: 'tm-muted' }, `· board ${entry.board}`) : null, remote ? h('span', { class: 'tm-muted' }, `· on ${entry.gateway}`) : null, h('button', { class: 'tm-x', title: 'Close (Esc)', onclick: closeModal }, '×')),
        h('h2', null, t.title || '(untitled)'),
        meta
      ),
      body,
      foot
    )
  }

  const openModal = async (id, fresh = true, force = false) => {
    hideTip()
    if (fresh) history = []
    modalId = id
    if (!modal) {
      modal = h('div', { class: 'tm-overlay', onclick: closeModal })
      root.append(modal)
    }
    const cached = cache.get(id)
    modal.replaceChildren(renderModal(id, cached && !force ? cached : null))
    const entry = await getTask(id, true)
    if (modalId !== id || !modal) return
    const scroll = modal.querySelector('.tm-body')?.scrollTop || 0
    modal.replaceChildren(renderModal(id, entry))
    const b = modal.querySelector('.tm-body')
    if (b && !fresh) b.scrollTop = scroll
    if (liveTimer) clearInterval(liveTimer)
    liveTimer = LIVE_STATUSES.has(entry.data?.task?.status)
      ? setInterval(async () => {
          if (!modal || modalId !== id || document.hidden) return
          const e2 = await getTask(id, true)
          if (!modal || modalId !== id) return
          const st = modal.querySelector('.tm-body')?.scrollTop || 0
          const openDetails = !!modal.querySelector('details[open]')
          modal.replaceChildren(renderModal(id, e2))
          const nb = modal.querySelector('.tm-body')
          if (nb) nb.scrollTop = st
          if (openDetails) modal.querySelector('details')?.setAttribute('open', '')
        }, LIVE_REFRESH_MS)
      : null
  }

  const dispose = () => {
    closeModal()
    hideTip()
    root.remove()
    style.remove()
  }
  return { root, showTip, hideTip, openModal, closeModal, isOpen: () => !!modal, dispose }
}

// ── plugin ───────────────────────────────────────────────────────────────────

export default {
  id: PLUGIN_ID,
  name: 'Task Mentions',
  register(ctx) {
    if (typeof document === 'undefined' || typeof MutationObserver === 'undefined') return
    registerWith(ctx, sdkHost)
  }
}

function registerWith(ctx, host) {
  hostRef = host
  cache.clear()
  boardOf.clear()
  connOf.clear()
  hintOf.clear()
  connCache = { at: 0, rows: [] }
  const ui = createUI(host)
  const hasHighlights = typeof CSS !== 'undefined' && CSS.highlights && typeof Highlight === 'function'

  // highlight upkeep
  let scanTimer = 0
  const rescan = () => {
    scanTimer = 0
    if (!hasHighlights) return
    const ranges = scanRanges()
    if (ranges.length) CSS.highlights.set(HL_NAME, new Highlight(...ranges))
    else CSS.highlights.delete(HL_NAME)
  }
  const schedule = () => {
    if (!scanTimer) scanTimer = setTimeout(rescan, 150)
  }
  const observer = new MutationObserver(records => {
    for (const r of records) if (!ui.root.contains(r.target)) return schedule()
  })
  observer.observe(document.body, { childList: true, subtree: true, characterData: true })
  rescan()

  // hover / click
  let hoverTimer = 0
  let hovering = null
  let lastMove = null
  let raf = 0
  const setHover = hit => {
    const id = hit?.id || null
    if (id) document.documentElement.setAttribute('data-tm-hover', '')
    else document.documentElement.removeAttribute('data-tm-hover')
    if (id === hovering?.id) {
      if (hit) hovering = hit
      return
    }
    hovering = hit
    clearTimeout(hoverTimer)
    ui.hideTip()
    if (hit && !ui.isOpen()) hoverTimer = setTimeout(() => hovering && ui.showTip(hovering.id, hovering.rect), HOVER_DELAY_MS)
  }
  const onMove = e => {
    lastMove = e
    if (raf) return
    raf = requestAnimationFrame(() => {
      raf = 0
      if (lastMove.buttons) return setHover(null)
      setHover(idAtPoint(lastMove.clientX, lastMove.clientY))
    })
  }
  const onClick = e => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
    const sel = window.getSelection?.()
    if (sel && !sel.isCollapsed && String(sel).trim()) return // finishing a text selection
    const hit = idAtPoint(e.clientX, e.clientY)
    if (!hit) return
    e.preventDefault()
    e.stopPropagation()
    clearTimeout(hoverTimer)
    void ui.openModal(hit.id)
  }
  const onKey = e => {
    if (e.key === 'Escape' && ui.isOpen()) {
      e.preventDefault()
      e.stopPropagation()
      ui.closeModal()
    }
  }
  const onScroll = () => {
    if (hovering) setHover(null)
  }
  document.addEventListener('mousemove', onMove, { passive: true })
  document.addEventListener('click', onClick, true)
  document.addEventListener('keydown', onKey, true)
  document.addEventListener('scroll', onScroll, { capture: true, passive: true })
  window.addEventListener('blur', onScroll)

  // Let other code (and the agent, via the console) open a task directly.
  window.__taskMentionsOpen = id => ui.openModal(String(id))

  const dispose = () => {
    observer.disconnect()
    clearTimeout(scanTimer)
    clearTimeout(hoverTimer)
    if (raf) cancelAnimationFrame(raf)
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('click', onClick, true)
    document.removeEventListener('keydown', onKey, true)
    document.removeEventListener('scroll', onScroll, { capture: true })
    window.removeEventListener('blur', onScroll)
    document.documentElement.removeAttribute('data-tm-hover')
    if (hasHighlights) CSS.highlights.delete(HL_NAME)
    delete window.__taskMentionsOpen
    ui.dispose()
  }
  ctx.onDispose?.(dispose)
}
