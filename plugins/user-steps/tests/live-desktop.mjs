// Opt-in live test against THIS Mac's Desktop backend. Creates ONE throwaway desktop chat whose reply asks for an
// action, checks the ::user-step frame + ledger step + rolling blocked card, and writes state for the GUI phase.
//   US_LIVE_URL=http://127.0.0.1:PORT node live-desktop.mjs create   -> prints {key, sid, step, card}
//   US_LIVE_URL=http://127.0.0.1:PORT node live-desktop.mjs check    -> asserts the step resolved + card archived
//   US_LIVE_URL=http://127.0.0.1:PORT node live-desktop.mjs cleanup  -> session.close + DELETE the row (always run)
// `create` deletes the chat itself if anything fails. Node >= 22 (global WebSocket).
import fs from 'node:fs/promises'
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { hideThrowaway, deleteThrowaway } from '../../session-mentions/tests/live-throwaway.mjs'

const base = process.env.US_LIVE_URL
if (!base || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base)) throw Error('Set US_LIVE_URL to an explicit local backend')
const STATE = (process.env.TMPDIR || '/tmp') + '/user-steps-live.json'
const html = await (await fetch(base + '/')).text()
const token = html.match(/__HERMES_SESSION_TOKEN__\s*=\s*["']([^"']+)/)?.[1]
if (!token) throw Error('Local bootstrap token unavailable')
const rest = async (path, opts = {}) => {
  const r = await fetch(base + path, { ...opts, headers: { 'X-Hermes-Session-Token': token, 'Content-Type': 'application/json' } })
  if (!r.ok) throw Error(`REST ${r.status} ${path}`)
  return r.json()
}
const sockets = []
async function client() {
  const ws = new WebSocket(base.replace('http:', 'ws:') + '/api/ws?token=' + encodeURIComponent(token)); sockets.push(ws)
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(Error('Websocket failed')) })
  const pending = new Map(); let n = 0
  ws.onmessage = e => { const m = JSON.parse(e.data); const p = pending.get(m.id); if (p) { pending.delete(m.id); m.error ? p.reject(Error(JSON.stringify(m.error))) : p.resolve(m.result) } }
  return (method, params = {}) => new Promise((resolve, reject) => { const id = 'us-' + ++n; pending.set(id, { resolve, reject }); ws.send(JSON.stringify({ jsonrpc: '2.0', id, method, params })); setTimeout(() => pending.has(id) && (pending.delete(id), reject(Error('RPC timeout ' + method))), 60000) })
}
const messages = key => rest(`/api/sessions/${encodeURIComponent(key)}/messages?profile=default&limit=50&order=latest`).then(p => p.messages || [])
const taskStatus = id => {
  const out = execFileSync('sqlite3', [process.env.HOME + '/.hermes/kanban.db', `select status from tasks where id='${id.replace(/[^a-z0-9_]/g, '')}'`]).toString().trim()
  return out
}
const mode = process.argv[2]
const done = () => sockets.forEach(ws => ws.close())

if (mode === 'create') {
  let sid, key, rpc
  try {
    rpc = await client()
    const c = await rpc('session.create', { profile: 'default', source: 'desktop', title: `user-steps live ${Date.now()}`, cwd: process.env.HOME + '/.hermes/cache/scratch' })
    sid = c.session_id; key = c.stored_session_id || c.session_key
    await fs.writeFile(STATE, JSON.stringify({ sid, key }))
    await rpc('prompt.submit', { session_id: sid, queued: true, text: 'This is a test of a reply-framing plugin. Do not use tools. Reply with exactly these two paragraphs and nothing else:\n\nTest build 7 is staged on the bench.\n\nPlug the test widget into the charger and tell me when it is done.' })
    await hideThrowaway(base, token, key)
    let reply
    for (let i = 0; i < 90 && !reply; i++) {
      await new Promise(r => setTimeout(r, 2000))
      reply = (await messages(key)).find(m => m.role === 'assistant' && /user-step|charger/.test(m.content || ''))
    }
    assert.ok(reply, 'no assistant reply')
    assert.match(reply.content, /::user-step\{id="us_[0-9a-f]+" s="[^"]+" t="[^"]*charger[^"]*"\}/, reply.content)
    const ledger = await rest(`/api/plugins/user-steps/steps?session=${encodeURIComponent(key)}`)
    assert.equal(ledger.steps.length, 1, JSON.stringify(ledger))
    assert.equal(ledger.steps[0].kind, 'do')
    assert.ok(ledger.card?.id, 'no rolling card')
    assert.equal(taskStatus(ledger.card.id), 'blocked')
    const state = { sid, key, reply: reply.content, step: ledger.steps[0], card: ledger.card }
    await fs.writeFile(STATE, JSON.stringify(state, null, 1))
    console.log(JSON.stringify(state, null, 1))
  } catch (e) {
    if (sid && rpc) await rpc('session.close', { session_id: sid }).catch(() => undefined)
    if (key) await deleteThrowaway(base, token, key).catch(err => console.error(String(err)))
    throw e
  } finally { done() }
} else if (mode === 'check') {
  const st = JSON.parse(await fs.readFile(STATE, 'utf8'))
  const msgs = await messages(st.key)
  const turn = msgs.find(m => m.role === 'user' && (m.content || '').startsWith('\u2713 Done:'))
  assert.ok(turn, 'no visible "✓ Done:" user turn')
  const step = (await rest(`/api/plugins/user-steps/step?id=${st.step.id}&s=${encodeURIComponent(st.key)}`)).step
  assert.equal(step.status, 'resolved')
  const ledger = await rest(`/api/plugins/user-steps/steps?session=${encodeURIComponent(st.key)}`)
  assert.equal(ledger.steps.length, 0); assert.equal(ledger.card, null)
  assert.equal(taskStatus(st.card.id), 'archived')
  console.log(JSON.stringify({ turn: turn.content, step: step.status, resolution: step.resolution, card: st.card.id, card_status: 'archived' }))
  done()
} else if (mode === 'cleanup') {
  const st = JSON.parse(await fs.readFile(STATE, 'utf8').catch(() => '{}'))
  if (st.sid) { const rpc = await client(); await rpc('session.close', { session_id: st.sid }).catch(() => undefined) }
  if (st.key) await deleteThrowaway(base, token, st.key)
  if (st.card?.id && taskStatus(st.card.id) && taskStatus(st.card.id) !== 'archived') execFileSync('hermes', ['kanban', 'archive', st.card.id])
  console.log('deleted', st.key || '(none)')
  done()
} else throw Error('mode: create | check | cleanup')
