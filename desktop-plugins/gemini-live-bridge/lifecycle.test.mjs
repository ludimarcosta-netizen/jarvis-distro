import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import { createRequire } from 'node:module'

function fixture(ui = {}) {
  const intervals = new Map()
  const timeouts = new Map()
  const calls = []
  const disposers = []
  const contributions = []
  const processors = []
  const events = []
  const contexts = []
  const startedAudio = []
  let stoppedAudio = 0
  let stoppedTracks = 0
  let now = 1000
  let poll = { session: 'live', input: [], output: [], audio: [], wake_seq: 1 }
  const ctx = {
    storage: { get: (_, fallback) => fallback },
    onDispose: fn => disposers.push(fn),
    register: c => contributions.push(c),
    rest: async (path, options) => {
      calls.push({ path, options })
      if (path === '/start') return { ok: true, model: 'test' }
      if (path === '/sentinel-mute') {
        poll = { ...poll, sentinel: { alive: true, muted: options.body.muted }, session: 'idle' }
        return { ok: true, sentinel: poll.sentinel }
      }
      if (path === '/listen-only') {
        poll = { ...poll, listen_only: options.body.enabled, session: 'idle' }
        return { ok: true, listen_only: options.body.enabled, sentinel: { alive: true, muted: false } }
      }
      if (path.startsWith('/poll')) return poll
      if (path === '/voice-identity/status') return { ok: true, voice_identity: poll.voice_identity }
      return { ok: true }
    }
  }
  class AudioContext {
    constructor() { this.closed = false; contexts.push(this) }
    currentTime = 0
    createAnalyser() { return { connect() {}, getByteTimeDomainData: buffer => buffer.fill(160) } }
    createBuffer(_, length, rate) { return { duration: length / rate, getChannelData: () => new Float32Array(length) } }
    createBufferSource() { return { connect() {}, start() { startedAudio.push(true) }, stop() { stoppedAudio++ } } }
    createMediaStreamSource() { return { connect() {} } }
    createScriptProcessor() {
      const processor = { connect() {}, disconnect() {}, onaudioprocess: null }
      processors.push(processor)
      return processor
    }
    async close() { this.closed = true }
  }
  const sandbox = {
    atom: value => ({ get: () => value, set: next => { value = next } }),
    useValue: atom => atom.get(),
    useId: () => 'test-tools-instance',
    useEffect() {},
    URL,
    jsx: (type, props) => ({ type, props }),
    jsxs: (type, props) => ({ type, props }),
    Popover: 'popover', PopoverTrigger: 'popover-trigger', PopoverContent: 'popover-content',
    Button: 'button', STATUSBAR_AREAS: { right: 'statusBar.right' },
    document: { createElement: () => ({ remove() {} }), head: { append() {} } },
    Date: class extends Date { static now() { return now } },
    setInterval: (fn, ms) => { intervals.set(ms, fn); return ms },
    clearInterval: id => intervals.delete(id),
    setTimeout: (fn, ms) => { timeouts.set(ms, fn); return ms },
    clearTimeout: id => timeouts.delete(id),
    AudioContext,
    window: { addEventListener() {}, dispatchEvent: event => events.push(event) },
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail } },
    atob: value => Buffer.from(value, 'base64').toString('binary'),
    btoa: value => Buffer.from(value, 'binary').toString('base64'),
    navigator: { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop() { stoppedTracks++ } }] }) } },
    ...ui,
  }
  const source = readFileSync(new URL('./plugin.js', import.meta.url), 'utf8')
    .replace(/^import .*$/gm, '').replace('export default {', 'globalThis.plugin = {')
  vm.runInNewContext(source + '\nglobalThis.makeLive = createLive; globalThis.VoiceIdentityPanel = VoiceIdentityPanel; globalThis.MediaPanel = MediaPanel; globalThis.MemoryPanel = MemoryPanel; globalThis.TaskHistoryPanel = TaskHistoryPanel; globalThis.ProactivityPanel = ProactivityPanel', sandbox)
  return { ctx, sandbox, intervals, calls, contributions, disposers, processors, events, contexts, startedAudio,
    stoppedAudio: () => stoppedAudio,
    stoppedTracks: () => stoppedTracks,
    advance: ms => { now += ms }, setPoll: value => { poll = { ...poll, ...value } } }
}

test('interrupção da voz local para os trechos já agendados sem fechar o microfone', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  await live.start()
  f.setPoll({ interrupt_seq: 0, audio: [Buffer.alloc(4800).toString('base64')] })
  await f.intervals.get(300)()
  assert.equal(f.startedAudio.length, 1)
  f.setPoll({ interrupt_seq: 1, audio: [] })
  await f.intervals.get(300)()
  assert.equal(f.stoppedAudio(), 1)
  assert.equal(f.stoppedTracks(), 0)
  assert.equal(live.status.get(), 'live')
  live.stop()
})

test('arquivos mantém resultado e prévia local; falha e fechar não deixam resposta atrasada reabrir painel', async () => {
  const f = fixture()
  const requests = []
  let delayed
  f.ctx.rest = async (path, { body }) => {
    requests.push({ path, ...body })
    if (body.action === 'start') return { ok: true, session: 'isolated' }
    if (body.action === 'files.search') return { ok: true, results: [{ ref: 'r1', name: 'arquivo.txt' }], partial: false }
    if (body.action === 'files.preview') return await new Promise(resolve => { delayed = resolve })
    return { ok: false, error: 'Arquivo indisponível.' }
  }
  const live = f.sandbox.makeLive(f.ctx)
  live.filesToggle()
  live.setFilesQuery('arquivo')
  await live.filesAction('files.search')
  assert.equal(live.filesUI.get().results[0].ref, 'r1')
  assert.equal(live.filesUI.get().busy, false)
  await live.filesAction('files.open', 'r1')
  assert.equal(live.filesUI.get().error, 'Arquivo indisponível.')
  const preview = live.filesAction('files.preview', 'r1')
  await Promise.resolve()
  live.filesToggle()
  delayed({ ok: true, text: 'conteúdo tardio', name: 'arquivo.txt' })
  await preview
  assert.equal(live.filesUI.get().open, false)
  assert.equal(live.filesUI.get().preview, null)
  assert.equal(live.filesUI.get().busy, false)
  assert.ok(requests.every(request => request.path === '/workspace'))
  assert.equal(requests.find(request => request.action === 'files.open').ref, 'r1')
})

