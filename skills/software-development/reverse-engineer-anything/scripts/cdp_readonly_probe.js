// Navegação somente leitura via CDP + estrutura das respostas XHR/Fetch (sem valores).
// Uso: node cdp_readonly_probe.js <targetId> <origemPermitida> <exprFile> [url1 url2 ...]
//   porta via env CDP_PORT (padrão 9333). exprFile: expressão JS que devolve string (JSON.stringify(...)).
//   Sem URLs: só avalia a expressão na página atual. Volta à URL inicial ao terminar.
const fs = require('fs');
const [id, origin, exprFile, ...urls] = process.argv.slice(2);
if (!id || !origin || !exprFile) { console.error('uso: targetId origem exprFile [urls]'); process.exit(2); }
const expr = fs.readFileSync(exprFile, 'utf8');
const ws = new WebSocket(`ws://127.0.0.1:${process.env.CDP_PORT || 9333}/devtools/page/${id}`);
let n = 0; const pend = {}; const R = {};
const send = (m, p = {}) => new Promise(r => { const i = ++n; pend[i] = r; ws.send(JSON.stringify({ id: i, method: m, params: p })); });
const sleep = ms => new Promise(r => setTimeout(r, ms));
const redact = u => u.replace(/([?&][a-z_]*(?:token|hash|key|codigo|cpf|email|senha|pass)[a-z_]*=)[^&]*/gi, '$1<redigido>');
const shape = (v, d = 0) => d > 4 ? '…' : Array.isArray(v) ? [v.length ? shape(v[0], d + 1) : 'vazio']
  : (v && typeof v === 'object') ? Object.fromEntries(Object.keys(v).slice(0, 40).map(k => [k, shape(v[k], d + 1)])) : (v === null ? 'null' : typeof v);
ws.onmessage = async e => {
  const m = JSON.parse(e.data); if (m.id && pend[m.id]) return pend[m.id](m);
  const p = m.params || {};
  if (m.method === 'Network.requestWillBeSent' && ['XHR', 'Fetch'].includes(p.type)) {
    const u = new URL(p.request.url);
    R[p.requestId] = { m: p.request.method, url: redact(u.origin + u.pathname + u.search), post_keys: p.request.postData ? [...new URLSearchParams(p.request.postData).keys()] : null };
  }
  if (m.method === 'Network.responseReceived' && R[p.requestId]) R[p.requestId].st = p.response.status;
  if (m.method === 'Network.loadingFinished' && R[p.requestId]) {
    const b = await send('Network.getResponseBody', { requestId: p.requestId });
    try { R[p.requestId].shape = shape(JSON.parse(b.result.body)); } catch (_) { R[p.requestId].shape = 'não-JSON ' + ((b.result || {}).body || '').length; }
  }
};
ws.onopen = async () => {
  await send('Network.enable');
  const ev = x => send('Runtime.evaluate', { expression: x, returnByValue: true, awaitPromise: true });
  const start = (await ev('location.href')).result.result.value;
  const pages = urls.length ? urls : [null];
  for (const u of pages) {
    if (u) {
      if (!u.startsWith(origin + '/')) { console.log('BLOQUEADA (fora da origem):', u); continue; }
      await send('Page.navigate', { url: u }); await sleep(7000);
    }
    const r = await ev(expr);
    console.log('PAGE', u || start, r.result?.result?.value ?? JSON.stringify(r.result));
  }
  await sleep(1500);
  console.log('NET', JSON.stringify(Object.values(R), null, 1));
  if (urls.length && start.startsWith(origin)) { await send('Page.navigate', { url: start }); await sleep(3000); }
  process.exit(0);
};
