// Opt-in GUI half of the live test: drives the RUNNING Desktop over the main-process inspector (kill -USR1 <Hermes main pid>
// first). Opens the throwaway chat, screenshots, clicks Done in the For-you strip, screenshots, restores the chat
// you had focused (in finally). Usage: node live-desktop-gui.mjs <stored session id>  (PNGs -> ~/.hermes/cache/scratch)
const KEY = process.argv[2]
const list = await (await fetch('http://127.0.0.1:9229/json/list')).json()
const ws = new WebSocket(list[0].webSocketDebuggerUrl)
await new Promise(r => ws.addEventListener('open', r))
let id = 0; const pending = new Map()
ws.addEventListener('message', ev => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id) } })
const send = (method, params) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })) })
const renderer = `(async () => {
  const host = window.__HERMES_PLUGIN_SDK__.host
  const sleep = ms => new Promise(r => setTimeout(r, ms))
  const out = { back: host.state.focusedStoredSessionId.get() }
  window.__usLiveBack = out.back
  await host.openSession(${JSON.stringify(KEY)})
  for (let i = 0; i < 40; i++) {
    if (document.querySelector('[data-user-step]') && document.querySelector('[data-slot="user-steps"] [data-user-step-action]')) break
    await sleep(500)
  }
  const box = document.querySelector('[data-user-step]')
  const strip = document.querySelector('[data-slot="user-steps"]')
  out.focused = host.state.focusedStoredSessionId.get()
  out.box = box ? { text: box.innerText.slice(0, 300), buttons: [...box.querySelectorAll('[data-user-step-action]')].map(b => b.textContent) } : null
  out.strip = strip ? { text: strip.innerText.slice(0, 300) } : null
  if (box) box.scrollIntoView({ block: 'center' })
  return JSON.stringify(out)
})()`
const click = `(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms))
  const b = document.querySelector('[data-slot="user-steps"] [data-user-step-action="Done"]')
  if (!b) return JSON.stringify({ clicked: false })
  b.click()
  for (let i = 0; i < 30; i++) { await sleep(500); if (!document.querySelector('[data-slot="user-steps"]')) break }
  await sleep(1500)
  const users = [...document.querySelectorAll('[data-slot="aui_user-message-root"], [data-role="user"]')].map(n => n.innerText.slice(0, 120))
  return JSON.stringify({ clicked: true, stripGone: !document.querySelector('[data-slot="user-steps"]'), lastUser: users.slice(-2), boxResolved: !!document.querySelector('[data-user-step-resolved]') })
})()`
const restore = `(async () => { const host = window.__HERMES_PLUGIN_SDK__.host; if (window.__usLiveBack) await host.openSession(window.__usLiveBack); return host.state.focusedStoredSessionId.get() })()`
const main = js => `(async () => {
  const { BrowserWindow } = process.mainModule.require('electron');
  const wins = BrowserWindow.getAllWindows().filter(w => !w.isDestroyed());
  const w = wins.sort((a,b) => b.getBounds().width*b.getBounds().height - a.getBounds().width*a.getBounds().height)[0];
  return await w.webContents.executeJavaScript(${JSON.stringify(js)});
})()`
const shot = file => `(async () => {
  const { BrowserWindow } = process.mainModule.require('electron');
  const fs = process.mainModule.require('fs');
  const wins = BrowserWindow.getAllWindows().filter(w => !w.isDestroyed());
  const w = wins.sort((a,b) => b.getBounds().width*b.getBounds().height - a.getBounds().width*a.getBounds().height)[0];
  const img = await w.webContents.capturePage(); fs.writeFileSync(${JSON.stringify(file)}, img.toPNG()); return img.getSize();
})()`
const ev = async e => { const r = await send('Runtime.evaluate', { expression: e, awaitPromise: true, returnByValue: true }); return r.result?.result?.value ?? r.result }
const dir = process.env.HOME + '/.hermes/cache/scratch/'
try {
  console.log('open', await ev(main(renderer)))
  console.log('shot1', JSON.stringify(await ev(shot(dir + 'us-live-1-before.png'))))
  console.log('click', await ev(main(click)))
  console.log('shot2', JSON.stringify(await ev(shot(dir + 'us-live-2-after.png'))))
} finally {
  console.log('restore', await ev(main(restore)))
  ws.close()
}