test('painel de arquivos escapa conteúdo e mostra busca parcial em vez de executar HTML', () => {
  const require = createRequire(new URL('../../hermes-agent/package.json', import.meta.url))
  const React = require('react')
  const { renderToStaticMarkup } = require('react-dom/server')
  const f = fixture({ ...require('react/jsx-runtime') })
  const live = f.sandbox.makeLive(f.ctx)
  live.filesUI.set({ open: true, query: 'arquivo', busy: false, error: '',
    notice: 'Busca parcial.', results: [{ ref: 'r1', name: '<script>arquivo</script>', location: 'Downloads/arquivo.txt' }],
    preview: { name: 'arquivo.txt', text: '<script>alert(1)</script>', notice: 'Dados de referência.', truncated: true } })
  const html = renderToStaticMarkup(React.createElement(f.sandbox.FilesPanel, { live }))
  assert.ok(!html.includes('<script>'))
  assert.ok(html.includes('&lt;script&gt;alert(1)&lt;/script&gt;'))
  assert.ok(html.includes('Prévia truncada.'))
  assert.ok(html.includes('Voltar aos arquivos encontrados'))
  assert.ok(!html.includes('Nome do arquivo'))
  assert.ok(!html.includes('Downloads/arquivo.txt'))
  live.filesUI.set({ ...live.filesUI.get(), preview: null })
  const results = renderToStaticMarkup(React.createElement(f.sandbox.FilesPanel, { live }))
  assert.ok(results.includes('Busca parcial.'))
  assert.ok(results.includes('Nome do arquivo'))
  assert.ok(results.includes('&lt;script&gt;arquivo&lt;/script&gt;'))
})

test('trocar perfil ou conversa limpa os resultados locais do painel', () => {
  const changed = []
  const f = fixture({ host: { state: { profile: { listen: callback => {
    changed.push(callback)
    return () => {}
  } } } } })
  const live = f.sandbox.makeLive(f.ctx)
  live.filesUI.set({ open: true, query: 'privado', busy: false, results: [{ ref: 'secret' }], preview: { text: 'privado' } })
  changed.forEach(callback => callback())
  assert.equal(live.filesUI.get().open, false)
  assert.equal(live.filesUI.get().query, '')
  assert.equal(live.filesUI.get().results.length, 0)
  assert.equal(live.filesUI.get().preview, null)
})

test('player de vídeo preservado usa host local, oferece controles e fecha', async () => {
  const effects = []
  const f = fixture({ useEffect: effect => effects.push(effect), window: {
    addEventListener() {},
    hermesDesktop: { getEmbedHostOrigin: async () => 'http://127.0.0.1:43123' }
  } })
  const render = () => {
    const [button, layer] = f.sandbox.AnthropicMusic().props.children
    return [button, layer && layer.props.children]
  }
  let [button, panel] = render()
  assert.equal(panel, false)
  assert.equal(button.props['aria-expanded'], false)
  button.props.onClick()
  ;[button, panel] = render()
  assert.equal(button.props['aria-expanded'], true)
  assert.equal(panel.props.id, button.props['aria-controls'])
  f.sandbox.AnthropicMusic({ playerOnly: true })
  effects.at(-1)()
  await new Promise(resolve => setImmediate(resolve))
  ;[button, panel] = render()
  const iframe = panel.props.children.find(child => child.type === 'iframe')
  assert.equal(iframe.props.src, 'http://127.0.0.1:43123/youtube/tRsQsTMvPNg?rel=0')
  assert.equal(iframe.props.referrerPolicy, 'strict-origin-when-cross-origin')
  assert.ok(!iframe.props.src.includes('controls=0'))
  assert.equal(panel.props.children.find(child => child.type === 'a').props.href,
    'https://www.youtube.com/watch?v=tRsQsTMvPNg')
  button.props.onClick()
  assert.equal(render()[1], false)
  assert.deepEqual(f.calls, [])
  render()[0].props.onClick()
  f.sandbox.plugin.register(f.ctx)
  f.disposers.forEach(dispose => dispose())
  assert.equal(render()[1], false)
})

test('música ambiente inicia e para no navegador sem abrir player na interface', async () => {
  const f = fixture()
  f.ctx.rest = async (path, { body }) => {
    f.calls.push(body)
    return body.action === 'start' ? { ok: true, session: 'music-session' }
      : { ok: true, playing: body.action === 'music.play' }
  }
  const live = f.sandbox.makeLive(f.ctx)
  const render = () => f.sandbox.AmbientMusicButton({ live }).props.children
  assert.equal(render()[0].props.children, '▶ Play')
  await live.music.toggle()
  assert.equal(render()[0].props.children, '■ Parar')
  assert.equal(f.sandbox.AnthropicMusic({ playerOnly: true }), null)
  live.toolsOpen.set(false)
  assert.equal(live.music.state.get().playing, true)
  await live.music.toggle()
  assert.equal(render()[0].props.children, '▶ Play')
  assert.equal(f.calls.filter(c => c.action === 'music.play').length, 1)
  assert.equal(f.calls.filter(c => c.action === 'music.stop').length, 1)
})

test('música bloqueia cliques duplicados e não inventa reprodução quando backend falha', async () => {
  const f = fixture()
  let finish
  f.ctx.rest = async (path, { body }) => body.action === 'start' ? { ok: true, session: 'music' }
    : await new Promise(resolve => { finish = resolve })
  const live = f.sandbox.makeLive(f.ctx)
  const first = live.music.toggle()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(live.music.state.get().busy, true)
  await live.music.toggle()
  finish({ ok: false, error: 'Não iniciou.' })
  await first
  assert.equal(live.music.state.get().playing, false)
  assert.equal(live.music.state.get().error, 'Não iniciou.')
  assert.equal(f.sandbox.AmbientMusicButton({ live }).props.children[1].props.role, 'alert')
})

