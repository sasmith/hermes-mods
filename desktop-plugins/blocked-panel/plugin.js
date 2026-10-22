/**
 * Blocked Panel — when the focused session is blocked, an amber panel above the
 * composer input says exactly what YOU need to do to unblock it.
 *
 * A session is blocked when:
 *   - one of its Kanban cards (created or shepherded by any id in its compression
 *     lineage, any board) sits in `blocked` waiting on a human. Each card shows the
 *     instructions the worker wrote when it blocked (the latest `blocked` event's
 *     reason; for a card created blocked with no reason, the card body) as a NUMBERED
 *     checklist. Source order: the `blocked-panel` agent plugin's model rewrite
 *     (POST /api/plugins/blocked-panel/steps, disk-cached) → the note's own numbering →
 *     one step per sentence. The list always ends with how to hand the card back; the raw
 *     note stays one click away ("Agent's note"). Buttons: Open card · Done → unblock
 *     · Reply & unblock (posts your note as a card comment, then unblocks, so the
 *     resumed worker reads it).
 *     Skipped: `transient` blocks (they clear on their own) and reasons that say
 *     "no human action needed" (time waits that should have been kanban_schedule).
 *   - you parked it with ⌘B (core's `hermes.desktop.blockedSessionIds`). With no
 *     blocked cards that shows one line: parked by you, ⌘B to unblock.
 *
 * Data: `/api/session-kanban-tasks?ids=<lineage>` (every board) + per blocked card
 * `/api/plugins/kanban/tasks/<id>?board=<slug>` (events → reason/kind). Polls every
 * 10 s while visible; card details are cached 60 s or until the blocked set changes.
 * Only the FOCUSED session is known to a composer plugin, so every tile's composer
 * renders the focused chat's panel only when that tile is the focused one.
 *
 * Retired as a composer panel when user-steps is loaded (window.__userSteps.ownsComposer): its "For you"
 * strip shows these cards via window.__blockedPanel.{snapshot,subscribe,unblockCard,openCard,checklist}.
 */
import { COMPOSER_AREAS, host, useValue } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'blocked-panel'
const POLL_MS = 10_000
const DETAIL_TTL_MS = 60_000
const LINEAGE_TTL_MS = 5 * 60_000
const PARKED_KEY = 'hermes.desktop.blockedSessionIds'
const COLLAPSED_LINES = 4

const str = v => (typeof v === 'string' ? v : v == null ? '' : String(v))
// Some cards store literal "\u2014" escapes in their title/reason; show the character.
export const unescapeText = v => str(v).replace(/\\u([0-9a-fA-F]{4})/g, (_, h) => String.fromCharCode(parseInt(h, 16)))
const log = (...a) => console.info(`[${ID}]`, ...a)

// ── pure helpers (exported for tests) ───────────────────────────────────────

// Web URLs, and absolute / home-relative local paths that start a word (so
// "7/10" or "and/or" never match). Trailing sentence punctuation is not part of the link.
// Scheme-less web addresses ("github.com/settings/billing", "www.x.io") link too, but only on a common TLD so
// file names like plugin.js / README.md never do; "user@host.com" doesn't (no word start).
const LINK_RE = /(https?:\/\/[^\s<>"'`]+)|(?:^|(?<=[\s("'`]))(~?\/[A-Za-z0-9._~-][^\s<>"'`()]*)|(?:^|(?<=[\s("'`]))((?:[A-Za-z0-9-]+\.)+(?:com|org|net|io|dev|ai|app|co|in|gov|edu|me|sh|so|gg|tv|us|uk|xyz)(?=[\/\s.,;:!?)\]}"'`]|$)(?:\/[^\s<>"'`]*)?)/g
/** href for a link segment: bare web addresses get https://, paths file://. */
export const linkHref = x => (x.kind === 'path' ? `file://${x.value}` : /^https?:\/\//i.test(x.value) ? x.value : `https://${x.value}`)
const TRAIL_RE = /[.,;:!?)\]}]+$/

/** text → [{ kind: 'text'|'url'|'path', value }] for rendering clickable links. */
export function linkSegments(text) {
  const s = str(text)
  const out = []
  let last = 0
  for (const m of s.matchAll(LINK_RE)) {
    let value = m[0]
    const trail = value.match(TRAIL_RE)?.[0] || ''
    if (trail) value = value.slice(0, -trail.length)
    const kind = m[2] ? 'path' : 'url'
    if (kind === 'path' && !/[A-Za-z]/.test(value)) continue
    if (m.index > last) out.push({ kind: 'text', value: s.slice(last, m.index) })
    out.push({ kind, value })
    last = m.index + value.length
  }
  if (last < s.length) out.push({ kind: 'text', value: s.slice(last) })
  return out
}

