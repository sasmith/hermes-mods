// node --test: pure helpers + actions (the SDK and React are stubbed by the loader below).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'

register('data:text/javascript,' + encodeURIComponent(`
export async function resolve(spec, ctx, next) {
  if (spec === '@hermes/plugin-sdk') return { url: 'data:text/javascript,export const COMPOSER_AREAS={top:"composer.top"};export const host={state:{}};export const useValue=()=>null', shortCircuit: true }
  if (spec === 'react' || spec === 'react/jsx-runtime') return { url: 'data:text/javascript,export const useEffect=()=>{};export const useState=v=>[v,()=>{}];export const jsx=()=>null;export const jsxs=()=>null', shortCircuit: true }
  return next(spec, ctx)
}`))
const m = await import('../plugin.js')

const ev = (id, kind, payload) => ({ id, kind, payload, created_at: 1000 + id })

test('latestBlock takes the newest block after the newest unblock', () => {
  const events = [
    ev(1, 'blocked', { reason: 'old ask', kind: 'needs_input' }),
    ev(2, 'unblocked', {}),
    ev(3, 'commented', {}),
    ev(4, 'blocked', JSON.stringify({ reason: 'Plug the phone in.', kind: 'capability' }))
  ]
  assert.deepEqual(m.latestBlock(events), { reason: 'Plug the phone in.', kind: 'capability', at: 1004 })
  assert.equal(m.latestBlock([ev(1, 'blocked', { reason: 'x' }), ev(2, 'unblocked', {})]), null)
  assert.equal(m.latestBlock([ev(1, 'blocked', { reason: 'initial_status' })]).reason, '')
})

test('needsHuman: transient and "no human action needed" are not shown; needs_input/capability/unknown are', () => {
  assert.equal(m.needsHuman({ kind: 'transient', reason: 'flaky' }), false)
  assert.equal(m.needsHuman({ kind: 'needs_input', reason: 'No human action needed. Waiting on time: two nightly runs.' }), false)
  assert.equal(m.needsHuman({ kind: 'needs_input', reason: 'Approve the design doc.' }), true)
  assert.equal(m.needsHuman({ kind: 'capability', reason: 'Restore the API login.' }), true)
  assert.equal(m.needsHuman(null), true)
})

test('stepsOf splits inline "1) … 2) …" runs and keeps the intro', () => {
  const r = m.stepsOf('Code is shipped. The rest needs you, in order: 1) plug the phone in; 2) open the app and lock it; 3) say done.')
  assert.equal(r.intro, 'Code is shipped. The rest needs you, in order:')
  assert.deepEqual(r.steps, ['plug the phone in', 'open the app and lock it', 'say done.'])
  const zero = m.stepsOf('Needs the phone. 0) Unlock it and plug it in. 1) Record PiP. 2) Hold the mic.')
  assert.deepEqual(zero.steps, ['Unlock it and plug it in.', 'Record PiP.', 'Hold the mic.'])
  assert.equal(zero.start, 0)
  assert.equal(r.start, 1)
})

test('stepsOf splits line-leading markers and leaves plain prose alone', () => {
  const r = m.stepsOf('I need 2 things:\n1) Fix billing\n   at the settings page.\n2) Reply "go".')
  assert.equal(r.intro, 'I need 2 things:')
  assert.deepEqual(r.steps, ['Fix billing at the settings page.', 'Reply "go".'])
  assert.deepEqual(m.stepsOf('Approve the design (see doc 3) before Phase 2.'), {
    intro: 'Approve the design (see doc 3) before Phase 2.',
    steps: [],
    start: 1
  })
  assert.deepEqual(m.stepsOf(''), { intro: '', steps: [], start: 1 })
})