test('player rejeita origens externas e file sem ponte; permite renderer web', async () => {
  for (const origin of ['https://example.com', 'http://localhost:1234', 'http://127.0.0.1:1234/path', 'http://user@127.0.0.1:1234', 'http://127.0.0.1:1234?token=secret']) {
    const f = fixture({ window: { hermesDesktop: { getEmbedHostOrigin: async () => origin } } })
    await assert.rejects(f.sandbox.musicPlayerSrc(), /Origem local/)
  }
  await assert.rejects(fixture({ window: { location: { protocol: 'file:' } } }).sandbox.musicPlayerSrc(), /indisponível/)
  const web = fixture({ window: { location: { protocol: 'https:' } } })
  assert.match(await web.sandbox.musicPlayerSrc(), /^https:\/\/www.youtube-nocookie.com\/embed\//)
})

test('fechar durante carregamento descarta resposta tardia e falha mantém link alternativo', async () => {
  const effects = []
  let resolveHost
  const f = fixture({ useEffect: effect => effects.push(effect), window: {
    hermesDesktop: { getEmbedHostOrigin: () => new Promise(resolve => { resolveHost = resolve }) }
  } })
  f.sandbox.AnthropicMusic({ triggerOnly: true }).props.children[0].props.onClick()
  const render = () => f.sandbox.AnthropicMusic({ playerOnly: true }).props.children[1].props.children
  render()
  const cleanup = effects.at(-1)()
  cleanup()
  resolveHost('http://127.0.0.1:43123')
  await new Promise(resolve => setImmediate(resolve))
  assert.ok(!render().props.children.some(child => child?.type === 'iframe'))
  f.sandbox.window.hermesDesktop.getEmbedHostOrigin = async () => { throw Error('IPC unavailable') }
  effects.at(-1)()
  await new Promise(resolve => setImmediate(resolve))
  const panel = render()
  assert.match(panel.props.children.find(child => child?.props?.role === 'status').props.children, /Não consegui/)
  assert.ok(panel.props.children.some(child => child?.type === 'a'))
  panel.props.children[0].props.children[1].props.onClick()
  assert.equal(f.sandbox.AnthropicMusic({ playerOnly: true }), null)
})

test('Só ouvir corta áudio enfileirado, mantém o controle ambiente e bloqueia voz e wake', async () => {
  const f = fixture()
  f.sandbox.plugin.register(f.ctx)
  const contribution = f.contributions.find(c => c.area === 'jarvis.voice.controls')
  const live = contribution.render().props.live
  await f.intervals.get(1200)()
  await live.start()
  f.setPoll({ audio: [Buffer.alloc(4800).toString('base64')] })
  await f.intervals.get(300)()
  assert.equal(f.startedAudio.length, 1)
  const button = contribution.render().type({ live }).props.children.find(c => c?.props['aria-label'] === 'Só ouvir')
  await button.props.onClick()
  assert.equal(live.listenOnly.get(), true)
  assert.equal(live.status.get(), 'idle')
  assert.ok(f.contexts.every(context => context.closed))
  assert.equal(live.sentinel.get().muted, false)
  assert.ok(!f.calls.some(c => c.path === '/sentinel-mute'))
  f.setPoll({ wake_seq: 2, audio: [Buffer.alloc(4800).toString('base64')] })
  await f.intervals.get(1200)()
  await live.start()
  assert.equal(f.calls.filter(c => c.path === '/start').length, 1)
  assert.equal(f.startedAudio.length, 1)
  await live.setListenOnly(false)
  await live.start()
  assert.equal(f.calls.filter(c => c.path === '/start').length, 2)
})

test('Só ouvir também corta uma sessão que ainda está conectando', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  const rest = f.ctx.rest
  let finish
  f.ctx.rest = (path, options) => path === '/start'
    ? new Promise(resolve => { finish = resolve }) : rest(path, options)
  const connecting = live.start()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(live.status.get(), 'connecting')
  await live.setListenOnly(true)
  finish({ ok: true, model: 'test' })
  await connecting
  assert.equal(live.status.get(), 'idle')
  assert.ok(f.contexts.every(context => context.closed))
  assert.ok(!f.intervals.has(300))
})

test('modo salvo pelo backend bloqueia voz ao reabrir a interface', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  live.startWakeWatch()
  f.setPoll({ listen_only: true, session: 'idle' })
  await f.intervals.get(1200)()
  await live.start()
  assert.equal(live.listenOnly.get(), true)
  assert.equal(f.processors.length, 0)
  assert.equal(f.calls.filter(c => c.path === '/start').length, 0)
})

test('ativação durante a abertura do microfone libera a captura atrasada', async () => {
  const f = fixture()
  let finish
  f.sandbox.navigator.mediaDevices.getUserMedia = () => new Promise(resolve => { finish = resolve })
  const live = f.sandbox.makeLive(f.ctx)
  const connecting = live.start()
  await live.setListenOnly(true)
  let tracksStopped = 0
  finish({ getTracks: () => [{ stop() { tracksStopped++ } }] })
  await connecting
  assert.equal(tracksStopped, 1)
  assert.ok(f.contexts.every(context => context.closed))
  assert.equal(f.calls.filter(c => c.path === '/start').length, 0)
})

test('mouth level follows played audio and resets after stopping', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  await live.start()
  f.setPoll({ audio: [Buffer.alloc(4800).toString('base64')] })
  await f.intervals.get(300)()
  f.intervals.get(50)()
  const audio = f.events.findLast(event => event.type === 'jarvis:voice-level')
  assert.equal(audio.detail.level, 1)
  live.stop()
  f.intervals.get(50)()
  assert.equal(f.events.findLast(event => event.type === 'jarvis:voice-level').detail.level, null)
})

test('Jarvis button starts Gemini; 30s quiet closes it and releases idle wake watcher on disposal', async () => {
  const f = fixture()
  f.sandbox.plugin.register(f.ctx)
  const contribution = f.contributions.find(c => c.area === 'jarvis.voice.controls')
  assert.ok(contribution)
  const live = contribution.render().props.live
  const tree = contribution.render().type({ live })
  await tree.props.children[1].props.onClick()
  assert.ok(f.calls.some(c => c.path === '/start'))
  assert.equal(live.status.get(), 'live')
  f.advance(30_001)
  await f.intervals.get(300)()
  assert.equal(live.status.get(), 'idle')
  assert.ok(f.calls.some(c => c.path === '/close'))
  f.disposers.forEach(fn => fn())
  assert.equal(f.intervals.size, 0)
})

