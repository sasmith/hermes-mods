/**
 * Session Tasks — a right-aligned strip of compact chips above the composer input,
 * one per thing the focused session is running right now:
 *
 *   ⟳ bg   background processes   (gateway RPC `process.list`, status != exited)
 *   ⟳ sub  delegate_task subagents (gateway RPC `subagent.list`, non-terminal status)
 *   ⟳ t_…  kanban tasks this session created or shepherds (tui subscription),
 *          across every board, in todo / ready / running / scheduled / blocked / review
 *          — the same cards the sidebar row's kanban counts cover
 *          (`/api/session-kanban-tasks?ids=<lineage>`; older backends: a scan of the
 *          current `/api/plugins/kanban/board` for cards the lineage created)
 *   ▣ box  rented machines linked to this session (or an ancestor in its lineage)
 *          by the compute-rentals plugin's local registry
 *          (`/api/plugins/compute-rentals/inventory`, polled every 30 s; absent → no chips)
 *
 * Hidden when nothing is running. Polls every 5 s while the window is visible.
 * Clicking a kanban chip opens the Task Mentions popup when that plugin is
 * loaded, else the Kanban page; a machine chip opens that machine in the
 * Compute Rentals status-bar popover. Hover any chip for the full title.
 */
import { COMPOSER_AREAS, host, useValue } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'session-tasks'
const POLL_MS = 5_000
const KANBAN_POLL_MS = 10_000
const MACHINE_POLL_MS = 30_000
const LINEAGE_TTL_MS = 5 * 60_000
const MAX_LABEL = 38
const KANBAN_ACTIVE = new Set(['todo', 'ready', 'running', 'scheduled', 'blocked', 'review'])
const SUBAGENT_DONE = new Set(['completed', 'failed', 'interrupted', 'cancelled', 'timeout', 'error'])

const STATE_COLOR = {
  running: 'var(--accent, #3b82f6)',
  stopped: '#8b949e',
  unknown: '#8b949e',
  error: '#f85149',
  ready: '#8b949e',
  todo: '#8b949e',
  scheduled: '#e3b341', // waiting on a timer
  blocked: '#f85149',
  review: '#db61a2'
}

// States whose whole pill (border + fill) takes the state colour, not just the dot/label.
const TINTED_STATES = new Set(['scheduled'])

/** Pill border/background for a chip state (exported for tests). */
export function chipFrame(state) {
  const c = TINTED_STATES.has(state) && STATE_COLOR[state]
  return c
    ? { border: `1px solid color-mix(in srgb, ${c} 60%, transparent)`, background: `color-mix(in srgb, ${c} 14%, transparent)` }
    : {
        border: '1px solid color-mix(in srgb, var(--border, #8884) 80%, transparent)',
        background: 'color-mix(in srgb, var(--card, transparent) 60%, transparent)'
      }
}

export const chipColor = state => STATE_COLOR[state] || STATE_COLOR.running

const str = v => (typeof v === 'string' ? v : v == null ? '' : String(v))
const truncate = (s, n = MAX_LABEL) => (s.length > n ? `${s.slice(0, n - 1)}…` : s)
const log = (...a) => console.info(`[${ID}]`, ...a)

