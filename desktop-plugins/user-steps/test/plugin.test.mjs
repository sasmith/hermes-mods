// node --test: user-steps desktop plugin. Real React + jsdom from the Desktop checkout; SDK stubbed via globals.
//   HERMES_DESKTOP_DIR=~/.hermes/hermes-agent/apps/desktop node --test test/plugin.test.mjs
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createRequire, register } from 'node:module'
import { homedir } from 'node:os'
import { resolve } from 'node:path'

const require = createRequire(resolve(process.env.HERMES_DESKTOP_DIR || `${homedir()}/.hermes/hermes-agent/apps/desktop`, 'package.json'))
const React = require('react')
const jsxRt = require('react/jsx-runtime')
const { JSDOM } = require('jsdom')
const dom = new JSDOM('<div id="root"></div>')
globalThis.window = dom.window
globalThis.document = dom.window.document
globalThis.IS_REACT_ACT_ENVIRONMENT = true
const { createRoot } = require('react-dom/client')

// SDK stub: a focused-session atom + composer recorder the tests drive.
const focused = { v: 'S1', subs: new Set(), get() { return this.v }, set(v) { this.v = v; this.subs.forEach(f => f()) } }
const composer = { sent: [], drafts: [], ok: true, submit(sid, text) { this.sent.push([sid, text]); return this.ok }, async setDraft(sid, text) { this.drafts.push([sid, text]); return true }, focus() {} }
globalThis.__us = {
  React,
  jsxRt,
  sdk: {
    COMPOSER_AREAS: { top: 'composer.top' },
    TRANSCRIPT_DIRECTIVE_AREA: 'transcript.directives',
    host: { state: { focusedStoredSessionId: focused }, composer, notify: () => {} },
    useValue: atom => atom.get()
  }
}
register('data:text/javascript,' + encodeURIComponent(`
export async function resolve(spec, ctx, next) {
  const map = {
    '@hermes/plugin-sdk': 'const s=globalThis.__us.sdk;export const COMPOSER_AREAS=s.COMPOSER_AREAS,TRANSCRIPT_DIRECTIVE_AREA=s.TRANSCRIPT_DIRECTIVE_AREA,host=s.host,useValue=s.useValue',
    'react': 'const R=globalThis.__us.React;export const useEffect=R.useEffect,useState=R.useState',
    'react/jsx-runtime': 'const J=globalThis.__us.jsxRt;export const jsx=J.jsx,jsxs=J.jsxs'
  }
  if (map[spec]) return { url: 'data:text/javascript,' + encodeURIComponent(map[spec]), shortCircuit: true }
  return next(spec, ctx)
}`))
const m = await import('../plugin.js')

// fake REST: /steps, /step, /resolve over an in-memory ledger
function fakeRest(steps, { card = null, down = false } = {}) {
  const calls = []
  const call = async (path, opts = {}) => {
    calls.push([path, opts.method || 'GET', opts.body])
    if (down) throw new Error('backend down')
    const u = new URL(path, 'http://x')
    if (u.pathname.endsWith('/steps')) return { steps: steps.filter(s => s.status === 'open'), card }
    if (u.pathname.endsWith('/step')) return { step: steps.find(s => s.id === u.searchParams.get('id')) || null }
    if (u.pathname.endsWith('/resolve')) {
      const s = steps.find(x => x.id === opts.body.id)
      if (s) Object.assign(s, { status: 'resolved', resolution: opts.body.resolution })
      if (!steps.some(x => x.status === 'open')) card = null
      return { ok: true, resolved: s ? [s.id] : [], card }
    }
    throw new Error('unexpected ' + path)
  }
  call.calls = calls
  return call
}

const step = (id, kind, extra = {}) => ({ id, kind, text: `text of ${id}`, status: 'open', created: 1, session: 'S1', ...extra })
const flush = () => new Promise(r => setTimeout(r, 0))
const root = createRoot(document.getElementById('root'))
const el = () => document.getElementById('root')
async function show(node) {
  await React.act(async () => { root.render(node) })
  await React.act(async () => { await flush() })
}

test('decodeAttr inverts the agent-side escapes; directiveIds validates ids', () => {
  assert.equal(m.decodeAttr('Reply \\qyes\\q or \\(no\\) \\\\ x\\nnext'), 'Reply "yes" or {no} \\ x\nnext')
  assert.deepEqual(m.directiveIds({ id: 'us_0123456789, us_abcdef12,evil' }), ['us_0123456789', 'us_abcdef12'])
})