test('cold preparation does not consume the live conversation silence budget', async () => {
  const f = fixture()
  const rest = f.ctx.rest
  let ready
  f.ctx.rest = (path, options) => path === '/start'
    ? new Promise(resolve => { ready = resolve }) : rest(path, options)
  const live = f.sandbox.makeLive(f.ctx)
  const connecting = live.start()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(live.status.get(), 'connecting')
  f.advance(90_000)
  ready({ ok: true, model: 'Hermes · F5-TTS' })
  await connecting
  await f.intervals.get(300)()
  assert.equal(live.status.get(), 'live')
  f.advance(29_999)
  await f.intervals.get(300)()
  assert.equal(live.status.get(), 'live')
  f.advance(1)
  await f.intervals.get(300)()
  assert.equal(live.status.get(), 'idle')
})

test('opening Jarvis requests background warmup once without starting microphone or conversation', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  live.startWakeWatch()
  live.startWakeWatch()
  assert.equal(f.calls.filter(c => c.path === '/warmup').length, 1)
  assert.equal(f.calls.filter(c => c.path === '/start').length, 0)
  assert.equal(f.contexts.length, 0)
  f.disposers.forEach(fn => fn())
})

test('speech and delegated work keep Live alive; completed wake does not reopen it', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  live.startWakeWatch()
  await f.intervals.get(1200)()
  await live.start()
  f.advance(30_001)
  f.setPoll({ input: ['Olá'], wake_seq: 2 })
  await f.intervals.get(300)()
  assert.equal(live.status.get(), 'live')
  f.advance(30_001)
  f.setPoll({ input: [], delegation: { status: 'running' } })
  await f.intervals.get(300)()
  assert.equal(live.status.get(), 'live')
  live.stop()
  await f.intervals.get(1200)()
  assert.equal(f.calls.filter(c => c.path === '/start').length, 1)
})

test('explicit farewell phrases stop the mic, let Gemini answer, then return cleanly to idle', async () => {
  for (const phrase of ['Tchau, Jarvis.', 'Até mais Jarvis', 'Pode encerrar', 'Pode dormir', 'Obrigado Jarvis, até mais',
    'Jarvis, encerrar sessão.', 'Encerrar a conversa', 'Pode encerrar a sessão, Jarvis', 'Finalizar sessão',
    'Tchau', 'Até logo', 'Por hoje é só, Jarvis', 'Era só isso, obrigado']) {
    const f = fixture()
    const live = f.sandbox.makeLive(f.ctx)
    await live.start()

    f.setPoll({ input: [phrase] })
    await f.intervals.get(300)()
    assert.ok(f.calls.some(c => c.path === '/farewell?explicit=true'), phrase)
    assert.equal(f.stoppedTracks(), 1, phrase)

    f.setPoll({ session: 'idle', input: [] })
    await f.intervals.get(300)()
    assert.equal(live.status.get(), 'idle', phrase)
  }
})

test('frases que apenas contêm termos de encerramento não encerram a conversa', async () => {
  for (const phrase of ['Jarvis, encerrar reunião', 'Encerra a sessão do Zoom', 'Pode encerrar o processo travado',
    'Qual a diferença entre tchau e adeus']) {
    const f = fixture()
    const live = f.sandbox.makeLive(f.ctx)
    await live.start()
    f.setPoll({ input: [phrase] })
    await f.intervals.get(300)()
    assert.ok(!f.calls.some(c => c.path.startsWith('/farewell')), phrase)
    assert.equal(live.status.get(), 'live', phrase)
    live.stop()
  }
})

test('pending voice confirmation keeps Live alive without approving automatically', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  await live.start()
  f.advance(30_001)
  f.setPoll({ delegation: { status: 'confirming' }, voice_identity: { mode: 'protect', pending: { id: 'p' } } })
  await f.intervals.get(300)()
  assert.equal(live.status.get(), 'live')
  assert.equal(live.voiceIdentity.get().pending.id, 'p')
  assert.equal(f.calls.filter(c => c.path.includes('/decision')).length, 0)
  live.stop()
})

test('screen decision sends the exact immutable request identity', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  await live.voiceAction('decision', { id: 'one-use', digest: 'request-digest', allow: true })
  const call = f.calls.find(c => c.path === '/voice-identity/decision')
  assert.equal(call.options.body.id, 'one-use')
  assert.equal(call.options.body.digest, 'request-digest')
  assert.equal(call.options.body.allow, true)
  assert.equal(live.voiceBusy.get(), false)
})

test('guided voice enrollment captures locally without starting Gemini Live', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  await live.startEnrollment()
  assert.equal(live.status.get(), 'idle')
  assert.equal(f.calls.filter(c => c.path === '/start').length, 0)
  assert.equal(f.calls.filter(c => c.path === '/voice-identity/enrollment').length, 1)
  assert.equal(f.processors.length, 1)

  const input = new Float32Array(4096).fill(0.25)
  for (let i = 0; i < 20; i++) {
    f.processors[0].onaudioprocess({ inputBuffer: { length: input.length, getChannelData: () => input } })
  }
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(f.calls.filter(c => c.path === '/voice-identity/enrollment-audio').length, 0)
  f.processors[0].onaudioprocess({ inputBuffer: { length: input.length, getChannelData: () => input } })
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(f.calls.filter(c => c.path === '/voice-identity/enrollment-audio').length, 1)

  f.setPoll({ voice_identity: { available: true, enrollment: { state: 'ready' } } })
  await f.intervals.get(500)()
  assert.equal(f.stoppedTracks(), 1)
})

test('voice panel renders consent, pending decision and escaped profile names with React', () => {
  const require = createRequire(new URL('../../hermes-agent/package.json', import.meta.url))
  const React = require('react')
  const { renderToStaticMarkup } = require('react-dom/server')
  const pass = ({ children }) => React.createElement(React.Fragment, null, children)
  const f = fixture({ ...require('react/jsx-runtime'), Popover: pass, PopoverTrigger: pass,
    PopoverContent: ({ children, className }) => React.createElement('div', { className }, children) })
  const live = f.sandbox.makeLive(f.ctx)
  live.voiceIdentity.set({ mode: 'protect', ready: true, profiles: [{ id: 'p', name: '<script>alert(1)</script>' }],
    candidates: [{ id: 'c', created: 1000, seconds: 21.4, enrollment_ready: true }],
    pending: { id: 'r', digest: 'd', request: 'Pedido', remaining_s: 80, screen_only: true } })
  const html = renderToStaticMarkup(React.createElement(f.sandbox.VoiceIdentityPanel, { live }))
  assert.ok(html.includes('Confirmar este pedido uma vez'))
  assert.match(html, /<input(?=[^>]*name="consent")(?=[^>]*required="")[^>]*>/)
  assert.ok(html.includes('&lt;script&gt;'))
  assert.ok(!html.includes('<script>'))
})