test('blockedItems keeps human-blocked cards, falls back to the card body, newest first', () => {
  const tasks = [
    { id: 't_a', title: 'A', status: 'blocked', board: 'default' },
    { id: 't_b', title: 'B', status: 'running', board: 'default' },
    { id: 't_c', title: 'C', status: 'blocked', board: 'ops' },
    { id: 't_d', title: 'D', status: 'blocked', board: 'default' },
    { id: 't_e', title: 'E', status: 'blocked', board: 'default' },
    { id: 't_a', title: 'A dup', status: 'blocked', board: 'default' }
  ]
  const details = new Map([
    ['default:t_a', { task: {}, events: [ev(5, 'blocked', { reason: 'Turn on UI Automation.', kind: 'needs_input' })] }],
    ['ops:t_c', { task: { body: 'Human op: rotate the key.' }, events: [ev(1, 'blocked', { reason: 'initial_status' })] }],
    ['default:t_d', { task: {}, events: [ev(9, 'blocked', { reason: 'retrying', kind: 'transient' })] }]
    // t_e: detail not loaded yet
  ])
  const items = m.blockedItems(tasks, details)
  assert.deepEqual(items.map(i => i.id), ['t_a', 't_c', 't_e'])
  assert.equal(items[0].kindLabel, 'Needs your input')
  assert.equal(items[0].text, 'Turn on UI Automation.')
  assert.equal(items[1].fromBody, true)
  assert.equal(items[1].board, 'ops')
  assert.equal(items[1].text, 'Human op: rotate the key.')
  assert.equal(items[2].loaded, false)
})

test('parked (⌘B) matches any lineage id; junk storage is empty', () => {
  assert.deepEqual(m.parseParkedIds('["s1","",3,"s2"]'), ['s1', 's2'])
  assert.deepEqual(m.parseParkedIds('not json'), [])
  assert.deepEqual(m.parseParkedIds(null), [])
  assert.equal(m.isParked(new Set(['tip', 'root']), ['root']), true)
  assert.equal(m.isParked(new Set(['tip']), ['other']), false)
})

test('unblockCard: Done → PATCH ready only; Reply → comment first, then PATCH, on the card board', async () => {
  const calls = []
  const call = async (path, opts) => (calls.push([path, opts.method, opts.body]), {})
  await m.unblockCard({ id: 't_a', board: 'default' }, '   ', call)
  assert.deepEqual(calls, [['/api/plugins/kanban/tasks/t_a', 'PATCH', { status: 'ready' }]])
  calls.length = 0
  await m.unblockCard({ id: 't_c', board: 'ops' }, 'Done, key rotated', call)
  assert.deepEqual(calls, [
    ['/api/plugins/kanban/tasks/t_c/comments?board=ops', 'POST', { body: 'Done, key rotated', author: 'user' }],
    ['/api/plugins/kanban/tasks/t_c?board=ops', 'PATCH', { status: 'ready' }]
  ])
})

test('unblockCard: a failed comment does not unblock', async () => {
  const calls = []
  const call = async (path, opts) => {
    calls.push(opts.method)
    if (opts.method === 'POST') throw new Error('409')
  }
  await assert.rejects(m.unblockCard({ id: 't_a', board: 'default' }, 'note', call))
  assert.deepEqual(calls, ['POST'])
})

test('openCard: core drawer hook, else task-mentions popup, else the Kanban page', () => {
  const seen = []
  assert.equal(m.openCard({ id: 't_a', board: 'ops' }, { __hermesKanbanOpenTask: (id, b) => (seen.push([id, b]), true) }), 'drawer')
  assert.deepEqual(seen, [['t_a', 'ops']])
  assert.equal(m.openCard({ id: 't_a' }, { __taskMentionsOpen: id => seen.push(id) }), 'popup')
  let nav = null
  assert.equal(m.openCard({ id: 't_a' }, {}, p => (nav = p)), 'page')
  assert.equal(nav, '/kanban')
})

test('unescapeText turns literal \\uXXXX escapes into characters', () => {
  assert.equal(m.unescapeText('panel host \\u2014 video'), 'panel host \u2014 video')
  assert.equal(m.unescapeText(null), '')
})

