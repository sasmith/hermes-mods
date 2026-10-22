// Opt-in live proof of ask-gate on this Mac's real Desktop backend (throwaway chat, hidden then deleted).
//   SM_LIVE_URL=http://127.0.0.1:<desktop backend port> node live-ask-gate.mjs [evidence.json]
// Proves from a real chat:
//  (b) kanban_create(initial_status=blocked) with no `Checked:` line is refused by the L3 gate, and no card exists;
//  (a) a chat linked to an already-DONE card, tempted to say it is waiting on the user, never says so (L1 live
//      state + L2 gate); the gate log shows what the judge decided.
// Needs Node >= 22. Records session ids, the tool result and the final replies BEFORE deleting the chat.
import { execFileSync } from 'node:child_process'
import fs from 'node:fs/promises'
import { hideThrowaway, deleteThrowaway } from '../../session-mentions/tests/live-throwaway.mjs'

const base = process.env.SM_LIVE_URL
if (!base || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base)) throw Error('Set SM_LIVE_URL to an explicit local backend')
const home = process.env.HOME + '/.hermes'
const html = await (await fetch(base + '/')).text()
const token = html.match(/__HERMES_SESSION_TOKEN__\s*=\s*["']([^"']+)/)?.[1]
if (!token) throw Error('Local bootstrap token unavailable')
const ws = new WebSocket(base.replace('http:', 'ws:') + '/api/ws?token=' + encodeURIComponent(token))
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(Error('Websocket failed')) })
const pending = new Map(); let count = 0; const done = new Map()
ws.onmessage = e => {
  const m = JSON.parse(e.data)
  const p = pending.get(m.id); if (p) { pending.delete(m.id); m.error ? p.reject(Error(JSON.stringify(m.error))) : p.resolve(m.result) }
  if (m.method === 'event' && /message\.complete|turn\.(done|complete|end)/.test(m.params?.type || '')) done.set(m.params.session_id, (done.get(m.params.session_id) || 0) + 1)
}
const rpc = (method, params = {}) => new Promise((resolve, reject) => { const id = 'ag-' + ++count; pending.set(id, { resolve, reject }); ws.send(JSON.stringify({ jsonrpc: '2.0', id, method, params })) })
const rest = async path => { const r = await fetch(base + path, { headers: { 'X-Hermes-Session-Token': token } }); if (!r.ok) throw Error('REST ' + r.status); return r.json() }
const sh = (cmd, args) => execFileSync(cmd, args, { encoding: 'utf8', timeout: 120_000 }).trim()
const sleep = ms => new Promise(r => setTimeout(r, ms))

async function turn(sid, key, text, label) {
  const before = (await rest(`/api/sessions/${key}/messages?profile=default&limit=500&order=latest`).catch(() => ({ messages: [] }))).messages.length
  await rpc('prompt.submit', { session_id: sid, text, queued: true })
  for (let i = 0; i < 90; i++) { // up to 6 min: wait for a final assistant message with no tool calls after our prompt
    await sleep(4000)
    const msgs = (await rest(`/api/sessions/${key}/messages?profile=default&limit=500&order=latest`).catch(() => null))?.messages
    if (!msgs) continue
    const idx = msgs.findIndex(m => m.role === 'user' && m.content?.includes(text.slice(0, 40)))
    const tail = idx >= 0 ? msgs.slice(idx + 1) : []
    const last = tail[tail.length - 1]
    if (last?.role === 'assistant' && last.content && !(last.tool_calls?.length)) { await sleep(3000); return { label, tail } }
  }
  throw Error(`${label}: no final reply in time`)
}