/** core's persisted ⌘B list (JSON string array) → string[]; junk → []. */
export function parseParkedIds(raw) {
  try {
    const parsed = JSON.parse(str(raw) || '[]')
    return Array.isArray(parsed) ? parsed.filter(x => typeof x === 'string' && x) : []
  } catch {
    return []
  }
}

export function isParked(lineage, parkedIds) {
  const parked = new Set(parkedIds || [])
  for (const id of lineage || []) if (parked.has(id)) return true
  return false
}

const payloadOf = e => {
  const p = e?.payload
  if (p && typeof p === 'object') return p
  try {
    return JSON.parse(str(p) || '{}') || {}
  } catch {
    return {}
  }
}

/** The block currently in force: the newest `blocked` event after the newest
 *  `unblocked`. `initial_status` (card created blocked) carries no instructions. */
export function latestBlock(events) {
  const list = (Array.isArray(events) ? events : []).slice().sort((a, b) => (Number(a?.id) || 0) - (Number(b?.id) || 0))
  let block = null
  for (const e of list) {
    if (e?.kind === 'unblocked') block = null
    else if (e?.kind === 'blocked') {
      const p = payloadOf(e)
      const reason = str(p.reason).trim()
      block = {
        reason: reason === 'initial_status' ? '' : reason,
        kind: str(p.kind) || null,
        at: Number(e.created_at) || 0
      }
    }
  }
  return block
}

const NO_HUMAN_RE = /^\s*(no human action (is )?(needed|required)|nothing (is )?needed from you|waiting on time)/i

/** Does this block need a person? transient → clears itself; "no human action
 *  needed" text → a time wait. Everything else (needs_input, capability, unset) does. */
export function needsHuman(block) {
  if (!block) return true
  if (block.kind === 'transient' || block.kind === 'dependency') return false
  return !NO_HUMAN_RE.test(block.reason || '')
}

/** Split instructions into { intro, steps[] }. Steps come from line-leading
 *  markers ("1) ", "2. ", "- ") or, on one line, from inline "1) … 2) …" runs
 *  that count up from 0 or 1. Anything else → intro only. */
export function stepsOf(text) {
  const t = str(text).replace(/\r\n/g, '\n').trim()
  if (!t) return { intro: '', steps: [], start: 1 }
  const lines = t.split('\n').map(l => l.trim()).filter(Boolean)
  const marker = /^(?:\d{1,2}[).]|[-*•])\s+/
  if (lines.filter(l => marker.test(l)).length >= 2) {
    const intro = []
    const steps = []
    for (const l of lines) {
      if (marker.test(l)) steps.push(l.replace(marker, ''))
      else if (steps.length) steps[steps.length - 1] += ` ${l}`
      else intro.push(l)
    }
    return { intro: intro.join(' '), steps, start: 1 }
  }
  const flat = lines.join(' ')
  const re = /(?:^|[\s:;.])(\d{1,2})\)\s+/g
  const hits = []
  let m
  while ((m = re.exec(flat))) hits.push({ n: Number(m[1]), start: m.index + m[0].length, at: m.index + (m[0].length - m[0].trimStart().length) })
  // keep the first run that counts up by one from 0 or 1
  const run = []
  for (const h of hits) {
    if (!run.length) {
      if (h.n === 0 || h.n === 1) run.push(h)
    } else if (h.n === run[run.length - 1].n + 1) run.push(h)
  }
  if (run.length < 2) return { intro: flat, steps: [], start: 1 }
  const intro = flat.slice(0, run[0].at).trim().replace(/[:;,]\s*$/, ':')
  const steps = run.map((h, i) => flat.slice(h.start, i + 1 < run.length ? run[i + 1].at : undefined).trim().replace(/[;,]$/, ''))
  return { intro, steps, start: run[0].n }
}

const KIND_LABEL = { needs_input: 'Needs your input', capability: 'Needs access you have' }