test('ambient drafts show saved clips before explicit identification and consent', () => {
  const require = createRequire(new URL('../../hermes-agent/package.json', import.meta.url))
  const React = require('react')
  const { renderToStaticMarkup } = require('react-dom/server')
  const pass = ({ children }) => React.createElement(React.Fragment, null, children)
  const f = fixture({ ...require('react/jsx-runtime'), Popover: pass, PopoverTrigger: pass,
    PopoverContent: ({ children, className }) => React.createElement('div', { className }, children) })
  const live = f.sandbox.makeLive(f.ctx)
  live.voiceIdentity.set({ mode: 'protect', ready: true, profiles: [],
    candidates: [{ id: 'c', created: 1000, seconds: 4 }] })
  const html = renderToStaticMarkup(React.createElement(f.sandbox.VoiceIdentityPanel, { live }))
  assert.ok(html.includes('Trechos da conversa são salvos automaticamente.'))
  assert.ok(html.includes('Identificar pessoa'))
  assert.ok(html.includes('mais trechos serão reunidos'))
  assert.ok(html.includes('Ouvir amostra'))
  assert.ok(html.includes('Excluir amostra'))
  assert.ok(!html.includes('name="consent"'))
  assert.ok(!html.includes('Registrar voz'))
})


test('captação inicia ouvindo, pausa pelo botão compartilhado e só retoma por clique', async () => {
  const f = fixture()
  f.sandbox.plugin.register(f.ctx)
  const contribution = f.contributions.find(c => c.area === 'jarvis.voice.controls')
  const live = contribution.render().props.live
  const button = () => contribution.render().type({ live }).props.children.find(c => c?.props['aria-label'] === 'Captação de áudio')
  assert.equal(live.captureMuted.get(), false)
  assert.equal(button().props['aria-pressed'], true)
  await live.start()
  await button().props.onClick()
  assert.equal(live.captureMuted.get(), true)
  assert.equal(button().props['aria-pressed'], false)
  assert.equal(live.status.get(), 'idle')
  assert.ok(f.stoppedTracks() > 0)
  assert.ok(f.contexts.every(context => context.closed))
  await f.intervals.get(1200)()
  f.setPoll({ wake_seq: 2 })
  await f.intervals.get(1200)()
  await live.start()
  assert.equal(f.calls.filter(c => c.path === '/start').length, 1)
  await button().props.onClick()
  assert.equal(live.captureMuted.get(), false)
  await live.start()
  assert.equal(f.calls.filter(c => c.path === '/start').length, 2)
  assert.equal(f.sandbox.makeLive(f.ctx).captureMuted.get(), false)
})

test('pausa durante abertura do microfone fecha a captura atrasada', async () => {
  const f = fixture()
  let finish
  f.sandbox.navigator.mediaDevices.getUserMedia = () => new Promise(resolve => { finish = resolve })
  const live = f.sandbox.makeLive(f.ctx)
  const connecting = live.start()
  await live.setCaptureMuted(true)
  finish({ getTracks: () => [{ stop() {} }] })
  await connecting
  assert.equal(f.calls.filter(c => c.path === '/start').length, 0)
  assert.equal(live.status.get(), 'idle')
  assert.ok(f.contexts.every(context => context.closed))
})

test('falha na pausa fica visível e não inventa confirmação do sentinel', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  const rest = f.ctx.rest
  f.ctx.rest = (path, options) => path === '/sentinel-mute'
    ? Promise.resolve({ ok: false, error: 'sentinel indisponível' }) : rest(path, options)
  await live.setCaptureMuted(true)
  assert.equal(live.message.get(), 'sentinel indisponível')
  assert.equal(live.captureBusy.get(), false)
  assert.equal(live.sentinel.get(), false)
})

test('pausa interrompe cadastro de voz e bloqueia nova gravação', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  await live.startEnrollment()
  await live.setCaptureMuted(true)
  const tracks = f.stoppedTracks()
  assert.ok(tracks > 0)
  const processors = f.processors.length
  await live.startEnrollment()
  assert.equal(f.processors.length, processors)
})


test('mídia ignora resposta atrasada após fechar e impede cliques duplicados', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  let finish
  const actions = []
  f.ctx.rest = async (path, options) => {
    actions.push(options.body.action)
    if (options.body.action === 'start') return { ok: true, session: 'media-one' }
    return new Promise(resolve => { finish = resolve })
  }
  live.media.toggle()
  live.media.query('Anthropic')
  const pending = live.media.action('videos.search')
  await Promise.resolve()
  await live.media.action('videos.search')
  assert.equal(actions.filter(a => a === 'videos.search').length, 1)
  live.media.toggle()
  finish({ ok: true, results: [{ ref: 'late', title: 'late' }] })
  await pending
  assert.equal(live.media.state.get().open, false)
  assert.equal(live.media.state.get().results.length, 0)
  assert.equal(live.media.state.get().busy, false)
})

test('mídia apresenta falha sem repetir o comando', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  let commands = 0
  f.ctx.rest = async (path, options) => {
    if (options.body.action === 'start') return { ok: true, session: 'media-two' }
    commands++
    return { ok: false, error: 'Não confirmado pelo aplicativo' }
  }
  live.media.toggle()
  await live.media.action('media.command', 'ref', 'play')
  assert.equal(commands, 1)
  assert.equal(live.media.state.get().error, 'Não confirmado pelo aplicativo')
  assert.equal(live.media.state.get().busy, false)
})

test('cancelar tarefa só chama o backend com tarefa em execução e mostra a resposta', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  const paths = []
  f.ctx.rest = async path => {
    paths.push(path)
    return { ok: true, message: 'Interrupção confirmada pelo Hermes. Ações já executadas não são desfeitas.' }
  }
  await live.cancelTask()
  assert.equal(paths.length, 0)
  live.delegation.set({ status: 'running', request: 'tarefa longa' })
  await Promise.all([live.cancelTask(), live.cancelTask()])
  assert.deepEqual(paths, ['/delegation-cancel'])
  assert.match(live.message.get(), /não são desfeitas/)
  assert.equal(live.cancelBusy.get(), false)
})

