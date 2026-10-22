// node --test: pure projections only (the SDK import is stubbed by the loader below).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'

register('data:text/javascript,' + encodeURIComponent(`
export async function resolve(spec, ctx, next) {
  if (spec === '@hermes/plugin-sdk') return { url: 'data:text/javascript,export const COMPOSER_AREAS={top:"composer.top"};export const host={};export const useValue=()=>null', shortCircuit: true }
  if (spec === 'react' || spec === 'react/jsx-runtime') return { url: 'data:text/javascript,export const useEffect=()=>{};export const useState=v=>[v,()=>{}];export const jsx=()=>null;export const jsxs=()=>null', shortCircuit: true }
  return next(spec, ctx)
}`))
const m = await import('../plugin.js')

test('processChips keeps only live processes, first command line', () => {
  const chips = m.processChips([
    { session_id: 'p1', command: 'npm run dev\n--watch', status: 'running', uptime_seconds: 90 },
    { session_id: 'p2', command: 'make', status: 'exited', exit_code: 0 }
  ])
  assert.equal(chips.length, 1)
  assert.equal(chips[0].label, 'npm run dev')
  assert.equal(m.elapsed(chips[0].since), '1m')
})

test('subagentChips drops terminal statuses', () => {
  const chips = m.subagentChips([
    { subagent_id: 'a', goal: 'Research X', status: 'running' },
    { subagent_id: 'b', goal: 'Done', status: 'completed' },
    { subagent_id: 'c', goal: 'Bad', status: 'failed' }
  ])
  assert.deepEqual(chips.map(c => c.key), ['sub:a'])
})

test('kanbanChips filters by session lineage and active status, running first', () => {
  const board = { columns: [
    { name: 'ready', tasks: [{ id: 't_1', title: 'queued', status: 'ready', session_id: 's_root' }] },
    { name: 'running', tasks: [
      { id: 't_2', title: 'mine', status: 'running', session_id: 's_tip' },
      { id: 't_3', title: 'other session', status: 'running', session_id: 's_other' }
    ] },
    { name: 'done', tasks: [{ id: 't_4', title: 'finished', status: 'done', session_id: 's_tip' }] }
  ] }
  const chips = m.kanbanChips(board, new Set(['s_tip', 's_root']))
  assert.deepEqual(chips.map(c => c.id), ['t_2', 't_1'])
})

test('elapsed formats', () => {
  const now = 1_000_000_000
  assert.equal(m.elapsed(now / 1000 - 5, now), '5s')
  assert.equal(m.elapsed(now / 1000 - 3700, now), '1h01')
  assert.equal(m.elapsed(0, now), '')
})

test('sessionTaskChips: every listed status, shepherded cards marked, running first, deduped', () => {
  const chips = m.sessionTaskChips([
    { id: 't_b', title: 'theirs', status: 'blocked', via: 'subscribed', board: 'default' },
    { id: 't_q', title: 'queued', status: 'todo', via: 'created', board: 'bb' },
    { id: 't_r', title: 'mine', status: 'running', via: 'created', current_run_started_at: 100 },
    { id: 't_r', title: 'mine (dup from another board)', status: 'running' },
    { id: 't_d', title: 'finished', status: 'done' }
  ])
  assert.deepEqual(chips.map(c => `${c.id}:${c.state}`), ['t_r:running', 't_b:blocked', 't_q:todo'])
  assert.match(chips[1].title, /shepherding/)
  assert.match(chips[2].title, /board bb/)
  assert.equal(chips[0].since, 100)
})

test('machineChips keeps machines bound to the session lineage on this gateway/profile', () => {
  const own = (session_id, extra = {}) => [{ gateway_id: 'gw', profile: 'default', session_id, ...extra }]
  const machines = [
    { key: 'k1', provider: 'vast', display_name: 'box-a', status: 'stopped', ownership: own('s_root'), hourly_rate: '0.4', currency: 'USD' },
    { key: 'k2', provider: 'vast', display_name: 'box-b', status: 'running', ownership: own('s_tip'), hourly_rate: '1.234', currency: 'USD', started_at: new Date(Date.now() - 120_000).toISOString(), hardware: '1 × H100' },
    { key: 'k3', provider: 'ec2', display_name: 'other', status: 'running', ownership: own('s_other') },
    { key: 'k4', provider: 'ec2', display_name: 'other-gw', status: 'running', ownership: [{ gateway_id: 'gw2', profile: 'default', session_id: 's_tip' }] },
    { key: 'k5', provider: 'ec2', display_name: 'other-profile', status: 'running', ownership: own('s_tip', { profile: 'work' }) },
    { key: 'k6', provider: 'slurm', display_name: 'node', status: 'running', ownership: [], hourly_rate: null }
  ]
  const chips = m.machineChips(machines, { gatewayId: 'gw', profile: 'default', ids: new Set(['s_tip', 's_root']) })
  assert.deepEqual(chips.map(c => c.id), ['k2', 'k1'])
  assert.equal(chips[0].extra, '$1.23/h')
  assert.equal(chips[0].state, 'running')
  assert.equal(m.elapsed(chips[0].since), '2m')
  assert.match(chips[0].title, /1 × H100/)
  assert.equal(chips[1].state, 'stopped')
  assert.equal(chips[1].since, 0)
  assert.deepEqual(m.machineChips(machines, { gatewayId: '', profile: 'default', ids: ['s_tip'] }), [])
})

test('machineArchiveLines summarizes machine chips for the archive confirm', () => {
  assert.deepEqual(m.machineArchiveLines([]), [])
  assert.deepEqual(m.machineArchiveLines([{ kind: 'kanban', label: 'x' }]), [])
  assert.deepEqual(m.machineArchiveLines([{ kind: 'machine', label: 'vast-4090', state: 'running', extra: '$0.40/h' }]), [
    { label: 'Rented machine', value: 'vast-4090 (running, $0.40/h)' }
  ])
  const many = ['a', 'b', 'c', 'd', 'e'].map(n => ({ kind: 'machine', label: n, state: 'stopped', extra: '' }))
  assert.deepEqual(m.machineArchiveLines(many), [
    { label: 'Rented machines (5)', value: 'a (stopped), b (stopped), c (stopped) +2 more' }
  ])
})

test('machineChips + machineArchiveLines match ownership across the lineage only', () => {
  const machines = [
    { key: 'k1', display_name: 'box-1', status: 'running', currency: 'USD', hourly_rate: '0.4', ownership: [{ gateway_id: 'g', profile: 'default', session_id: 's_root' }] },
    { key: 'k2', display_name: 'box-2', status: 'running', ownership: [{ gateway_id: 'g', profile: 'default', session_id: 's_other' }] }
  ]
  const chips = m.machineChips(machines, { gatewayId: 'g', profile: 'default', ids: new Set(['s_tip', 's_root']) })
  assert.deepEqual(m.machineArchiveLines(chips), [{ label: 'Rented machine', value: 'box-1 (running, $0.40/h)' }])
})

test('scheduled (timer-blocked) chips are yellow, pill included', () => {
  assert.equal(m.chipColor('scheduled'), '#e3b341')
  const f = m.chipFrame('scheduled')
  assert.match(f.border, /#e3b341/)
  assert.match(f.background, /#e3b341/)
  // other states keep the neutral frame
  assert.doesNotMatch(m.chipFrame('todo').border, /#e3b341/)
  assert.equal(m.chipColor('blocked'), '#f85149')
})