export function elapsed(sinceSec, nowMs = Date.now()) {
  if (!sinceSec) return ''
  const s = Math.max(0, Math.floor(nowMs / 1000 - sinceSec))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}`
  return `${Math.floor(s / 86400)}d`
}

// ── pure projections (exported for tests) ───────────────────────────────────

export function processChips(processes) {
  return (Array.isArray(processes) ? processes : [])
    .filter(p => p && str(p.status) !== 'exited')
    .map(p => {
      const cmd = str(p.command).split('\n')[0].trim() || 'background process'
      const up = Number(p.uptime_seconds)
      const since = up >= 0 && Number.isFinite(up) && p.uptime_seconds != null
        ? Date.now() / 1000 - up
        : Number(p.started_at) || (Date.parse(str(p.started_at)) || 0) / 1000
      return { key: `bg:${str(p.session_id) || cmd}`, kind: 'bg', state: 'running', label: cmd, title: str(p.command) || cmd, since }
    })
}

export function subagentChips(subagents) {
  return (Array.isArray(subagents) ? subagents : [])
    .filter(s => s && !SUBAGENT_DONE.has(str(s.status).toLowerCase()))
    .map(s => {
      const goal = str(s.goal).split('\n')[0].trim() || 'subagent'
      return {
        key: `sub:${str(s.subagent_id) || `${str(s.delegation_id)}:${str(s.task_index)}:${goal}`}`,
        kind: 'sub',
        state: 'running',
        label: goal,
        title: str(s.goal) || goal,
        since: Number(s.started_at) || 0
      }
    })
}

const KANBAN_RANK = { running: 0, blocked: 1, review: 2, ready: 3, scheduled: 4, todo: 5 }

function taskChip(t, status) {
  const watching = t.via === 'subscribed'
  return {
    key: `kb:${t.id}`,
    kind: 'kanban',
    id: str(t.id),
    state: status,
    label: str(t.title) || t.id,
    title: `${t.id} · ${status}${t.assignee ? ` · ${t.assignee}` : ''}${watching ? ' · shepherding (created by another chat)' : ''}${t.board && t.board !== 'default' ? ` · board ${t.board}` : ''}\n${str(t.title)}`,
    since: Number(t.current_run_started_at || t.started_at || t.created_at) || 0
  }
}

const byRank = chips => chips.sort((a, b) => (KANBAN_RANK[a.state] ?? 9) - (KANBAN_RANK[b.state] ?? 9))

/** `/api/session-kanban-tasks` rows (already filtered to the lineage by the backend). */
export function sessionTaskChips(tasks) {
  const seen = new Set()
  const out = []
  for (const t of Array.isArray(tasks) ? tasks : []) {
    const status = str(t?.status)
    if (!t?.id || !KANBAN_ACTIVE.has(status) || seen.has(t.id)) continue
    seen.add(t.id)
    out.push(taskChip(t, status))
  }
  return byRank(out)
}

/** Fallback for backends without that route: scan the current board. */
export function kanbanChips(board, sessionIds) {
  const ids = sessionIds instanceof Set ? sessionIds : new Set(sessionIds || [])
  const out = []
  for (const col of board?.columns || []) {
    for (const t of col?.tasks || []) {
      const status = str(t?.status || col?.name)
      if (!KANBAN_ACTIVE.has(status) || !ids.has(str(t?.session_id))) continue
      out.push(taskChip(t, status))
    }
  }
  return byRank(out)
}

// Rented machines (compute-rentals inventory rows). A machine belongs to the
// session when one of its ownership bindings names this gateway + profile and
// any id in the session's lineage (bindings keep the id they were made under).
const MACHINE_RANK = { running: 0, error: 1 }

function rateLabel(m) {
  if (m?.billing_class === 'internal_prepaid') return ''
  const rate = Number(m?.hourly_rate)
  if (!m?.currency || m?.hourly_rate == null || !Number.isFinite(rate)) return ''
  const amount = rate === 0 ? '0' : rate >= 10 ? rate.toFixed(0) : rate.toFixed(2)
  return m.currency === 'USD' ? `$${amount}/h` : `${amount} ${m.currency}/h`
}

export function machineChips(machines, { gatewayId, profile, ids } = {}) {
  const lineage = ids instanceof Set ? ids : new Set(ids || [])
  if (!gatewayId || !profile || !lineage.size) return []
  const seen = new Set()
  const out = []
  for (const m of Array.isArray(machines) ? machines : []) {
    if (!m?.key || seen.has(m.key)) continue
    const mine = (m.ownership || []).some(
      o => o && o.gateway_id === gatewayId && o.profile === profile && lineage.has(str(o.session_id))
    )
    if (!mine) continue
    seen.add(m.key)
    const status = str(m.status).toLowerCase() || 'unknown'
    const rate = rateLabel(m)
    const name = str(m.display_name) || str(m.provider_id) || 'machine'
    out.push({
      key: `box:${m.key}`,
      kind: 'machine',
      id: str(m.key),
      state: status,
      label: name,
      extra: rate,
      title: [
        `${name} · ${str(m.provider)}${m.region ? ` ${m.region}` : ''} · ${status}${m.stale ? ' (stale)' : ''}`,
        str(m.hardware),
        rate || (m.billing_class === 'internal_prepaid' ? 'internal prepaid' : 'rate unknown'),
        m.ssh ? `ssh: ${m.ssh}` : ''
      ].filter(Boolean).join('\n'),
      since: status === 'running' ? (Date.parse(str(m.started_at)) || 0) / 1000 : 0
    })
  }
  return out.sort((a, b) => (MACHINE_RANK[a.state] ?? 9) - (MACHINE_RANK[b.state] ?? 9))
}

// Archive guard line: the machines this chat still holds, so archiving it
// (which only hides the chat) doesn't leave them running unnoticed.
export function machineArchiveLines(chips, max = 3) {
  const machines = (chips || []).filter(c => c?.kind === 'machine')
  if (!machines.length) return []
  const part = c => `${c.label} (${[c.state, c.extra].filter(Boolean).join(', ')})`
  const shown = machines.slice(0, max).map(part).join(', ')
  const more = machines.length > max ? ` +${machines.length - max} more` : ''
  return [{ label: machines.length === 1 ? 'Rented machine' : `Rented machines (${machines.length})`, value: shown + more }]
}

// ── data ────────────────────────────────────────────────────────────────────

function activeConnection() {
  try {
    return host.activeConnectionId?.() || host.state?.connectionId?.get?.() || undefined
  } catch {
    return undefined
  }
}

function api(path, connection, timeoutMs = 15_000) {
  const bridge = typeof window !== 'undefined' ? window.hermesDesktop?.api : null
  if (!bridge) return Promise.reject(new Error('Hermes desktop bridge unavailable'))
  const connectionId = connection || activeConnection()
  return bridge({ path, ...(connectionId ? { connectionId } : {}), timeoutMs })
}

// stored id → { at, ids:Set } — the session plus its parent chain (compression
// rotates the stored id; kanban tasks keep the id they were created under).
const lineageCache = new Map()

async function lineageIds(storedId, connection) {
  const cacheKey = connection ? `${connection}\u0000${storedId}` : storedId
  const hit = lineageCache.get(cacheKey)
  if (hit && Date.now() - hit.at < LINEAGE_TTL_MS) return hit.ids
  const ids = new Set([storedId])
  let cur = storedId
  for (let hop = 0; hop < 12 && cur; hop++) {
    try {
      const row = await api(`/api/sessions/${encodeURIComponent(cur)}`, connection, 5_000)
      if (row?.id) ids.add(str(row.id))
      cur = str(row?.parent_session_id)
      if (cur) ids.add(cur)
    } catch {
      break
    }
  }
  lineageCache.set(cacheKey, { at: Date.now(), ids })
  return ids
}

async function safe(fn, fallback) {
  try {
    return await fn()
  } catch {
    return fallback
  }
}

// One shared poller for every mounted strip (tiles mount one composer each).
let snapshot = { runtime: null, stored: null, chips: [] }
const subs = new Set()
let timer = null
let lastKanbanAt = 0
let kanbanCache = []
let lastMachinesAt = 0
let machineCache = []
let running = false
let rerun = false

const emit = () => subs.forEach(fn => fn(snapshot))

async function loadKanban(ids) {
  const list = await safe(
    () => api(`/api/session-kanban-tasks?ids=${encodeURIComponent([...ids].join(','))}`),
    null
  )
  if (list && Array.isArray(list.tasks)) return sessionTaskChips(list.tasks)
  const board = await safe(() => api('/api/plugins/kanban/board'), null)
  return board ? kanbanChips(board, ids) : null
}

function focusedProfile() {
  try {
    const owner = host.state.focusedSessionOwner?.get?.()
    const conn = activeConnection()
    // The inventory is read through the active connection; a session owned by
    // another connection/profile would be matched against the wrong registry.
    if (owner?.connectionId && conn && owner.connectionId !== conn) return null
    return owner?.profile || host.state.profile?.get?.() || 'default'
  } catch {
    return null
  }
}

async function loadMachines(ids) {
  const profile = focusedProfile()
  if (!profile) return []
  const inv = await safe(() => api('/api/plugins/compute-rentals/inventory?session_id=&profile=&gateway_id='), null)
  if (!inv || !Array.isArray(inv.machines)) return null
  return machineChips(inv.machines, { gatewayId: str(inv.gateway_id), profile, ids })
}

// ctx.registerArchiveGuard: look up the machines bound to the chat being
// archived on ITS gateway (connectionId from the row), across its lineage.
export async function archiveGuard({ sessionId, ids, profile, connectionId } = {}) {
  if (!sessionId) return []
  const lineage = new Set(ids || [])
  for (const id of await lineageIds(sessionId, connectionId)) lineage.add(id)
  const inv = await api('/api/plugins/compute-rentals/inventory?session_id=&profile=&gateway_id=', connectionId, 2_000)
  if (!inv || !Array.isArray(inv.machines)) return []
  const chips = machineChips(inv.machines, { gatewayId: str(inv.gateway_id), profile: profile || 'default', ids: lineage })
  return machineArchiveLines(chips)
}

async function refresh() {
  // A focus change during an in-flight round must not be dropped (that left the
  // strip empty for a whole poll interval after opening a chat): run again after.
  if (running) {
    rerun = true
    return
  }
  if (typeof document !== 'undefined' && document.hidden) return
  running = true
  try {
    const runtime = host.state.focusedSessionId.get()
    const stored = host.state.focusedStoredSessionId.get()
    if (!runtime && !stored) {
      snapshot = { runtime, stored, chips: [] }
      return emit()
    }
    const sameSession = stored === snapshot.stored
    const [procs, subagents] = await Promise.all([
      runtime ? safe(() => host.request('process.list', { session_id: runtime }), null) : null,
      runtime ? safe(() => host.request('subagent.list', { session_id: runtime }), null) : null
    ])
    const kanbanDue = stored && (!sameSession || Date.now() - lastKanbanAt >= KANBAN_POLL_MS)
    const machinesDue = stored && (!sameSession || Date.now() - lastMachinesAt >= MACHINE_POLL_MS)
    if (kanbanDue || machinesDue) {
      const ids = await lineageIds(stored)
      const [kChips, mChips] = await Promise.all([
        kanbanDue ? ((lastKanbanAt = Date.now()), loadKanban(ids)) : undefined,
        machinesDue ? ((lastMachinesAt = Date.now()), loadMachines(ids)) : undefined
      ])
      // null = fetch failed: keep this chat's last good list, never the previous chat's.
      if (kChips) kanbanCache = kChips
      else if (kanbanDue && !sameSession) kanbanCache = []
      if (mChips) machineCache = mChips
      else if (machinesDue && !sameSession) machineCache = []
    }
    if (!stored) {
      kanbanCache = []
      machineCache = []
    }
    // Focus moved while we were fetching: drop this round, the next tick reloads.
    if (host.state.focusedSessionId.get() !== runtime) return
    snapshot = {
      runtime,
      stored,
      chips: [...kanbanCache, ...machineCache, ...subagentChips(subagents?.subagents), ...processChips(procs?.processes)]
    }
    emit()
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

const KIND_GLYPH = { bg: 'codicon-server-process', sub: 'codicon-agent', kanban: 'codicon-tasklist', machine: 'codicon-vm' }

function Chip({ chip, now }) {
  const color = chipColor(chip.state)
  const showMachine = chip.kind === 'machine' && typeof window.__computeRentalsShow === 'function'
  const clickable = chip.kind === 'kanban' || showMachine
  const onClick = chip.kind === 'kanban'
    ? () => {
        if (typeof window.__taskMentionsOpen === 'function') window.__taskMentionsOpen(chip.id)
        else host.navigate?.('/kanban')
      }
    : showMachine
      ? () => window.__computeRentalsShow(chip.id)
      : undefined
  const age = elapsed(chip.since, now)
  return jsxs(clickable ? 'button' : 'span', {
    type: clickable ? 'button' : undefined,
    onClick,
    title: chip.title,
    'data-session-task': chip.kind,
    style: {
      display: 'inline-flex',
      alignItems: 'center',
      gap: 5,
      maxWidth: 280,
      padding: '1px 8px',
      borderRadius: 999,
      ...chipFrame(chip.state),
      color: 'var(--muted-foreground, inherit)',
      fontSize: 11,
      lineHeight: '18px',
      whiteSpace: 'nowrap',
      cursor: clickable ? 'pointer' : 'default',
      fontFamily: 'inherit'
    },
    children: [
      jsx('span', {
        'aria-hidden': true,
        className: chip.state === 'running' ? 'session-tasks-pulse' : undefined,
        style: { width: 6, height: 6, borderRadius: 999, background: color, flex: 'none' }
      }),
      jsx('i', { 'aria-hidden': true, className: `codicon ${KIND_GLYPH[chip.kind]}`, style: { fontSize: 12, flex: 'none', opacity: 0.8 } }),
      chip.kind === 'kanban' && jsx('span', { style: { opacity: 0.7, fontFamily: 'var(--font-mono, ui-monospace, monospace)', fontSize: 10.5 }, children: chip.id }),
      jsx('span', { style: { overflow: 'hidden', textOverflow: 'ellipsis', fontSize: 11 }, children: truncate(chip.label) }),
      chip.extra && jsx('span', { style: { opacity: 0.7, fontSize: 10.5, fontVariantNumeric: 'tabular-nums' }, children: chip.extra }),
      chip.state !== 'running' && jsx('span', { style: { color, fontSize: 10.5 }, children: chip.state }),
      age && jsx('span', { style: { opacity: 0.6, fontSize: 10.5, fontVariantNumeric: 'tabular-nums' }, children: age })
    ]
  })
}

function Strip() {
  const [snap, setSnap] = useState(snapshot)
  const [now, setNow] = useState(Date.now())
  const focused = useValue(host.state.focusedSessionId)
  useEffect(() => subscribe(next => (setSnap(next), setNow(Date.now()))), [])
  useEffect(() => void refresh(), [focused])
  const chips = snap.runtime === focused ? snap.chips : []
  if (!chips.length) return null
  return jsx('div', {
    'data-slot': 'session-tasks',
    style: { display: 'flex', flexWrap: 'wrap', justifyContent: 'flex-end', gap: 4, width: '100%', marginBottom: 2 },
    children: chips.map(chip => jsx(Chip, { chip, now }, chip.key))
  })
}

const STYLE = `@keyframes session-tasks-pulse{0%,100%{opacity:1}50%{opacity:.35}}
.session-tasks-pulse{animation:session-tasks-pulse 1.6s ease-in-out infinite}
@media (prefers-reduced-motion: reduce){.session-tasks-pulse{animation:none}}`

export default {
  id: ID,
  name: 'Session Tasks',
  register(ctx) {
    if (typeof document !== 'undefined' && !document.getElementById(`${ID}-style`)) {
      const el = document.createElement('style')
      el.id = `${ID}-style`
      el.textContent = STYLE
      document.head.appendChild(el)
    }
    ctx.register({ id: 'strip', area: COMPOSER_AREAS.top, order: -10, render: () => jsx(Strip, {}) })
    // Hosts with the fork's archive confirmation ask before archiving a chat
    // that still holds rented machines (Kanban tasks are core's own check).
    ctx.registerArchiveGuard?.(archiveGuard)
    const onVis = () => document.hidden || void refresh()
    document.addEventListener('visibilitychange', onVis)
    window.__sessionTasks = { get snapshot() { return snapshot }, refresh }
    return () => {
      document.removeEventListener('visibilitychange', onVis)
      document.getElementById(`${ID}-style`)?.remove()
      delete window.__sessionTasks
      if (timer) clearInterval(timer)
      timer = null
    }
  }
}