test('volume da mídia valida o nível antes de chamar o backend', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  const sent = []
  f.ctx.rest = async (path, options) => {
    sent.push(options.body)
    if (options.body.action === 'start') return { ok: true, session: 'media-volume' }
    return { ok: true, volume: 30, message: 'Volume do aplicativo confirmado em 30%.' }
  }
  live.media.toggle()
  live.media.volume('150')
  await live.media.action('media.volume', 'ref')
  assert.equal(sent.length, 0)
  assert.match(live.media.state.get().error, /0 a 100/)
  live.media.volume('30')
  await live.media.action('media.volume', 'ref')
  assert.equal(sent.at(-1).level, 30)
  assert.equal(sent.at(-1).ref, 'ref')
  assert.equal(live.media.state.get().notice, 'Volume do aplicativo confirmado em 30%.')
})


test('painel de mídia renderiza resultados escapados e expõe comandos acessíveis', () => {
  const require = createRequire(new URL('../../hermes-agent/package.json', import.meta.url))
  const React = require('react')
  const { renderToStaticMarkup } = require('react-dom/server')
  const f = fixture({ ...require('react/jsx-runtime') })
  const live = f.sandbox.makeLive(f.ctx)
  live.media.state.set({ open: true, busy: false, query: 'Anthropic',
    results: [{ ref: 'v', title: '<script>bad()</script>', channel: 'Anthropic' }],
    sessions: [{ ref: 's', app: 'Spotify.exe', status: 'Paused' }],
    error: '', notice: 'Escolha um vídeo', fallback: '' })
  const html = renderToStaticMarkup(React.createElement(f.sandbox.MediaPanel, { live }))
  assert.ok(html.includes('&lt;script&gt;bad()&lt;/script&gt;'))
  assert.ok(!html.includes('<script>'))
  assert.ok(html.includes('Abrir este vídeo'))
  assert.ok(html.includes('Pausar'))
  assert.ok(html.includes('Aplicar volume'))
  assert.ok(html.includes('Tocar no Spotify'))
  assert.ok(html.includes('aria-label="Volume do aplicativo"'))
  assert.ok(html.includes('aria-label="Buscar vídeo ou música"'))
})


test('revisão de memória exige preparar e confirmar, e editar invalida confirmação', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  const sent = []
  f.ctx.rest = async (path, options) => {
    const body = options.body
    sent.push(body)
    if (body.action === 'start') return { ok: true, session: 'memory-one' }
    if (body.action === 'memory.select') return { ok: true, source: 'projetos/Teste.md', text: 'original', editable: true, state: 'active', history: [] }
    if (body.action === 'memory.prepare') return { ok: true, ticket: 'bound-ticket', source: 'projetos/Teste.md', change: body.change, text: body.text, reason: body.reason }
    return { ok: true, message: 'Revisão salva' }
  }
  live.memory.toggle()
  await live.memory.action('memory.select', 'found-ref')
  live.memory.update({ text: 'correção', reason: 'fonte explícita' })
  await live.memory.action('memory.confirm')
  assert.equal(sent.filter(row => row.action === 'memory.confirm').length, 0)
  await live.memory.action('memory.prepare', null, 'correct')
  assert.equal(live.memory.state.get().prepared.ticket, 'bound-ticket')
  live.memory.update({ text: 'outra correção' })
  assert.equal(live.memory.state.get().prepared, null)
  await live.memory.action('memory.prepare', null, 'correct')
  await live.memory.action('memory.confirm')
  assert.equal(sent.filter(row => row.action === 'memory.confirm').length, 1)
  assert.equal(sent.at(-1).ticket, 'bound-ticket')
  assert.equal(live.memory.state.get().selected, null)
})

test('fechar memória descarta resultados atrasados e nunca os transforma em confirmação', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  let finish
  let ready
  const waiting = new Promise(resolve => { ready = resolve })
  f.ctx.rest = (path, options) => options.body.action === 'start' ? Promise.resolve({ ok: true, session: 'm' })
    : new Promise(resolve => { finish = resolve; ready() })
  live.memory.toggle()
  live.memory.update({ query: 'teste' })
  const pending = live.memory.action('memory.search')
  await waiting
  live.memory.toggle()
  finish({ ok: true, results: [{ ref: 'late', excerpt: 'privado' }] })
  await pending
  assert.equal(live.memory.state.get().open, false)
  assert.equal(live.memory.state.get().results.length, 0)
  assert.equal(live.memory.state.get().prepared, null)
})

test('trocar perfil limpa memória, rascunho e confirmação', () => {
  const callbacks = []
  const g = fixture({ host: { state: { profile: { listen: callback => { callbacks.push(callback); return () => {} } } } } })
  const owned = g.sandbox.makeLive(g.ctx)
  owned.memory.toggle()
  owned.memory.update({ text: 'privado', selected: {}, ref: 'r' })
  callbacks.forEach(callback => callback())
  assert.equal(owned.memory.state.get().open, false)
  assert.equal(owned.memory.state.get().text, '')
  assert.equal(owned.memory.state.get().ref, '')
  assert.equal(owned.memory.state.get().prepared, null)
})

test('memória escapa conteúdo e mostra fonte, histórico e confirmação explícita', () => {
  const require = createRequire(new URL('../../hermes-agent/package.json', import.meta.url))
  const React = require('react')
  const { renderToStaticMarkup } = require('react-dom/server')
  const f = fixture({ ...require('react/jsx-runtime') })
  const live = f.sandbox.makeLive(f.ctx)
  live.memory.state.set({ ...live.memory.state.get(), open: true, text: '<script>bad()</script>', reason: 'revisão',
    selected: { source: 'registros/teste.md', editable: true, state: 'active', history: [] },
    prepared: { ticket: 't', source: 'registros/teste.md', change: 'correct', text: '<script>bad()</script>', reason: 'revisão' } })
  const html = renderToStaticMarkup(React.createElement(f.sandbox.MemoryPanel, { live }))
  assert.ok(html.includes('&lt;script&gt;bad()&lt;/script&gt;'))
  assert.ok(!html.includes('<script>'))
  assert.ok(html.includes('Confirmar revisão'))
  assert.ok(html.includes('nota inteira'))
  assert.ok(html.includes('Histórico de revisões'))
  assert.ok(html.includes('aria-label="Motivo da revisão"'))
})


