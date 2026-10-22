/**
 * User Steps — what this chat is waiting for YOU to do, with one-click answers.
 *
 * Two surfaces, one ledger (the `user-steps` agent plugin, REST /api/plugins/user-steps):
 *   - `::user-step{id="us_…[,us_…]" s="<session>" t="<paragraph text>"}` directive: the agent plugin replaces the
 *     paragraph of a reply that asks you something with this leaf. It renders as an amber-framed box with the
 *     original text and the step's buttons, fetched from GET /step by id. Ledger unreachable → the `t` text as a
 *     plain paragraph (nothing is lost).
 *   - "For you" strip (COMPOSER_AREAS.top): the focused chat's open ledger steps, oldest first, plus its blocked
 *     human Kanban cards (blocked-panel's data via window.__blockedPanel; the chat's own rolling user-steps card
 *     is left out, its steps are already listed). Hidden when empty.
 * Buttons by kind: approve [Do it][No] · do [Done][Can't] · choose [one per option, recommended first]
 * · input [Reply…] (prefills the composer). A click sends a short VISIBLE user turn ("Go: …", "✓ Done: …",
 * "Chose: …") through host.composer.submit and, once sent, POSTs /resolve (which archives the chat's rolling
 * blocked card after the last step). Kanban cards keep blocked-panel's Done → unblock / Reply & unblock.
 */
import { COMPOSER_AREAS, TRANSCRIPT_DIRECTIVE_AREA, host, useValue } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'user-steps'
const VERSION = 4
// The composer chrome is user-select:none app-wide; the strip and the in-message box must stay copyable.
const SELECTABLE = { userSelect: 'text', WebkitUserSelect: 'text', cursor: 'auto' }
const POLL_MS = 8_000
const BASE = '/api/plugins/user-steps'

const str = v => (typeof v === 'string' ? v : v == null ? '' : String(v))
const log = (...a) => console.info(`[${ID}]`, ...a)

// ── pure helpers (exported for tests) ───────────────────────────────────────

// Same link rules as blocked-panel (kept in sync by both test files).
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

/** Inverse of the agent plugin's core.enc (attr-safe escapes). */
export function decodeAttr(s) {
  const map = { '\\': '\\', n: '\n', q: '"', '(': '{', ')': '}' }
  let out = ''
  const t = str(s)
  for (let i = 0; i < t.length; i++) {
    if (t[i] === '\\' && i + 1 < t.length && map[t[i + 1]] !== undefined) {
      out += map[t[i + 1]]
      i++
    } else out += t[i]
  }
  return out
}

export const directiveIds = attrs => str(attrs?.id).split(',').map(x => x.trim()).filter(x => /^us_[0-9a-f]{6,32}$/.test(x))

const short = (s, n = 140) => {
  const t = str(s).replace(/\s+/g, ' ').trim()
  return t.length > n ? `${t.slice(0, n - 1)}…` : t
}

/** The buttons a step offers. kind 'submit' sends `text` as a visible user turn then resolves with `resolution`;
 *  kind 'draft' prefills the composer and leaves the step open (the reply resolves it). */
export function actionsFor(step) {
  const t = short(step?.text)
  switch (step?.kind) {
    case 'approve':
      return [
        { label: 'Do it', primary: true, kind: 'submit', text: `Go: ${t}`, resolution: 'Go' },
        { label: 'No', kind: 'submit', text: `No: ${t}`, resolution: 'No' }
      ]
    case 'do':
      return [
        { label: 'Done', primary: true, kind: 'submit', text: `\u2713 Done: ${t}`, resolution: 'Done' },
        { label: 'Can\u2019t', kind: 'submit', text: `\u2717 Can\u2019t: ${t}`, resolution: 'Can\u2019t' }
      ]
    case 'choose': {
      const opts = (Array.isArray(step.options) ? step.options : []).map(str).filter(Boolean)
      if (opts.length >= 2)
        return opts.map((o, i) => ({ label: o, primary: i === 0, kind: 'submit', text: `Chose: ${o} \u2014 ${t}`, resolution: `Chose: ${o}` }))
      return [{ label: 'Reply\u2026', primary: true, kind: 'draft', text: `Re: ${t} \u2014 ` }]
    }
    default:
      return [{ label: 'Reply\u2026', primary: true, kind: 'draft', text: `Re: ${t} \u2014 ` }]
  }
}

/** Run one action. Returns 'sent' | 'drafted' | 'no-composer'. Resolve only after the turn was handed to a
 *  visible composer, so a failed click never closes a step silently. Deps are injectable for tests. */