/** Session tasks + fetched details → panel items (human-blocked cards only). */
export function blockedItems(tasks, details) {
  const out = []
  const seen = new Set()
  for (const t of Array.isArray(tasks) ? tasks : []) {
    if (!t?.id || str(t.status) !== 'blocked' || seen.has(t.id)) continue
    seen.add(t.id)
    const d = details?.get ? details.get(detailKey(t)) : details?.[detailKey(t)]
    const block = d ? latestBlock(d.events) : null
    if (!needsHuman(block)) continue
    const fromBody = !block?.reason
    const text = unescapeText(block?.reason || str(d?.task?.body).trim())
    out.push({
      id: str(t.id),
      board: str(t.board) || 'default',
      title: unescapeText(t.title) || str(t.id),
      kind: block?.kind || null,
      kindLabel: KIND_LABEL[block?.kind] || 'Waiting on you',
      text,
      fromBody,
      loaded: Boolean(d),
      at: block?.at || 0,
      ...stepsOf(text)
    })
  }
  return out.sort((a, b) => b.at - a.at)
}

export const detailKey = t => `${str(t?.board) || 'default'}:${str(t?.id)}`

export const HANDBACK = 'When you\u2019re done, click Done \u2192 unblock (or Reply & unblock to send the agent an answer).'
const HANDBACK_RE = /\bunblock\b/i