test('histórico durável aparece mesmo sem sessão de voz e escapa conteúdo', async () => {
  const require = createRequire(new URL('../../hermes-agent/package.json', import.meta.url))
  const React = require('react')
  const { renderToStaticMarkup } = require('react-dom/server')
  const f = fixture({ ...require('react/jsx-runtime') })
  const live = f.sandbox.makeLive(f.ctx)
  f.setPoll({ session: null, task_history: { tasks: [{ id: 'one', status: 'uncertain', request: '<script>bad()</script>', reply: 'Confira antes de repetir.' }], error: '' } })
  live.startWakeWatch()
  await f.intervals.get(1200)()
  assert.equal(live.taskHistory.get().tasks[0].status, 'uncertain')
  const before = live.taskHistory.get()
  await f.intervals.get(1200)()
  assert.equal(live.taskHistory.get(), before, 'poll idêntico não recria o estado')
  const html = renderToStaticMarkup(React.createElement(f.sandbox.TaskHistoryPanel, { live }))
  assert.ok(html.includes('Resultado incerto'))
  assert.ok(html.includes('&lt;script&gt;bad()&lt;/script&gt;'))
  assert.ok(!html.includes('<script>'))
  assert.ok(html.includes('antes de repetir'))
  assert.ok(!f.calls.some(c => c.path === '/start'), 'consultar histórico não abre voz nem reexecuta')
})

test('histórico indisponível mostra erro sem esconder falha', () => {
  const require = createRequire(new URL('../../hermes-agent/package.json', import.meta.url))
  const React = require('react')
  const { renderToStaticMarkup } = require('react-dom/server')
  const f = fixture({ ...require('react/jsx-runtime') })
  const live = f.sandbox.makeLive(f.ctx)
  live.taskHistory.set({ tasks: [], error: 'Histórico indisponível.' })
  const html = renderToStaticMarkup(React.createElement(f.sandbox.TaskHistoryPanel, { live }))
  assert.ok(html.includes('role="alert"'))
  assert.ok(html.includes('Histórico indisponível.'))
})


test('troca de perfil descarta histórico anterior e resposta atrasada', async () => {
  const listeners = []
  const scope = { listen: fn => { listeners.push(fn); return () => {} } }
  const f = fixture({ host: { state: { profile: scope, connectionId: scope, focusedSessionId: scope } } })
  const live = f.sandbox.makeLive(f.ctx)
  live.taskHistory.set({ tasks: [{ id: 'old' }], error: '' })
  let finish
  f.ctx.rest = () => new Promise(resolve => { finish = resolve })
  live.startWakeWatch()
  const pending = f.intervals.get(1200)()
  listeners.forEach(fn => fn())
  assert.equal(live.taskHistory.get().tasks.length, 0)
  finish({ session: null, task_history: { tasks: [{ id: 'late' }], error: '' } })
  await pending
  assert.equal(live.taskHistory.get().tasks.length, 0)
})


test('check-ins exigem salvar opt-in e preservam rascunho durante poll', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  const config = { enabled: false, interval_minutes: 60, start: '09:00', end: '18:00' }
  f.setPoll({ session: null, proactivity: { ok: true, config, status: 'Desativado', notice: null } })
  live.startWakeWatch()
  await f.intervals.get(1200)()
  assert.equal(live.proactivity.get().draft.enabled, false)
  live.editProactivity({ enabled: true })
  await f.intervals.get(1200)()
  assert.equal(live.proactivity.get().draft.enabled, true)
  assert.ok(!f.calls.some(c => c.path === '/proactivity'))
  const original = f.ctx.rest
  f.ctx.rest = async (path, options) => path === '/proactivity'
    ? { ok: true, config: options.body.config, status: 'Ativo', notice: null } : original(path, options)
  await live.proactivityAction('save')
  assert.equal(live.proactivity.get().config.enabled, true)
  assert.equal(live.proactivity.get().dirty, false)
})

test('check-ins renderizam controles acessíveis e aviso escapado', () => {
  const require = createRequire(new URL('../../hermes-agent/package.json', import.meta.url))
  const React = require('react')
  const { renderToStaticMarkup } = require('react-dom/server')
  const f = fixture({ ...require('react/jsx-runtime') })
  const live = f.sandbox.makeLive(f.ctx)
  live.proactivity.set({ draft: { enabled: false, interval_minutes: 60, start: '09:00', end: '18:00' },
    dirty: false, busy: false, error: '', notice: { id: 'x', text: '<script>bad()</script>' } })
  const html = renderToStaticMarkup(React.createElement(f.sandbox.ProactivityPanel, { live }))
  assert.ok(html.includes('Ativar check-ins'))
  assert.ok(html.includes('Salvar check-ins'))
  assert.ok(html.includes('type="time"'))
  assert.ok(html.includes('&lt;script&gt;bad()&lt;/script&gt;'))
  assert.ok(!html.includes('<script>'))
})


test('barra mantém voz e captação acessíveis e agrupa ferramentas sem submissão de formulário', async () => {
  const f = fixture()
  f.sandbox.plugin.register(f.ctx)
  const contribution = f.contributions.find(c => c.area === 'jarvis.voice.controls')
  const live = contribution.render().props.live
  const renderControls = () => contribution.render().type({ live })
  const tree = renderControls()
  assert.equal(tree.props.role, 'group')
  const buttons = tree.props.children.filter(node => node?.type === 'button')
  assert.equal(buttons.length, 3)
  assert.ok(buttons.every(node => node.props.type === 'button'))
  assert.ok(buttons.some(node => node.props['aria-label'] === 'Captação de áudio'))
  const toolsComponent = tree.props.children.find(node => node?.type?.name === 'JarvisTools')
  const tools = toolsComponent.type({ live })
  assert.equal(tools.props.children[0].props.children.props.children, 'Ferramentas')
  assert.equal(tools.props.children[1].props['aria-label'], 'Ferramentas do Jarvis')
  const panels = tools.props.children[1].props.children[1].props.children
  assert.equal(panels.length, 7)
  assert.ok(panels.every(panel => panel.type === 'button' && panel.props.type === 'button'))
  live.proactivity.set({ ...live.proactivity.get(), notice: { text: 'Revisar tarefa' } })
  const updatedTools = toolsComponent.type({ live })
  assert.equal(updatedTools.props.children[0].props.children.props.children, 'Ferramentas · aviso')
})