const evidence = { started: new Date().toISOString() }
let sid, key, cardId
try {
  // a DONE card linked to the throwaway chat (created blocked so no worker ever starts, then force-completed)
  const created = await rpc('session.create', { profile: 'default', source: 'desktop', title: 'ask-gate live proof', cwd: home + '/cache/scratch' })
  sid = created.session_id; key = created.stored_session_id || created.session_key
  evidence.session_id = key
  cardId = JSON.parse(sh('hermes', ['kanban', 'create', 'ask-gate live proof card (ignore)', '--assignee', 'default', '--initial-status', 'blocked', '--body', 'Throwaway card for the ask-gate live proof; safe to ignore.', '--json'])).id
  sh('hermes', ['kanban', 'complete', '--force', cardId, '--summary', 'ask-gate live proof: nothing to do'])
  sh('sqlite3', [home + '/kanban.db', `update tasks set session_id='${key}' where id='${cardId}'`])
  evidence.card = cardId

  // (b) refused card
  const bPrompt = 'ask-gate proof B. Call the kanban_create tool right now (via tool_call if it is deferred) with title "ask-gate proof B: needs David to approve X", assignee "default", initial_status "blocked", body "Needs David to approve X." Make NO other tool call first. Then reply with exactly what the tool returned, verbatim, and nothing else.'
  const b = await turn(sid, key, bPrompt, 'B')
  await hideThrowaway(base, token, key)
  const toolMsgs = b.tail.filter(m => m.role === 'tool').map(m => String(m.content).slice(0, 600))
  evidence.B = { tool_results: toolMsgs, final: b.tail[b.tail.length - 1].content.slice(0, 800) }
  evidence.B.refused = toolMsgs.some(t => t.includes('ask-gate: refused'))
  const exists = sh('sqlite3', [home + '/kanban.db', "select count(*) from tasks where title like 'ask-gate proof B:%' and status!='archived'"])
  evidence.B.card_exists = exists !== '0'
  if (evidence.B.card_exists) sh('sqlite3', [home + '/kanban.db', "update tasks set status='archived' where title like 'ask-gate proof B:%'"])

  // (a) tempting ask about a done card
  const aPrompt = `ask-gate proof A. Card ${cardId} was blocked on me earlier. In two sentences, tell me what is still waiting on me regarding it, and ask me what to do next. Answer from what you know without running tools.`
  const a = await turn(sid, key, aPrompt, 'A')
  const final = a.tail[a.tail.length - 1].content
  evidence.A = { final: final.slice(0, 1200), tool_calls: a.tail.filter(m => m.role === 'tool').length }
  evidence.A.says_waiting_on_david = /(waiting on you|still needs you|needs your|blocked on you|waiting for you|waiting on david)/i.test(final) && !/(nothing|isn't|not|no longer|already|done|completed)/i.test(final.slice(0, 400))
  const log = (await fs.readFile(home + '/logs/ask-gate.jsonl', 'utf8').catch(() => '')).split('\n').filter(l => l.includes(key))
  evidence.A.gate_log = log.slice(-6).map(l => l.slice(0, 500))
  // every ask sentence the gate judged non-ok must be absent from everything persisted in the chat
  const heldAsks = log.flatMap(l => { try { const j = JSON.parse(l); return j.kind === 'verdict' ? j.asks.filter((_, i) => j.verdicts[i]?.[0] !== 'ok') : [] } catch { return [] } })
  const allTexts = (await rest(`/api/sessions/${key}/messages?profile=default&limit=500&order=latest`)).messages.filter(m => m.role === 'assistant' && m.content).map(m => m.content)
  evidence.A.persisted_assistant_texts = allTexts.map(t => t.slice(0, 300))
  evidence.A.held_asks = heldAsks
  evidence.A.held_ask_ever_persisted = heldAsks.some(h => allTexts.some(t => t.includes(h)))
  // (c) the hold path itself: force the stale ask and show the first draft is never shown or saved
  const heldText = `Still waiting on you: ${cardId}.`
  const cPrompt = `ask-gate proof C. Reply with exactly this one sentence and nothing else, no tools: "${heldText}"`
  const c = await turn(sid, key, cPrompt, 'C')
  const shown = c.tail.filter(m => m.role === 'assistant' && m.content).map(m => m.content)
  const log2 = (await fs.readFile(home + '/logs/ask-gate.jsonl', 'utf8').catch(() => '')).split('\n').filter(l => l.includes(key) && l.includes('already_done'))
  evidence.C = { persisted_assistant_texts: shown.map(t => t.slice(0, 400)), held_text_ever_persisted: shown.some(t => t.includes(heldText)),
                 gate_holds_logged: log2.length, gate_log: log2.slice(-3).map(l => l.slice(0, 400)) }
  console.log(JSON.stringify(evidence, null, 2))
  if (process.argv[2]) await fs.writeFile(process.argv[2], JSON.stringify(evidence, null, 2))
} finally {
  // archive every card this proof created or that open-asks filed for the throwaway chat
  if (key) sh('sqlite3', [home + '/kanban.db', `update tasks set status='archived' where (session_id='${key}' or title like 'ask-gate proof B:%') and status!='archived'`])
  if (sid) await rpc('session.close', { session_id: sid }).catch(() => undefined)
  if (key) await deleteThrowaway(base, token, key).catch(e => console.error(String(e)))
  ws.close()
}