test('actionsFor gives each kind its buttons', () => {
  const lbl = s => m.actionsFor(s).map(a => a.label)
  assert.deepEqual(lbl(step('us_aaaaaa01', 'approve')), ['Do it', 'No'])
  assert.deepEqual(lbl(step('us_aaaaaa02', 'do')), ['Done', 'Can\u2019t'])
  assert.deepEqual(lbl(step('us_aaaaaa03', 'choose', { options: ['SQLite', 'Postgres'] })), ['SQLite', 'Postgres'])
  assert.deepEqual(lbl(step('us_aaaaaa04', 'input')), ['Reply\u2026'])
  const [go] = m.actionsFor(step('us_aaaaaa01', 'approve'))
  assert.equal(go.text, 'Go: text of us_aaaaaa01')
  assert.equal(m.actionsFor(step('us_aaaaaa02', 'do'))[0].text, '\u2713 Done: text of us_aaaaaa02')
  assert.equal(m.actionsFor(step('us_aaaaaa03', 'choose', { options: ['SQLite', 'Postgres'] }))[0].text, 'Chose: SQLite \u2014 text of us_aaaaaa03')
})

test('click: submit a visible turn, THEN resolve; no composer = no resolve; Reply only drafts', async () => {
  const steps = [step('us_aaaaaa02', 'do'), step('us_aaaaaa04', 'input')]
  const call = fakeRest(steps)
  composer.sent = []
  const done = m.actionsFor(steps[0])[0]
  assert.equal(await m.performAction(steps[0], done, 'S1', { composer, call }), 'sent')
  assert.deepEqual(composer.sent, [['S1', '\u2713 Done: text of us_aaaaaa02']])
  assert.deepEqual(call.calls.at(-1), ['/api/plugins/user-steps/resolve', 'POST', { id: 'us_aaaaaa02', session: 'S1', resolution: 'Done', by: 'desktop' }])
  composer.ok = false
  const n = call.calls.length
  assert.equal(await m.performAction(steps[0], done, 'S1', { composer, call }), 'no-composer')
  assert.equal(call.calls.length, n)
  composer.ok = true
  assert.equal(await m.performAction(steps[1], m.actionsFor(steps[1])[0], 'S1', { composer, call }), 'drafted')
  assert.deepEqual(composer.drafts.at(-1), ['S1', 'Re: text of us_aaaaaa04 \u2014 '])
  assert.equal(call.calls.length, n)
})

test('projectStrip: open steps oldest first, then blocked cards, minus the chat\u2019s rolling card', () => {
  const ledger = {
    steps: [step('us_bbbbbb02', 'do', { created: 20 }), step('us_bbbbbb01', 'approve', { created: 10 }), step('us_bbbbbb03', 'do', { status: 'resolved' })],
    card: { id: 't_rolling1', board: 'default' }
  }
  const kanban = [{ id: 't_other002', board: 'default', title: 'B', at: 50 }, { id: 't_rolling1', board: 'default', title: 'For you: …', at: 1 }, { id: 't_other001', board: 'x', title: 'A', at: 40 }]
  const rows = m.projectStrip(ledger, kanban)
  assert.deepEqual(rows.map(r => r.key), ['s:us_bbbbbb01', 's:us_bbbbbb02', 'c:x:t_other001', 'c:default:t_other002'])
  assert.deepEqual(m.projectStrip({ steps: [], card: null }, []), [])
  // after the last step resolved: card is null but the archived rolling card may still sit in a stale snapshot
  assert.deepEqual(m.projectStrip({ steps: [], card: null, own_cards: ['t_rolling1'] }, kanban).map(r => r.key), ['c:x:t_other001', 'c:default:t_other002'])
  assert.equal(m.cardSummary({ text: 'Plug it in. Then say done.' }, () => ({ steps: ['Plug it in.'] })), 'Plug it in.')
})