/** Prose → one step per sentence (fallback when the note isn't enumerated and no rewrite exists). */
export function sentenceSteps(text) {
  const t = str(text).replace(/\s+/g, ' ').trim()
  if (!t) return []
  return t
    .split(/(?<=[.!?;])\s+(?=[A-Z0-9"'\u201c(])/)
    .map(s => s.trim().replace(/;$/, '.'))
    .filter(s => s.length > 1)
}

/** The numbered checklist the panel shows: model rewrite → the note's own numbering → one step per
 *  sentence. Always ends with how to hand the card back. */
export function checklist(item, rewritten) {
  let steps
  let start = 1
  let intro = ''
  let source
  if (Array.isArray(rewritten) && rewritten.length) {
    steps = rewritten.slice()
    source = 'rewritten'
  } else if (item?.steps?.length >= 2) {
    steps = item.steps.slice()
    start = item.start ?? 1
    intro = item.intro || ''
    source = 'parsed'
  } else {
    steps = sentenceSteps(item?.text)
    source = 'sentences'
  }
  if (!steps.length) steps = ['Open the card to see what it needs.']
  if (!HANDBACK_RE.test(steps[steps.length - 1])) steps.push(HANDBACK)
  return { steps, start, intro, source }
}

export const PARKED_STEPS = ['Do what you parked this chat for (see its last messages above).', 'Press \u2318B to unblock it.']

export const rewriteKey = item => `${item.board}:${item.id}|${item.text.length}|${item.text.slice(0, 64)}|${item.text.slice(-64)}`

// ── data ────────────────────────────────────────────────────────────────────

function activeConnection() {
  try {
    return host.activeConnectionId?.() || host.state?.connectionId?.get?.() || undefined
  } catch {
    return undefined
  }
}

function api(path, opts = {}) {
  const bridge = typeof window !== 'undefined' ? window.hermesDesktop?.api : null
  if (!bridge) return Promise.reject(new Error('Hermes desktop bridge unavailable'))
  const connectionId = opts.connectionId || activeConnection()
  return bridge({ path, timeoutMs: 15_000, ...opts, ...(connectionId ? { connectionId } : {}) })
}

const boardQs = board => (board && board !== 'default' ? `?board=${encodeURIComponent(board)}` : '')
const taskPath = (id, board, sub = '') => `/api/plugins/kanban/tasks/${encodeURIComponent(id)}${sub}${boardQs(board)}`

/** Done → unblock, optionally posting `note` first. `call` is injectable for tests. */
export async function unblockCard(item, note = '', call = api) {
  const body = str(note).trim()
  if (body) {
    await call(taskPath(item.id, item.board, '/comments'), {
      method: 'POST',
      body: { body, author: 'user' }
    })
  }
  return call(taskPath(item.id, item.board), { method: 'PATCH', body: { status: 'ready' } })
}

/** Open the card's drawer: core hook → task-mentions popup → Kanban page. */
export function openCard(item, win = typeof window !== 'undefined' ? window : {}, nav = p => host.navigate?.(p)) {
  if (typeof win.__hermesKanbanOpenTask === 'function' && win.__hermesKanbanOpenTask(item.id, item.board)) return 'drawer'
  if (typeof win.__taskMentionsOpen === 'function') {
    win.__taskMentionsOpen(item.id)
    return 'popup'
  }
  nav('/kanban')
  return 'page'
}

const lineageCache = new Map()

async function lineageIds(storedId) {
  const conn = activeConnection() || ''
  const key = `${conn}\u0000${storedId}`
  const hit = lineageCache.get(key)
  if (hit && Date.now() - hit.at < LINEAGE_TTL_MS) return hit.ids
  const ids = new Set([storedId])
  let cur = storedId
  for (let hop = 0; hop < 12 && cur; hop++) {
    try {
      const row = await api(`/api/sessions/${encodeURIComponent(cur)}`, { timeoutMs: 5_000 })
      if (row?.id) ids.add(str(row.id))
      cur = str(row?.parent_session_id)
      if (cur) ids.add(cur)
    } catch {
      break
    }
  }
  lineageCache.set(key, { at: Date.now(), ids })
  return ids
}

function readParked() {
  try {
    return parseParkedIds(window.localStorage.getItem(PARKED_KEY))
  } catch {
    return []
  }
}

// One shared poller for every mounted composer.
let snapshot = { stored: null, items: [], parked: false }
const subs = new Set()
const details = new Map() // detailKey → { at, data }
let timer = null
let running = false
let rerun = false
const rewrites = new Map() // rewriteKey → { status: 'pending'|'done'|'failed', steps, at }
let backend = { ok: null, at: 0 } // blocked-panel agent plugin present?
const REWRITE_RETRY_MS = 5 * 60_000

const withRewrites = items =>
  items.map(it => {
    const r = rewrites.get(rewriteKey(it))
    return { ...it, rewritten: r?.status === 'done' ? r.steps : null, rewriting: r?.status === 'pending' }
  })

async function backendReady() {
  if (backend.ok !== null && Date.now() - backend.at < REWRITE_RETRY_MS) return backend.ok
  try {
    const r = await api('/api/plugins/blocked-panel/ping', { timeoutMs: 5_000 })
    backend = { ok: Boolean(r?.ok), at: Date.now() }
  } catch {
    backend = { ok: false, at: Date.now() }
  }
  return backend.ok
}

/** Ask the backend (aux model, disk-cached) to rewrite each card's note as steps; re-emit as they land. */
async function requestRewrites(items, detailMap) {
  const todo = items.filter(it => {
    if (!it.loaded || !it.text) return false
    const r = rewrites.get(rewriteKey(it))
    return !r || (r.status === 'failed' && Date.now() - r.at > REWRITE_RETRY_MS)
  })
  if (!todo.length || !(await backendReady())) return
  for (const it of todo) rewrites.set(rewriteKey(it), { status: 'pending', at: Date.now() })
  snapshot = { ...snapshot, items: withRewrites(snapshot.items) }
  emit()
  for (const it of todo) {
    const k = rewriteKey(it)
    const d = detailMap.get(`${it.board}:${it.id}`)
    const comments = (Array.isArray(d?.comments) ? d.comments : []).slice(-3).map(c => str(c?.body)).filter(Boolean)
    api('/api/plugins/blocked-panel/steps', {
      method: 'POST',
      timeoutMs: 60_000,
      body: { title: it.title, reason: it.fromBody ? '' : it.text, body: str(d?.task?.body), comments }
    })
      .then(r => {
        const steps = Array.isArray(r?.steps) ? r.steps.map(str).filter(Boolean) : []
        rewrites.set(k, steps.length ? { status: 'done', steps, at: Date.now() } : { status: 'failed', at: Date.now() })
      })
      .catch(e => {
        log('rewrite failed', it.id, e?.message || e)
        rewrites.set(k, { status: 'failed', at: Date.now() })
      })
      .finally(() => {
        if (snapshot.items.some(x => rewriteKey(x) === k)) {
          snapshot = { ...snapshot, items: withRewrites(snapshot.items) }
          emit()
        }
      })
  }
}

const emit = () => subs.forEach(fn => fn(snapshot))

async function loadDetails(tasks) {
  const now = Date.now()
  await Promise.all(
    tasks
      .filter(t => str(t.status) === 'blocked')
      .map(async t => {
        const k = detailKey(t)
        const hit = details.get(k)
        if (hit && now - hit.at < DETAIL_TTL_MS) return
        try {
          const data = await api(taskPath(t.id, t.board), { timeoutMs: 8_000 })
          details.set(k, { at: now, data })
        } catch (e) {
          log('detail failed', t.id, e?.message || e)
        }
      })
  )
}

export async function refresh(force = false) {
  if (running) {
    rerun = true
    return
  }
  if (typeof document !== 'undefined' && document.hidden && !force) return
  running = true
  try {
    const stored = host.state.focusedStoredSessionId.get()
    if (!stored) {
      snapshot = { stored, items: [], parked: false }
      return emit()
    }
    if (force) details.clear()
    const ids = await lineageIds(stored)
    const list = await api(`/api/session-kanban-tasks?ids=${encodeURIComponent([...ids].join(','))}`).catch(() => null)
    const tasks = Array.isArray(list?.tasks) ? list.tasks : null
    if (tasks) await loadDetails(tasks)
    if (host.state.focusedStoredSessionId.get() !== stored) return
    const flat = new Map([...details].map(([k, v]) => [k, v.data]))
    const items = tasks ? blockedItems(tasks, flat) : snapshot.stored === stored ? snapshot.items : []
    snapshot = {
      stored,
      // fetch failed: keep this chat's last list, never another chat's
      items: withRewrites(items),
      parked: isParked(ids, readParked())
    }
    emit()
    void requestRewrites(items, flat)
  } catch (e) {
    log('refresh failed', e)
  } finally {
    running = false
    if (rerun) {
      rerun = false
      void refresh()
    }
  }
}

function subscribe(fn) {
  subs.add(fn)
  if (!timer) {
    timer = setInterval(refresh, POLL_MS)
    void refresh()
  }
  return () => {
    subs.delete(fn)
    if (!subs.size && timer) {
      clearInterval(timer)
      timer = null
    }
  }
}

// ── UI ──────────────────────────────────────────────────────────────────────

const AMBER = 'var(--warning, #d29922)'
const btn = (primary = false) => ({
  padding: '2px 10px',
  borderRadius: 6,
  border: `1px solid ${primary ? AMBER : 'color-mix(in srgb, var(--border, #8884) 90%, transparent)'}`,
  background: primary ? `color-mix(in srgb, ${AMBER} 22%, transparent)` : 'transparent',
  color: 'var(--foreground, inherit)',
  fontSize: 11.5,
  lineHeight: '18px',
  cursor: 'pointer',
  fontFamily: 'inherit'
})

function notify(kind, message) {
  try {
    host.notify?.({ kind, message })
  } catch {
    /* older hosts */
  }
}

let osApi = null // ctx.os, set in register()

async function openLink(value) {
  try {
    if (await osApi?.openExternal?.(value)) return
  } catch {
    /* fall through */
  }
  notify('error', `Couldn't open ${value}`)
}

/** Text with its URLs and local paths as clickable links (paths open with the OS handler). */
function Linked({ text }) {
  const segs = linkSegments(text)
  if (!segs.some(x => x.kind !== 'text')) return str(text)
  return segs.map((x, i) =>
    x.kind === 'text'
      ? x.value
      : jsx('a', {
          href: linkHref(x),
          'data-blocked-link': x.kind,
          title: x.kind === 'url' ? linkHref(x) : `Open ${x.value}`,
          onClick: e => {
            e.preventDefault()
            e.stopPropagation()
            void openLink(x.kind === 'url' ? linkHref(x) : x.value)
          },
          style: { color: 'var(--accent-foreground, var(--primary, #58a6ff))', textDecoration: 'underline', textUnderlineOffset: 2, cursor: 'pointer', wordBreak: 'break-all' },
          children: x.value
        }, i)
  )
}

function Item({ item }) {
  const [open, setOpen] = useState(false)
  const [replying, setReplying] = useState(false)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [showNote, setShowNote] = useState(false)
  const list = checklist(item, item.rewritten)
  const long = list.steps.length > COLLAPSED_LINES + 1

  const act = async withNote => {
    setBusy(true)
    try {
      await unblockCard(item, withNote ? note : '')
      notify('success', `Unblocked ${item.id}${withNote && note.trim() ? ' with your note' : ''}`)
      setReplying(false)
      setNote('')
      void refresh(true)
    } catch (e) {
      notify('error', `Couldn't unblock ${item.id}: ${e?.message || e}`)
    } finally {
      setBusy(false)
    }
  }

  const steps = open || !long ? list.steps : list.steps.slice(0, COLLAPSED_LINES)

  return jsxs('div', {
    'data-blocked-card': item.id,
    style: { display: 'flex', flexDirection: 'column', gap: 4, padding: '6px 0', borderTop: '1px solid color-mix(in srgb, var(--border, #8884) 60%, transparent)' },
    children: [
      jsxs('div', {
        style: { display: 'flex', alignItems: 'baseline', gap: 6, minWidth: 0 },
        children: [
          jsx('span', { style: { fontFamily: 'var(--font-mono, ui-monospace, monospace)', fontSize: 10.5, opacity: 0.65, flex: 'none' }, children: item.id }),
          jsx('span', { title: item.title, style: { fontWeight: 600, fontSize: 12.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }, children: item.title }),
          jsx('span', { style: { marginLeft: 'auto', flex: 'none', fontSize: 10.5, color: AMBER }, children: item.kindLabel })
        ]
      }),
      jsxs('div', {
        style: { fontSize: 12.5, lineHeight: 1.45, color: 'var(--foreground, inherit)', userSelect: 'text' },
        children: [
          !item.loaded && jsx('span', { style: { opacity: 0.6 }, children: 'Loading instructions…' }),
          item.loaded &&
            jsx('div', {
              style: { fontSize: 10.5, opacity: 0.6, marginBottom: 2 },
              children: item.rewriting && list.source !== 'rewritten'
                ? 'Writing clearer steps…'
                : !item.text
                  ? 'No instructions were recorded.'
                  : item.fromBody
                    ? 'No block reason recorded; from the card description. To unblock:'
                    : 'To unblock:'
            }),
          item.loaded && list.intro && jsx('div', { style: { whiteSpace: 'pre-wrap' }, children: jsx(Linked, { text: list.intro }) }),
          item.loaded &&
            jsx('ol', {
              'data-blocked-steps': list.source,
              start: list.start,
              style: { margin: '2px 0 0', paddingLeft: 20, display: 'flex', flexDirection: 'column', gap: 3 },
              children: steps.map((s, i) => jsx('li', { style: { paddingLeft: 2 }, children: jsx(Linked, { text: s }) }, i))
            }),
          showNote &&
            jsx('div', {
              style: { marginTop: 4, padding: '4px 8px', borderLeft: `2px solid color-mix(in srgb, ${AMBER} 50%, transparent)`, fontSize: 11.5, opacity: 0.8, whiteSpace: 'pre-wrap' },
              children: jsx(Linked, { text: item.text })
            })
        ]
      }),
      jsxs('div', {
        style: { display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' },
        children: [
          long && jsx('button', { type: 'button', style: { ...btn(), border: 'none', padding: '2px 4px', opacity: 0.75 }, onClick: () => setOpen(!open), children: open ? 'Show less' : `Show all ${list.steps.length} steps` }),
          item.text && list.source !== 'parsed' && jsx('button', { type: 'button', style: { ...btn(), border: 'none', padding: '2px 4px', opacity: 0.75 }, onClick: () => setShowNote(!showNote), children: showNote ? 'Hide agent\u2019s note' : 'Agent\u2019s note' }),
          jsx('span', { style: { flex: 1 } }),
          jsx('button', { type: 'button', style: btn(), onClick: () => openCard(item), children: 'Open card' }),
          jsx('button', { type: 'button', style: btn(), disabled: busy, onClick: () => setReplying(!replying), children: 'Reply & unblock' }),
          jsx('button', { type: 'button', style: btn(true), disabled: busy, onClick: () => act(false), title: 'I did it — move the card back to ready so the worker resumes', children: busy ? 'Unblocking…' : 'Done → unblock' })
        ]
      }),
      replying &&
        jsxs('div', {
          style: { display: 'flex', gap: 6, alignItems: 'flex-end' },
          children: [
            jsx('textarea', {
              value: note,
              autoFocus: true,
              rows: 2,
              placeholder: 'Answer or note for the worker (posted as a card comment)…',
              onChange: e => setNote(e.target.value),
              onKeyDown: e => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && note.trim()) {
                  e.preventDefault()
                  void act(true)
                } else if (e.key === 'Escape') setReplying(false)
                e.stopPropagation()
              },
              style: {
                flex: 1,
                resize: 'vertical',
                fontSize: 12.5,
                fontFamily: 'inherit',
                padding: '4px 6px',
                borderRadius: 6,
                border: '1px solid var(--border, #8884)',
                background: 'var(--card, transparent)',
                color: 'var(--foreground, inherit)'
              }
            }),
            jsx('button', { type: 'button', style: btn(true), disabled: busy || !note.trim(), onClick: () => act(true), title: '⌘↩', children: 'Send & unblock' })
          ]
        })
    ]
  })
}

function Panel() {
  const [snap, setSnap] = useState(snapshot)
  const [collapsed, setCollapsed] = useState(false)
  const focused = useValue(host.state.focusedStoredSessionId)
  useEffect(() => subscribe(setSnap), [])
  useEffect(() => void refresh(), [focused])
  useEffect(() => {
    // ⌘B writes localStorage in this window; re-read on the next tick.
    const onKey = e => (e.metaKey || e.ctrlKey) && str(e.key).toLowerCase() === 'b' && setTimeout(() => void refresh(), 50)
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [])

  if (snap.stored !== focused) return null
  // user-steps' "For you" strip lists these cards (with the same Done → unblock / Reply & unblock buttons);
  // this panel only renders when that plugin is absent.
  if (typeof window !== 'undefined' && window.__userSteps?.ownsComposer) return null
  const { items, parked } = snap
  if (!items.length && !parked) return null

  const header = items.length
    ? `Blocked: waiting on you${items.length > 1 ? ` (${items.length} cards)` : ''}`
    : 'Blocked: parked by you'

  return jsxs('div', {
    'data-slot': 'blocked-panel',
    role: 'status',
    style: {
      width: '100%',
      boxSizing: 'border-box',
      marginBottom: 6,
      padding: '6px 10px',
      borderRadius: 8,
      border: `1px solid color-mix(in srgb, ${AMBER} 55%, transparent)`,
      background: `color-mix(in srgb, ${AMBER} 9%, var(--card, transparent))`,
      color: 'var(--foreground, inherit)',
      maxHeight: '45vh',
      overflowY: 'auto'
    },
    children: [
      jsxs('div', {
        style: { display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, paddingBottom: items.length && !collapsed ? 4 : 0 },
        children: [
          jsx('i', { 'aria-hidden': true, className: 'codicon codicon-debug-pause', style: { color: AMBER, fontSize: 13 } }),
          jsx('span', { style: { fontWeight: 600 }, children: header }),
          !items.length && jsx('span', { style: { opacity: 0.6, fontSize: 11 }, children: 'To unblock:' }),
          parked && items.length > 0 && jsx('span', { style: { opacity: 0.6, fontSize: 11 }, children: '· also parked with ⌘B' }),
          items.length > 0 &&
            jsx('button', {
              type: 'button',
              style: { ...btn(), marginLeft: 'auto', border: 'none', padding: '0 4px' },
              onClick: () => setCollapsed(!collapsed),
              'aria-label': collapsed ? 'Expand' : 'Collapse',
              children: jsx('i', { className: `codicon codicon-chevron-${collapsed ? 'down' : 'up'}` })
            })
        ]
      }),
      !items.length &&
        jsx('ol', {
          'data-blocked-steps': 'parked',
          style: { margin: '2px 0 0', paddingLeft: 20, fontSize: 12.5, lineHeight: 1.45, display: 'flex', flexDirection: 'column', gap: 3 },
          children: PARKED_STEPS.map((s, i) => jsx('li', { children: jsx(Linked, { text: s }) }, i))
        }),
      !collapsed && items.map(item => jsx(Item, { item }, item.id))
    ]
  })
}

export default {
  id: ID,
  name: 'Blocked Panel',
  register(ctx) {
    osApi = ctx.os || null
    ctx.register({ id: 'panel', area: COMPOSER_AREAS.top, order: -20, render: () => jsx(Panel, {}) })
    const onVis = () => document.hidden || void refresh()
    document.addEventListener('visibilitychange', onVis)
    // version 4: subscribe/unblockCard/openCard for user-steps' "For you" strip (it reuses this poller + actions)
    window.__blockedPanel = { version: 4, get snapshot() { return snapshot }, refresh, checklist, subscribe, unblockCard, openCard }
    return () => {
      document.removeEventListener('visibilitychange', onVis)
      delete window.__blockedPanel
      if (timer) clearInterval(timer)
      timer = null
    }
  }
}