export async function performAction(step, action, sessionId, deps = {}) {
  const composer = deps.composer || host.composer
  const call = deps.call || api
  if (action.kind === 'draft') {
    const ok = await composer?.setDraft?.(sessionId, action.text)
    composer?.focus?.(sessionId)
    return ok === false ? 'no-composer' : 'drafted'
  }
  const ok = composer?.submit?.(sessionId, action.text)
  if (!ok) return 'no-composer'
  await call(`${BASE}/resolve`, { method: 'POST', body: { id: step.id, session: str(sessionId), resolution: action.resolution, by: 'desktop' } })
  return 'sent'
}

/** Ledger steps + blocked-panel Kanban items → strip rows, ledger steps oldest first, then cards oldest first.
 *  The chat's own rolling card is dropped: its steps are already listed. */
export function projectStrip(ledger, kanbanItems) {
  // the chat's own rolling cards (current + already archived: blocked-panel's snapshot can lag the archive)
  const own = new Set([str(ledger?.card?.id), ...(Array.isArray(ledger?.own_cards) ? ledger.own_cards.map(str) : [])].filter(Boolean))
  const steps = (Array.isArray(ledger?.steps) ? ledger.steps : [])
    .filter(s => s?.id && (s.status || 'open') === 'open')
    .slice()
    .sort((a, b) => (Number(a.created) || 0) - (Number(b.created) || 0))
    .map(s => ({ type: 'step', key: `s:${s.id}`, step: s, actions: actionsFor(s) }))
  const cards = (Array.isArray(kanbanItems) ? kanbanItems : [])
    .filter(it => it?.id && !own.has(str(it.id)))
    .slice()
    .sort((a, b) => (Number(a.at) || 0) - (Number(b.at) || 0))
    .map(it => ({ type: 'card', key: `c:${it.board || 'default'}:${it.id}`, item: it }))
  return [...steps, ...cards]
}

/** First line of what a blocked card wants (blocked-panel's checklist when available). */
export function cardSummary(item, checklist) {
  try {
    const list = typeof checklist === 'function' ? checklist(item, item.rewritten) : null
    if (list?.steps?.length) return short(list.steps[0], 160)
  } catch {
    /* fall through */
  }
  return short(item?.text || item?.title, 160)
}

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
  return bridge({ path, timeoutMs: 10_000, ...opts, ...(connectionId ? { connectionId } : {}) })
}

const focusedId = () => {
  try {
    return host.state.focusedStoredSessionId.get() || null
  } catch {
    return null
  }
}

let snapshot = { stored: null, ledger: { steps: [], card: null }, kanban: [], ok: null }
const subs = new Set()
let timer = null
let running = false
let rerun = false
let unsubKanban = null
const stepCache = new Map() // id → { at, step }

const emit = () => subs.forEach(fn => fn(snapshot))

function blockedPanel() {
  return typeof window !== 'undefined' ? window.__blockedPanel : null
}

function attachKanban() {
  if (unsubKanban) return
  const bp = blockedPanel()
  if (typeof bp?.subscribe !== 'function') return
  unsubKanban = bp.subscribe(s => {
    snapshot = { ...snapshot, kanban: s?.stored === snapshot.stored ? s.items || [] : [] }
    emit()
  })
}

export async function refresh(call = api) {
  if (running) {
    rerun = true
    return
  }
  running = true
  try {
    attachKanban()
    const stored = focusedId()
    if (!stored) {
      snapshot = { ...snapshot, stored, ledger: { steps: [], card: null } }
      return emit()
    }
    let ledger = snapshot.stored === stored ? snapshot.ledger : { steps: [], card: null }
    let ok = snapshot.ok
    try {
      const r = await call(`${BASE}/steps?session=${encodeURIComponent(stored)}`, { timeoutMs: 6_000 })
      ledger = { steps: Array.isArray(r?.steps) ? r.steps : [], card: r?.card || null, own_cards: Array.isArray(r?.own_cards) ? r.own_cards : [] }
      ok = true
      const open = new Set(ledger.steps.map(s => s.id))
      for (const [sid, hit] of stepCache) if ((hit.step?.status || 'open') === 'open' && !open.has(sid) && hit.step?.session && snapshot.ledger.steps.some(x => x.id === sid)) stepCache.delete(sid)
      for (const s of ledger.steps) stepCache.set(s.id, { at: Date.now(), step: s })
    } catch (e) {
      ok = false
      log('steps fetch failed', e?.message || e)
    }
    if (focusedId() !== stored) {
      rerun = true
      return
    }
    const bp = blockedPanel()?.snapshot
    snapshot = { stored, ledger, kanban: bp?.stored === stored ? bp.items || [] : snapshot.stored === stored ? snapshot.kanban : [], ok }
    emit()
  } finally {
    running = false
    if (rerun) {
      rerun = false
      void refresh(call)
    }
  }
}