test('directive renders a framed box from the ledger, and the click resolves it in place', async () => {
  m.clearStepCache()
  const steps = [step('us_cccccc01', 'approve', { text: 'Approve the deploy' })]
  const call = fakeRest(steps)
  composer.sent = []
  await show(React.createElement(m.StepBox, { attrs: { id: 'us_cccccc01', s: 'S1', t: 'Want me to deploy build \\q412\\q?' }, streaming: false, deps: { call, composer, sessionId: 'S1' } }))
  const box = el().querySelector('[data-user-step="us_cccccc01"]')
  assert.ok(box, el().innerHTML)
  assert.equal(box.style.userSelect || box.style.webkitUserSelect, 'text')
  assert.equal(box.querySelector('[data-user-step-text]').textContent, 'Want me to deploy build "412"?')
  const buttons = [...box.querySelectorAll('[data-user-step-action]')].map(b => b.textContent)
  assert.deepEqual(buttons, ['Do it', 'No'])
  await React.act(async () => { box.querySelector('[data-user-step-action="Do it"]').click(); await flush() })
  await React.act(async () => { await flush() })
  assert.deepEqual(composer.sent, [['S1', 'Go: Approve the deploy']])
  assert.equal(steps[0].status, 'resolved')
  assert.match(el().querySelector('[data-user-step-resolved]').textContent, /Go/)
  assert.equal(el().querySelector('[data-user-step-action]'), null)
})

test('directive falls back to plain text when the ledger is unreachable; empty t renders nothing', async () => {
  m.clearStepCache()
  const call = fakeRest([], { down: true })
  await show(React.createElement(m.StepBox, { key: 'a', attrs: { id: 'us_dddddd01', s: 'S1', t: 'Plug in the \\(watch\\)' }, streaming: false, deps: { call } }))
  assert.equal(el().querySelector('[data-user-step]'), null)
  assert.equal(el().querySelector('[data-user-step-fallback]').textContent, 'Plug in the {watch}')
  await show(React.createElement(m.StepBox, { key: 'b', attrs: { id: 'us_dddddd02', s: 'S1' }, streaming: false, deps: { call } }))
  assert.equal(el().textContent, '')
})

test('strip: lists open steps + other blocked cards for the focused chat, hides when empty', async () => {
  const steps = [step('us_eeeeee01', 'do', { text: 'Plug the watch in' })]
  let card = { id: 't_rolling9', board: 'default' }
  const call = fakeRest(steps, { card })
  window.__blockedPanel = { snapshot: { stored: 'S1', items: [{ id: 't_rolling9', board: 'default', title: 'For you: Plug', at: 1 }, { id: 't_human001', board: 'default', title: 'Approve the doc', text: 'Read the doc.', at: 2 }] } }
  focused.set('S1')
  await m.refresh(call)
  await show(React.createElement(m.Strip, { key: 's1' }))
  const strip = el().querySelector('[data-slot="user-steps"]')
  assert.ok(strip, el().innerHTML)
  // the composer chrome is user-select:none; the strip must opt back in so its text can be copied
  assert.equal(strip.style.userSelect || strip.style.webkitUserSelect, 'text')
  assert.match(strip.textContent, /For you \(2\)/)
  assert.ok(strip.querySelector('[data-user-steps-step="us_eeeeee01"]'))
  assert.ok(strip.querySelector('[data-user-steps-card="t_human001"]'))
  assert.equal(strip.querySelector('[data-user-steps-card="t_rolling9"]'), null)
  // Kanban-backed rows keep blocked-panel's unblock behaviour (no chat turn is sent)
  const unblocked = []
  window.__blockedPanel.unblockCard = async (item, note) => unblocked.push([item.id, note])
  composer.sent = []
  const doneBtn = [...strip.querySelectorAll('[data-user-steps-card="t_human001"] button')].find(b => b.textContent.startsWith('Done'))
  await React.act(async () => { doneBtn.click(); await flush() })
  assert.deepEqual(unblocked, [['t_human001', '']])
  assert.deepEqual(composer.sent, [])
  // everything resolved, no other cards -> no strip at all
  steps[0].status = 'resolved'
  window.__blockedPanel = { snapshot: { stored: 'S1', items: [] } }
  await React.act(async () => { await m.refresh(call); await flush() })
  assert.equal(el().querySelector('[data-slot="user-steps"]'), null)
  delete window.__blockedPanel
})