test('fechar ferramentas preserva player e botão separado permite parar a música', () => {
  const f = fixture()
  const music = f.sandbox.AnthropicMusic
  assert.equal(music({ playerOnly: true }), null)
  const trigger = () => music({ triggerOnly: true }).props.children[0]
  trigger().props.onClick()
  assert.equal(music({ triggerOnly: true }).props.children[1], false)
  assert.equal(music({ playerOnly: true }).props.children[1].props.children.props.id, 'jarvis-music-player')
  trigger().props.onClick()
  assert.equal(music({ playerOnly: true }), null)
})


test('ferramentas mostra um painel por vez e voltar fecha estados e descarta respostas antigas', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  live.toolsOpen.set(true)
  live.setToolsPage('files')
  assert.equal(live.filesUI.get().open, true)
  live.setFilesQuery('relatorio')
  let finish
  f.ctx.rest = async () => new Promise(resolve => { finish = resolve })
  const search = live.filesAction('files.search')
  live.setToolsPage('media')
  assert.equal(live.filesUI.get().open, false)
  assert.equal(live.media.state.get().open, true)
  finish({ ok: true, session: 'late' })
  await search
  assert.equal(live.filesUI.get().results.length, 0)
  live.setToolsPage('memory')
  assert.equal(live.media.state.get().open, false)
  assert.equal(live.memory.state.get().open, true)
  live.voiceSample.set('private-audio')
  live.setToolsPage('')
  assert.equal(live.memory.state.get().open, false)
  assert.equal(live.toolsPage.get(), '')
  assert.equal(live.voiceSample.get(), '')
  live.setToolsPage('files')
  f.disposers.forEach(dispose => dispose())
  assert.equal(live.toolsOpen.get(), false)
  assert.equal(live.toolsPage.get(), '')
})


test('somente a instância que abriu Ferramentas exibe o menu; fechamento de outra instância não interfere', () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  f.sandbox.useId = () => 'app'
  let app = f.sandbox.JarvisTools({ live })
  f.sandbox.useId = () => 'face'
  let face = f.sandbox.JarvisTools({ live })
  face.props.onOpenChange(true)
  assert.equal(live.toolsOwner.get(), 'face')
  app.props.onOpenChange(false)
  assert.equal(live.toolsOpen.get(), true)
  f.sandbox.useId = () => 'app'
  app = f.sandbox.JarvisTools({ live })
  f.sandbox.useId = () => 'face'
  face = f.sandbox.JarvisTools({ live })
  assert.equal(app.props.open, false)
  assert.equal(face.props.open, true)
  face.props.onOpenChange(false)
  assert.equal(live.toolsOpen.get(), false)
  assert.equal(live.toolsOwner.get(), null)
})


test('captions replace each speaker independently, suppress ordinary reply cards and clear after silence', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  await live.start()
  f.setPoll({ input: ['Primeiro pedido', '—'], output: ['Primeira resposta'], delegation: {status: 'done', request: 'Primeiro pedido', reply: 'Primeira resposta'} })
  await f.intervals.get(300)()
  assert.equal(live.captions.get().you, 'Primeiro pedido')
  assert.equal(live.captions.get().jarvis, 'Primeira resposta')
  assert.ok(!f.events.some(event => event.type === 'jarvis:reply'))
  f.setPoll({ input: ['Outro pedido', '—'], output: [], delegation: null })
  await f.intervals.get(300)()
  assert.equal(live.captions.get().you, 'Outro pedido')
  assert.equal(live.captions.get().jarvis, 'Primeira resposta')
  f.setPoll({ input: [], output: ['Nova resposta'] })
  await f.intervals.get(300)()
  assert.equal(live.captions.get().jarvis, 'Nova resposta')
  f.setPoll({ input: [], output: [] })
  f.advance(29999)
  await f.intervals.get(300)()
  assert.equal(live.status.get(), 'live')
  assert.equal(live.captions.get().you, 'Outro pedido')
  f.advance(1)
  await f.intervals.get(300)()
  assert.equal(live.status.get(), 'idle')
  assert.equal(live.captions.get().you, '')
  assert.equal(live.captions.get().jarvis, '')
})

test('partial hypotheses replace themselves and never remove the previous Jarvis response', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  await live.start()
  f.setPoll({ local_captions: true, input_partial: 'Organize' })
  await f.intervals.get(300)()
  assert.equal(live.captions.get().you, 'Organize')
  f.setPoll({ input_partial: 'Organize minha agenda' })
  await f.intervals.get(300)()
  assert.equal(live.captions.get().you, 'Organize minha agenda')
  f.setPoll({ input_partial: '', input: ['Organize a minha agenda.', '—'] })
  await f.intervals.get(300)()
  assert.equal(live.captions.get().you, 'Organize a minha agenda.')
})

test('caption words follow playback and queued long audio prevents premature timeout', async () => {
  const f = fixture()
  const live = f.sandbox.makeLive(f.ctx)
  await live.start()
  f.setPoll({ local_captions: true, audio: [Buffer.alloc(4800).toString('base64')], a_total: 1,
    speech_cues: [{offset: 0, text: 'Uma resposta longa', duration: 40, first: true}] })
  await f.intervals.get(300)()
  const playback = f.contexts[0]
  playback.currentTime = .03
  f.intervals.get(50)()
  assert.equal(live.captions.get().jarvis, 'Uma ')
  playback.currentTime = .1 + 40 / 3
  f.intervals.get(50)()
  assert.equal(live.captions.get().jarvis, 'Uma resposta ')
  playback.currentTime = 40
  f.intervals.get(50)()
  assert.equal(live.captions.get().jarvis, 'Uma resposta longa')
  // Real audio longer than 30s remains queued in WebAudio.
  f.setPoll({ input: [], output: [], speech_cues: [], audio: [Buffer.alloc(48000 * 40).toString('base64')], a_total: 2 })
  await f.intervals.get(300)()
  f.setPoll({ audio: [], speech_cues: [] })
  f.advance(30001)
  await f.intervals.get(300)()
  assert.equal(live.status.get(), 'live')
  playback.currentTime = 81
  f.intervals.get(50)()
  f.advance(30000)
  await f.intervals.get(300)()
  assert.equal(live.status.get(), 'idle')
})