export const getSnapshot = () => snapshot

function subscribe(fn) {
  subs.add(fn)
  if (!timer) {
    timer = setInterval(() => (typeof document !== 'undefined' && document.hidden) || void refresh(), POLL_MS)
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

/** GET /step for a directive id (cached 5 s so a re-render doesn't refetch). `call` injectable for tests. */
export async function fetchStep(id, session, call = api) {
  const hit = stepCache.get(id)
  if (hit && Date.now() - hit.at < 5_000) return hit.step
  const r = await call(`${BASE}/step?id=${encodeURIComponent(id)}&s=${encodeURIComponent(str(session))}`, { timeoutMs: 6_000 })
  const step = r?.step || null
  if (step) stepCache.set(id, { at: Date.now(), step })
  return step
}

export const clearStepCache = () => stepCache.clear()

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
const KIND_ICON = { approve: 'codicon-question', do: 'codicon-checklist', choose: 'codicon-list-selection', input: 'codicon-comment' }

let osApi = null // ctx.os, set in register()

async function openLink(value) {
  try {
    if (await osApi?.openExternal?.(value)) return
  } catch {
    /* fall through */
  }
  try {
    if (typeof window !== 'undefined' && window.open?.(value, '_blank')) return
  } catch {
    /* fall through */
  }
  notify('error', `Couldn\u2019t open ${value}`)
}

/** Text with its URLs and local paths as clickable links (paths open with the OS handler). */
export function Linked({ text }) {
  const segs = linkSegments(text)
  if (!segs.some(x => x.kind !== 'text')) return str(text)
  return segs.map((x, i) =>
    x.kind === 'text'
      ? x.value
      : jsx('a', {
          href: linkHref(x),
          'data-user-steps-link': x.kind,
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

function notify(kind, message) {
  try {
    host.notify?.({ kind, message })
  } catch {
    /* older hosts */
  }
}

function StepButtons({ step, deps, onDone }) {
  const [busy, setBusy] = useState(false)
  const click = async action => {
    if (busy) return
    setBusy(true)
    try {
      const sid = deps?.sessionId || focusedId() || step.session
      const r = await performAction(step, action, sid, deps)
      if (r === 'no-composer') notify('error', 'Open this chat\u2019s composer to answer the step.')
      else if (r === 'sent') {
        stepCache.set(step.id, { at: Date.now(), step: { ...step, status: 'resolved', resolution: action.resolution } })
        onDone?.(action)
        void refresh(deps?.call)
        void blockedPanel()?.refresh?.(true) // the rolling card may just have been archived
      }
    } catch (e) {
      notify('error', `Couldn\u2019t resolve the step: ${e?.message || e}`)
    } finally {
      setBusy(false)
    }
  }
  return jsx('div', {
    style: { display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' },
    children: actionsFor(step).map((a, i) =>
      jsx('button', { type: 'button', 'data-user-step-action': a.label, style: btn(a.primary), disabled: busy, onClick: () => click(a), children: a.label }, i)
    )
  })
}

/** The in-message framed box for `::user-step{...}`. */
export function StepBox({ attrs, streaming, deps }) {
  const ids = directiveIds(attrs)
  const fallback = decodeAttr(attrs?.t)
  const [state, setState] = useState({ loading: true, steps: [], failed: false })
  const [tick, setTick] = useState(0)
  const key = ids.join(',')
  // Re-read when the shared snapshot changes: a click in the strip (or the model resolving the step) must flip
  // this box too. Listening only; the poll timer belongs to the strip.
  useEffect(() => {
    const fn = () => setTick(t => t + 1)
    subs.add(fn)
    return () => subs.delete(fn)
  }, [])
  useEffect(() => {
    let live = true
    Promise.all(ids.map(id => fetchStep(id, attrs?.s, deps?.call)))
      .then(steps => {
        if (!live) return
        const got = steps.filter(Boolean)
        // keep a known step when a refetch fails transiently; never re-open one we saw resolved
        setState(st => (got.length || !st.steps.length ? { loading: false, steps: got, failed: steps.every(s => !s) && !st.steps.length } : { ...st, loading: false }))
      })
      .catch(() => live && setState(st => (st.steps.length ? { ...st, loading: false } : { loading: false, steps: [], failed: true })))
    return () => {
      live = false
    }
  }, [key, tick])
  if (!ids.length || state.failed) {
    // ledger unreachable / unknown ids: the paragraph as plain text (empty t = the text is already above)
    return fallback ? jsx('div', { 'data-user-step-fallback': '', style: { whiteSpace: 'pre-wrap' }, children: jsx(Linked, { text: fallback }) }) : null
  }
  const text = fallback || state.steps.map(s => s.text).join('\n')
  return jsxs('div', {
    'data-user-step': key,
    style: {
      ...SELECTABLE,
      margin: '6px 0',
      padding: '8px 10px',
      borderRadius: 8,
      border: `1px solid color-mix(in srgb, ${AMBER} 60%, transparent)`,
      background: `color-mix(in srgb, ${AMBER} 8%, transparent)`,
      display: 'flex',
      flexDirection: 'column',
      gap: 6
    },
    children: [
      jsxs('div', {
        key: 'h',
        style: { display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: AMBER, fontWeight: 600 },
        children: [jsx('i', { 'aria-hidden': true, className: 'codicon codicon-account' }), 'For you']
      }),
      jsx('div', { key: 't', 'data-user-step-text': '', style: { whiteSpace: 'pre-wrap', lineHeight: 1.5 }, children: jsx(Linked, { text }) }),
      state.loading && jsx('div', { key: 'l', style: { fontSize: 11, opacity: 0.6 }, children: 'Loading\u2026' }),
      ...state.steps.map(s =>
        (s.status || 'open') === 'open'
          ? jsxs('div', {
              style: { display: 'flex', flexDirection: 'column', gap: 4 },
              children: [
                state.steps.length > 1 && jsx('div', { style: { fontSize: 12, opacity: 0.85 }, children: jsx(Linked, { text: s.text }) }),
                !streaming && jsx(StepButtons, { step: s, deps, onDone: a => setState(st => ({ ...st, steps: st.steps.map(x => (x.id === s.id ? { ...x, status: 'resolved', resolution: a.resolution } : x)) })) })
              ]
            }, s.id)
          : jsx('div', { 'data-user-step-resolved': s.id, style: { fontSize: 11.5, opacity: 0.65 }, children: `\u2713 ${s.resolution || 'Resolved'}${state.steps.length > 1 ? ` \u2014 ${s.text}` : ''}` }, s.id)
      )
    ]
  })
}

const rowStyle = { display: 'flex', flexDirection: 'column', gap: 4, padding: '6px 0', borderTop: '1px solid color-mix(in srgb, var(--border, #8884) 60%, transparent)' }

function CardRow({ item }) {
  const bp = blockedPanel()
  const [replying, setReplying] = useState(false)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const unblock = async withNote => {
    if (typeof bp?.unblockCard !== 'function') return notify('error', 'blocked-panel is not loaded')
    setBusy(true)
    try {
      await bp.unblockCard(item, withNote ? note : '')
      notify('success', `Unblocked ${item.id}`)
      setReplying(false)
      setNote('')
      void bp.refresh?.(true)
    } catch (e) {
      notify('error', `Couldn\u2019t unblock ${item.id}: ${e?.message || e}`)
    } finally {
      setBusy(false)
    }
  }
  return jsxs('div', {
    'data-user-steps-card': item.id,
    style: rowStyle,
    children: [
      jsxs('div', {
        style: { display: 'flex', gap: 6, alignItems: 'baseline', minWidth: 0 },
        children: [
          jsx('i', { 'aria-hidden': true, className: 'codicon codicon-debug-pause', style: { color: AMBER, flex: 'none' } }),
          jsx('span', { style: { fontWeight: 600, fontSize: 12.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }, title: item.title, children: item.title }),
          jsx('span', { style: { marginLeft: 'auto', flex: 'none', fontSize: 10.5, fontFamily: 'var(--font-mono, ui-monospace, monospace)', opacity: 0.6 }, children: item.id })
        ]
      }),
      jsx('div', { style: { fontSize: 12.5, lineHeight: 1.45 }, children: jsx(Linked, { text: cardSummary(item, bp?.checklist) }) }),
      jsxs('div', {
        style: { display: 'flex', flexWrap: 'wrap', gap: 6 },
        children: [
          jsx('button', { type: 'button', style: btn(true), disabled: busy, onClick: () => unblock(false), children: busy ? 'Unblocking\u2026' : 'Done \u2192 unblock' }),
          jsx('button', { type: 'button', style: btn(), disabled: busy, onClick: () => setReplying(!replying), children: 'Reply & unblock' }),
          typeof bp?.openCard === 'function' && jsx('button', { type: 'button', style: btn(), onClick: () => bp.openCard(item), children: 'Open card' })
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
              placeholder: 'Answer for the worker (posted as a card comment)\u2026',
              onChange: e => setNote(e.target.value),
              onKeyDown: e => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && note.trim()) {
                  e.preventDefault()
                  void unblock(true)
                }
                e.stopPropagation()
              },
              style: { flex: 1, fontSize: 12.5, fontFamily: 'inherit', padding: '4px 6px', borderRadius: 6, border: '1px solid var(--border, #8884)', background: 'var(--card, transparent)', color: 'var(--foreground, inherit)' }
            }),
            jsx('button', { type: 'button', style: btn(true), disabled: busy || !note.trim(), onClick: () => unblock(true), children: 'Send & unblock' })
          ]
        })
    ]
  })
}

function StepRow({ step, sessionId }) {
  return jsxs('div', {
    'data-user-steps-step': step.id,
    style: rowStyle,
    children: [
      jsxs('div', {
        style: { display: 'flex', gap: 6, alignItems: 'baseline' },
        children: [
          jsx('i', { 'aria-hidden': true, className: `codicon ${KIND_ICON[step.kind] || 'codicon-comment'}`, style: { color: AMBER, flex: 'none' } }),
          jsx('span', { style: { fontSize: 12.5, lineHeight: 1.45 }, children: jsx(Linked, { text: step.text }) })
        ]
      }),
      jsx(StepButtons, { step, deps: { sessionId } })
    ]
  })
}

export function Strip() {
  const [snap, setSnap] = useState(snapshot)
  const [collapsed, setCollapsed] = useState(false)
  const focused = useValue(host.state.focusedStoredSessionId)
  useEffect(() => subscribe(setSnap), [])
  useEffect(() => void refresh(), [focused])
  if (!focused || snap.stored !== focused) return null
  const rows = projectStrip(snap.ledger, snap.kanban)
  if (!rows.length) return null
  return jsxs('div', {
    'data-slot': 'user-steps',
    role: 'status',
    style: {
      ...SELECTABLE,
      width: '100%',
      boxSizing: 'border-box',
      marginBottom: 6,
      padding: '6px 10px',
      borderRadius: 8,
      border: `1px solid color-mix(in srgb, ${AMBER} 55%, transparent)`,
      background: `color-mix(in srgb, ${AMBER} 9%, var(--card, transparent))`,
      color: 'var(--foreground, inherit)',
      maxHeight: '40vh',
      overflowY: 'auto'
    },
    children: [
      jsxs('div', {
        key: 'h',
        style: { display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, paddingBottom: collapsed ? 0 : 4 },
        children: [
          jsx('i', { 'aria-hidden': true, className: 'codicon codicon-account', style: { color: AMBER } }),
          jsx('span', { style: { fontWeight: 600 }, children: `For you${rows.length > 1 ? ` (${rows.length})` : ''}` }),
          jsx('button', {
            type: 'button',
            style: { ...btn(), marginLeft: 'auto', border: 'none', padding: '0 4px' },
            onClick: () => setCollapsed(!collapsed),
            'aria-label': collapsed ? 'Expand' : 'Collapse',
            children: jsx('i', { className: `codicon codicon-chevron-${collapsed ? 'down' : 'up'}` })
          })
        ]
      }),
      ...(collapsed ? [] : rows.map(r => (r.type === 'step' ? jsx(StepRow, { step: r.step, sessionId: focused }, r.key) : jsx(CardRow, { item: r.item }, r.key))))
    ]
  })
}

export default {
  id: ID,
  name: 'User Steps',
  register(ctx) {
    osApi = ctx?.os || null
    ctx.register({ id: 'strip', area: COMPOSER_AREAS.top, order: -25, render: () => jsx(Strip, {}) })
    ctx.register({ id: 'user-step-directive', area: TRANSCRIPT_DIRECTIVE_AREA, data: { name: 'user-step', render: props => jsx(StepBox, props) } })
    const onVis = () => (typeof document !== 'undefined' && document.hidden) || void refresh()
    if (typeof document !== 'undefined') document.addEventListener('visibilitychange', onVis)
    if (typeof window !== 'undefined') window.__userSteps = { version: VERSION, ownsComposer: true, get snapshot() { return snapshot }, refresh }
    return () => {
      if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', onVis)
      if (typeof window !== 'undefined' && window.__userSteps?.version === VERSION) delete window.__userSteps
      unsubKanban?.()
      unsubKanban = null
      if (timer) clearInterval(timer)
      timer = null
    }
  }
}