test('a click in the strip also flips the in-message box to resolved', async () => {
  m.clearStepCache()
  const steps = [step('us_ffffff01', 'do', { text: 'Charge the widget' })]
  const call = fakeRest(steps, { card: { id: 't_rolling8' } })
  composer.sent = []
  focused.set('S1')
  await m.refresh(call)
  await show(React.createElement('div', { key: 'both' }, [
    React.createElement(m.StepBox, { key: 'box', attrs: { id: 'us_ffffff01', s: 'S1', t: 'Charge the widget, then tell me.' }, streaming: false, deps: { call } }),
    React.createElement(m.Strip, { key: 'strip' })
  ]))
  assert.ok(el().querySelector('[data-user-step="us_ffffff01"] [data-user-step-action="Done"]'))
  // the strip's buttons use the real REST bridge; route it to the fake
  window.hermesDesktop = { api: ({ path, ...opts }) => call(path, opts) }
  await React.act(async () => { el().querySelector('[data-slot="user-steps"] [data-user-step-action="Done"]').click(); await flush() })
  await React.act(async () => { await flush(); await flush() })
  assert.deepEqual(composer.sent, [['S1', '\u2713 Done: Charge the widget']])
  assert.equal(steps[0].status, 'resolved')
  assert.ok(el().querySelector('[data-user-step-resolved="us_ffffff01"]'), el().innerHTML.slice(0, 400))
  assert.equal(el().querySelector('[data-slot="user-steps"]'), null)
  delete window.hermesDesktop
})

test('linkSegments/linkHref: https, bare web addresses and paths link; file names, emails and fractions do not', () => {
  const t = 'Open github.com/settings/billing while signed in. See https://x.io/a?b=1, www.example.org. Edit plugin.js and README.md; mail someone@example.com; 7/10; ~/notes/a.md.'
  const links = m.linkSegments(t).filter(x => x.kind !== 'text')
  assert.deepEqual(links, [
    { kind: 'url', value: 'github.com/settings/billing' },
    { kind: 'url', value: 'https://x.io/a?b=1' },
    { kind: 'url', value: 'www.example.org' },
    { kind: 'path', value: '~/notes/a.md' }
  ])
  assert.equal(m.linkSegments(t).map(x => x.value).join(''), t)
  assert.deepEqual(links.map(m.linkHref), ['https://github.com/settings/billing', 'https://x.io/a?b=1', 'https://www.example.org', 'file://~/notes/a.md'])
  assert.deepEqual(m.linkSegments('example.company and foo.community').filter(x => x.kind !== 'text'), [])
})

test('strip: URLs in a blocked card summary and in a step are clickable and open via ctx.os.openExternal', async () => {
  const opened = []
  const dispose = m.default.register({ register: () => {}, os: { openExternal: async u => (opened.push(u), true) } })
  const steps = [step('us_eeeeee77', 'do', { text: 'Visit https://example.com/step now' })]
  const call = fakeRest(steps)
  window.__blockedPanel = { snapshot: { stored: 'S1', items: [{ id: 't_billing1', board: 'default', title: 'Fix CI', text: 'Open github.com/settings/billing while signed in as you.', at: 2 }] } }
  focused.set('S1')
  await m.refresh(call)
  await show(React.createElement(m.Strip, { key: 's-links' }))
  const card = el().querySelector('[data-user-steps-card="t_billing1"] a[data-user-steps-link="url"]')
  assert.ok(card, el().innerHTML)
  assert.equal(card.textContent, 'github.com/settings/billing')
  assert.equal(card.getAttribute('href'), 'https://github.com/settings/billing')
  const stepLink = el().querySelector('[data-user-steps-step="us_eeeeee77"] a[data-user-steps-link="url"]')
  assert.ok(stepLink, el().innerHTML)
  await React.act(async () => { card.click(); stepLink.click(); await flush() })
  assert.deepEqual(opened, ['https://github.com/settings/billing', 'https://example.com/step'])
  steps[0].status = 'resolved'
  window.__blockedPanel = { snapshot: { stored: 'S1', items: [] } }
  await React.act(async () => { await m.refresh(call); await flush() })
  delete window.__blockedPanel
  dispose()
})

test('register: strip in composer.top and the user-step directive', () => {
  const got = []
  const dispose = m.default.register({ register: c => got.push(c) })
  assert.deepEqual(got.map(c => c.area), ['composer.top', 'transcript.directives'])
  assert.equal(got[1].data.name, 'user-step')
  assert.equal(window.__userSteps.ownsComposer, true)
  dispose()
  assert.equal(window.__userSteps, undefined)
})

test('done', () => { root.unmount() })