test('checklist: model rewrite wins and gets a hand-back step only when missing', () => {
  const item = { id: 't_x', board: 'default', text: 'Code shipped. Plug the phone in.', steps: [], start: 1, intro: '' }
  const a = m.checklist(item, ['Plug the phone into the Mac.', 'Click Done → unblock.'])
  assert.equal(a.source, 'rewritten')
  assert.deepEqual(a.steps, ['Plug the phone into the Mac.', 'Click Done → unblock.'])
  const b = m.checklist(item, ['Plug the phone into the Mac.'])
  assert.deepEqual(b.steps, ['Plug the phone into the Mac.', m.HANDBACK])
})

test('checklist: falls back to the note numbering, then one step per sentence, always numbered', () => {
  const parsed = m.checklist({ text: 'x', steps: ['plug in', 'open app'], start: 0, intro: 'Do:' }, null)
  assert.equal(parsed.source, 'parsed')
  assert.equal(parsed.start, 0)
  assert.equal(parsed.intro, 'Do:')
  assert.deepEqual(parsed.steps, ['plug in', 'open app', m.HANDBACK])
  const prose = m.checklist({ text: 'Code is on main; the watch is on 295. After 07:00 PT, update the watch to 316. Then reply with what you heard.', steps: [], start: 1 }, [])
  assert.equal(prose.source, 'sentences')
  assert.deepEqual(prose.steps, ['Code is on main; the watch is on 295.', 'After 07:00 PT, update the watch to 316.', 'Then reply with what you heard.', m.HANDBACK])
  assert.deepEqual(m.checklist({ text: '', steps: [] }, null).steps, ['Open the card to see what it needs.', m.HANDBACK])
})

test('sentenceSteps keeps decimals and abbreviations-free prose intact', () => {
  assert.deepEqual(m.sentenceSteps('Median was 0.99 s. Restart the app!  Then wait'), ['Median was 0.99 s.', 'Restart the app!', 'Then wait'])
  assert.deepEqual(m.sentenceSteps(''), [])
})

test('parked chats get numbered steps too', () => {
  assert.equal(m.PARKED_STEPS.length, 2)
  assert.match(m.PARKED_STEPS[1], /⌘B/)
})

test('linkSegments: web URLs and local paths become links; fractions, and/or and trailing punctuation do not', () => {
  const t = 'On your MacBook, open /Users/me/.hermes/kanban/evidence/blind-sheet-v2.html in the browser. See https://example.com/x?a=1. Score 7/10 and/or ~/notes/a.md, then done.'
  const links = m.linkSegments(t).filter(s => s.kind !== 'text')
  assert.deepEqual(links, [
    { kind: 'path', value: '/Users/me/.hermes/kanban/evidence/blind-sheet-v2.html' },
    { kind: 'url', value: 'https://example.com/x?a=1' },
    { kind: 'path', value: '~/notes/a.md' }
  ])
  assert.equal(m.linkSegments(t).map(s => s.value).join(''), t)
  assert.deepEqual(m.linkSegments('(see `/tmp/x.log`)').filter(s => s.kind !== 'text'), [{ kind: 'path', value: '/tmp/x.log' }])
  assert.deepEqual(m.linkSegments('no links here, 1/2 done'), [{ kind: 'text', value: 'no links here, 1/2 done' }])
  assert.deepEqual(m.linkSegments(''), [])
})

test('linkSegments: scheme-less web addresses link with an https href; file names and emails do not', () => {
  const t = 'Open github.com/settings/billing while signed in. Edit plugin.js, README.md; mail someone@example.com.'
  const links = m.linkSegments(t).filter(s => s.kind !== 'text')
  assert.deepEqual(links, [{ kind: 'url', value: 'github.com/settings/billing' }])
  assert.equal(m.linkHref(links[0]), 'https://github.com/settings/billing')
  assert.equal(m.linkHref({ kind: 'url', value: 'https://a.io' }), 'https://a.io')
  assert.equal(m.linkSegments(t).map(s => s.value).join(''), t)
})
