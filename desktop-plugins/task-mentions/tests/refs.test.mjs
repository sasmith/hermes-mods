// Gateway-qualified refs: `@task:<board>@<gateway>/<id>` must be looked up on that gateway.
import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import { test } from 'node:test'
const source = await fs.readFile(new URL('../plugin.js', import.meta.url), 'utf8')
const m = await import(
  'data:text/javascript;base64,' + Buffer.from(source.replace("import { host as sdkHost } from '@hermes/plugin-sdk'", 'const sdkHost = {}')).toString('base64')
)

test('refQualifier reads the board/gateway prefix right before an id', () => {
  assert.deepEqual(m.refQualifier('see @task:bb@mac-mini/'), { board: 'bb', gateway: 'mac-mini' })
  assert.deepEqual(m.refQualifier('`@task:default/'), { board: 'default', gateway: '' })
  assert.equal(m.refQualifier('plain text '), null)
  assert.equal(m.refQualifier('@task:bb@mac-mini/ then '), null)
})

test('findConnection matches slugged labels and ids', () => {
  const conns = [
    { id: 'local', label: 'This device' },
    { id: 'skint-polycentrism', label: 'nous-1' }
  ]
  assert.equal(m.findConnection(conns, 'nous-1'), 'skint-polycentrism')
  assert.equal(m.findConnection(conns, 'this-device'), 'local')
  assert.equal(m.findConnection(conns, 'skint-polycentrism'), 'skint-polycentrism')
  assert.equal(m.findConnection(conns, 'nope'), null)
})

test('openCard calls the core open-task hook with the explicit board', async () => {
  const calls = []
  const win = { [m.OPEN_TASK_HOOK]: (...a) => (calls.push(a), true) }
  const nav = { navigate: () => calls.push(['navigate']) }
  assert.equal(await m.openCard('t_1a2b3c4d', { board: 'bb', conn: 'local' }, win, nav), true)
  assert.deepEqual(calls, [['t_1a2b3c4d', 'bb', null]])
})

test('openCard passes the owning gateway for a card on another gateway', async () => {
  const calls = []
  const win = { [m.OPEN_TASK_HOOK]: (...a) => (calls.push(a), true) }
  assert.equal(await m.openCard('t_1a2b3c4d', { board: 'bb', conn: 'skint-polycentrism' }, win, {}), true)
  assert.deepEqual(calls, [['t_1a2b3c4d', 'bb', 'skint-polycentrism']])
})

test('openCard resolves the current board when the task was found without one', async () => {
  globalThis.window = { hermesDesktop: { api: async ({ path }) => (path.endsWith('/boards') ? { boards: [], current: 'ops' } : null) } }
  try {
    const calls = []
    const win = { [m.OPEN_TASK_HOOK]: (...a) => (calls.push(a), true) }
    await m.openCard('t_00000000', { board: '', conn: 'local' }, win, {})
    assert.deepEqual(calls, [['t_00000000', 'ops', null]])
  } finally {
    delete globalThis.window
  }
})

test('openCard falls back to the board page on a core without the hook', async () => {
  const seen = []
  const nav = { navigate: p => seen.push(p), notify: n => seen.push(n.kind) }
  assert.equal(await m.openCard('t_1a2b3c4d', { board: 'bb', conn: 'local' }, {}, nav), false)
  assert.deepEqual(seen, ['/kanban', 'info'])
})

test('scheduled (timer-blocked) status pill is yellow', () => {
  assert.equal(m.statusColor('scheduled'), '#e3b341')
  assert.equal(m.statusColor('blocked'), '#f85149')
})

test('sessionTarget prefers the run worker session, labels the creator fallback, hides the focused chat', () => {
  const d = { task: { session_id: 'creator', current_run_id: 2 }, runs: [{ id: 1, worker_session_id: 'old' }, { id: 2, worker_session_id: 'w2' }] }
  assert.deepEqual(m.sessionTarget(d, 'creator'), { id: 'w2', label: 'Open worker session' })
  assert.equal(m.sessionTarget(d, 'w2'), null)
  assert.deepEqual(m.sessionTarget({ task: { session_id: 'creator' }, runs: [{ id: 1 }] }, 'other'), { id: 'creator', label: 'Open creating chat' })
  assert.equal(m.sessionTarget({ task: { session_id: 'creator' }, runs: [] }, 'creator'), null)
  assert.deepEqual(m.sessionTarget({ task: { session_id: 'c', current_run_id: 3 }, runs: [{ id: 1, worker_session_id: 'a' }, { id: 3 }] }, ''), { id: 'a', label: 'Open worker session' })
})
