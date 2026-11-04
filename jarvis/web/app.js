// Página de reuniões do Jarvis. Sem dependências; o DOM é montado com textContent
// (a transcrição tem fala de terceiros e nunca vira HTML).
'use strict'

const TOKEN = document.querySelector('meta[name="jarvis-token"]').content
const TZ = 'America/Sao_Paulo'
const fmtTime = new Intl.DateTimeFormat('pt-BR', { timeZone: TZ, hour: '2-digit', minute: '2-digit' })
const fmtTimeSec = new Intl.DateTimeFormat('pt-BR', { timeZone: TZ, hour: '2-digit', minute: '2-digit', second: '2-digit' })
const fmtDay = new Intl.DateTimeFormat('pt-BR', { timeZone: TZ, weekday: 'long', day: '2-digit' })
const fmtMonth = new Intl.DateTimeFormat('pt-BR', { timeZone: TZ, month: 'long' })
const fmtFull = new Intl.DateTimeFormat('pt-BR', { timeZone: TZ, dateStyle: 'full' })
const keyParts = new Intl.DateTimeFormat('en-CA', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit' })

const state = { meetings: [], current: null, selected: new Set(), lastClicked: null, filter: '' }

// ── utilidades ────────────────────────────────────────────────────────────────
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag)
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v === null || v === undefined) continue
    if (k === 'class') node.className = v
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v)
    else if (k === 'text') node.textContent = v
    else node.setAttribute(k, v === true ? '' : v)
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) node.append(c instanceof Node ? c : String(c))
  return node
}

async function api(path, body) {
  const opts = { headers: { 'X-Jarvis-Token': TOKEN } }
  if (body !== undefined) {
    opts.method = 'POST'
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  }
  const res = await fetch('/api/' + path, opts)
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.error || `Erro ${res.status}`)
  return data
}

let toastTimer = null
function toast(message, kind = 'ok') {
  const box = document.getElementById('toast')
  box.textContent = message
  box.dataset.kind = kind
  box.hidden = false
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { box.hidden = true }, kind === 'error' ? 7000 : 3500)
}

async function guarded(button, label, fn) {
  const original = button ? button.textContent : ''
  if (button) { button.disabled = true; button.textContent = label }
  try { return await fn() } catch (err) { toast(err.message, 'error') } finally {
    if (button) { button.disabled = false; button.textContent = original }
  }
}

function dayKey(iso) { return keyParts.format(new Date(iso)) } // AAAA-MM-DD no fuso de Brasília
function span(m) { return `${fmtTime.format(new Date(m.started_at))}–${fmtTime.format(new Date(m.ended_at))}` }
function titleOf(m) { return m.title || m.ata_title || 'Reunião sem título' }
function statusBadge(m) {
  if (m.status === 'prompting') return el('span', { class: 'badge badge-prompt', text: 'aguardando confirmação' })
  if (m.status === 'open') return el('span', { class: 'badge badge-live', text: 'em andamento' })
  if (m.offline_status === 'pending') return el('span', { class: 'badge', text: 'áudio na fila' })
  if (m.offline_status === 'running') return el('span', { class: 'badge', text: 'processando áudio' })
  if (m.offline_status === 'error') return el('span', { class: 'badge badge-error', text: 'falha no áudio' })
  if (m.ata_status === 'error') return el('span', { class: 'badge badge-error', text: 'ata pendente' })
  if (m.ata_status === 'pending') return el('span', { class: 'badge', text: 'gerando ata' })
  return null
}

// ── árvore ano › mês › dia ───────────────────────────────────────────────────
function renderTree() {
  const nav = document.getElementById('tree')
  const q = state.filter.toLowerCase()
  const items = state.meetings.filter(m => !q || titleOf(m).toLowerCase().includes(q)
    || (m.participants || []).some(p => p.toLowerCase().includes(q)))
  nav.replaceChildren()
  if (!items.length) {
    nav.append(el('p', { class: 'muted pad', text: q ? 'Nenhuma reunião encontrada.' : 'Nenhuma reunião registrada ainda.' }))
    return
  }
  const years = new Map()
  for (const m of items) {
    const [y, mo, d] = dayKey(m.started_at).split('-')
    if (!years.has(y)) years.set(y, new Map())
    const months = years.get(y)
    if (!months.has(mo)) months.set(mo, new Map())
    const days = months.get(mo)
    if (!days.has(d)) days.set(d, [])
    days.get(d).push(m)
  }
  const firstYear = years.keys().next().value
  for (const [y, months] of years) {
    const total = [...months.values()].reduce((n, days) => n + [...days.values()].reduce((a, l) => a + l.length, 0), 0)
    const yearNode = el('details', { open: y === firstYear }, el('summary', {}, y, el('span', { class: 'count', text: String(total) })))
    let firstMonth = true
    for (const [, days] of months) {
      const sample = [...days.values()][0][0]
      const count = [...days.values()].reduce((a, l) => a + l.length, 0)
      const monthName = fmtMonth.format(new Date(sample.started_at))
      const monthNode = el('details', { open: y === firstYear && firstMonth },
        el('summary', {}, monthName.charAt(0).toUpperCase() + monthName.slice(1), el('span', { class: 'count', text: String(count) })))
      firstMonth = false
      for (const [, list] of days) {
        const dayName = fmtDay.format(new Date(list[0].started_at))
        monthNode.append(el('div', { class: 'day-label', text: dayName.charAt(0).toUpperCase() + dayName.slice(1) }))
        for (const m of list) {
          monthNode.append(el('button', {
            type: 'button', class: 'meeting-link', 'aria-current': state.current?.meeting.id === m.id ? 'true' : 'false',
            onclick: () => openMeeting(m.id),
          }, el('span', { class: 't', text: titleOf(m) }),
          el('span', { class: 'm' }, span(m), (m.participants || []).length ? `· ${m.participants.length} part.` : '', statusBadge(m))))
        }
      }
      yearNode.append(monthNode)
    }
    nav.append(yearNode)
  }
}

async function loadMeetings() {
  try {
    state.meetings = (await api('meetings')).meetings
    renderTree()
  } catch (err) {
    document.getElementById('tree').replaceChildren(el('p', { class: 'muted pad', role: 'alert', text: 'Não foi possível carregar: ' + err.message }))
  }
}

// ── detalhe da reunião ───────────────────────────────────────────────────────
async function openMeeting(id) {
  const detail = document.getElementById('detail')
  detail.replaceChildren(el('p', { class: 'muted', text: 'Carregando reunião…' }))
  try {
    state.current = await api('meetings/' + id)
    state.selected.clear()
    state.lastClicked = null
    renderDetail()
    renderTree()
    history.replaceState(null, '', '#' + id)
  } catch (err) {
    detail.replaceChildren(el('p', { role: 'alert', text: 'Não foi possível abrir a reunião: ' + err.message }))
  }
}

function renderDetail() {
  const { meeting: m, transcripts, ata, suggestions, ignored, voices = [], utterances = [] } = state.current
  const detail = document.getElementById('detail')
  const active = m.status === 'open' || m.status === 'prompting'
  const title = el('input', { class: 'title-input', type: 'text', value: titleOf(m), maxlength: '200', 'aria-label': 'Título da reunião' })
  title.addEventListener('change', () => guarded(null, '', async () => {
    await api(`meetings/${m.id}/title`, { title: title.value })
    toast('Título salvo.')
    await refresh(m.id)
  }))

  const closeBtn = active && el('button', { type: 'button', class: 'btn btn-primary', text: 'Encerrar reunião' })
  closeBtn && closeBtn.addEventListener('click', () => guarded(closeBtn, 'Encerrando…', async () => {
    await api(`meetings/${m.id}/close`, { participants: m.participants })
    toast('Reunião encerrada. A ata está sendo gerada.')
    await refresh(m.id)
  }))
  const regenBtn = !active && el('button', { type: 'button', class: 'btn', text: 'Refazer ata' })
  regenBtn && regenBtn.addEventListener('click', () => guarded(regenBtn, 'Gerando…', async () => {
    await api(`meetings/${m.id}/regenerate`, {})
    toast('Refazendo a ata…')
    await refresh(m.id)
  }))
  const mergeBtn = el('button', { type: 'button', class: 'btn', text: 'Juntar com outra reunião…', onclick: () => openMerge() })
  const deleteBtn = !active && el('button', { type: 'button', class: 'btn btn-danger', text: 'Excluir reunião' })
  deleteBtn && deleteBtn.addEventListener('click', () => {
    if (!confirm('Excluir esta reunião e a ata? As falas ficam guardadas como ignoradas e as pendências continuam.')) return
    guarded(deleteBtn, 'Excluindo…', async () => {
      await api(`meetings/${m.id}/delete`, {})
      state.current = null
      history.replaceState(null, '', location.pathname)
      document.getElementById('detail').replaceChildren(el('div', { class: 'empty' },
        el('h1', { text: 'Reunião excluída' }), el('p', { class: 'muted', text: 'Selecione outra reunião na lista.' })))
      await loadMeetings()
      toast('Reunião excluída.')
    })
  })

  detail.replaceChildren(
    el('div', { class: 'head' }, title, el('div', { class: 'actions' }, closeBtn, regenBtn, mergeBtn, deleteBtn)),
    el('p', { class: 'when' }, `${fmtFull.format(new Date(m.started_at))} · ${span(m)} · ${transcripts.length} falas`
      + (ignored ? ` · ${ignored} trecho(s) de ruído/eco ignorado(s)` : ''), statusBadge(m)),
    renderParticipants(m, suggestions),
    renderVoices(m, voices),
    renderAta(ata, m),
    utterances.length ? renderUtterances(utterances) : null,
    utterances.length ? renderLiveFold(transcripts) : renderTranscript(transcripts),
  )
  renderSelectionBar()
  clearTimeout(state.ataTimer)
  const processing = m.offline_status === 'pending' || m.offline_status === 'running'
  if (m.ata_status === 'pending' || processing) state.ataTimer = setTimeout(() => refresh(m.id, true).catch(() => {}), processing ? 8000 : 4000)
}

function renderParticipants(m, suggestions) {
  const people = [...(m.participants || [])]
  const chips = el('div', { class: 'chips' })
  const input = el('input', { type: 'text', placeholder: 'Adicionar participante e Enter', 'aria-label': 'Adicionar participante', list: 'people-list' })
  const save = async names => {
    await api(`meetings/${m.id}/participants`, { participants: names })
    await refresh(m.id, true)
    toast('Participantes salvos.')
  }
  const draw = () => {
    chips.replaceChildren(...(people.length ? people.map((name, i) => el('span', { class: 'chip' }, name,
      el('button', { type: 'button', 'aria-label': `Remover ${name}`, text: '×', onclick: () => guarded(null, '', () => save(people.filter((_, j) => j !== i))) })))
      : [el('span', { class: 'muted', text: 'Nenhum participante definido.' })]))
  }
  draw()
  input.addEventListener('keydown', ev => {
    if (ev.key !== 'Enter' || !input.value.trim()) return
    ev.preventDefault()
    guarded(null, '', () => save([...people, input.value.trim()]))
  })
  const datalist = el('datalist', { id: 'people-list' }, (suggestions || []).map(s => el('option', { value: s.name })))
  const sugg = (suggestions || []).length
    ? [el('p', { class: 'subhead', text: 'Sugestões do sistema' }),
       el('div', { class: 'chips' }, suggestions.map(s => el('button', {
         type: 'button', class: 'chip chip-suggest', title: s.reason,
         onclick: () => guarded(null, '', () => save([...people, s.name])),
       }, '+ ', s.name, ' ', el('small', { text: `(${s.reason})` }))))]
    : []
  return el('section', { class: 'card', 'aria-labelledby': 'h-part' },
    el('h2', { id: 'h-part', text: 'Participantes' }), chips, el('div', { class: 'add-person' }, input), datalist, ...sugg)
}

// ── vozes (passada offline) ──────────────────────────────────────────────────
let sampleAudio = null
async function playSample(voiceId, button, path = `/api/voices/${voiceId}/sample`) {
  try {
    const res = await fetch(path, { headers: { 'X-Jarvis-Token': TOKEN } })
    if (!res.ok) throw new Error('Trecho indisponível.')
    const url = URL.createObjectURL(await res.blob())
    sampleAudio?.pause()
    sampleAudio = new Audio(url)
    button.textContent = '■ Parar'
    const done = () => { button.textContent = '▶ Ouvir'; URL.revokeObjectURL(url) }
    sampleAudio.addEventListener('ended', done)
    sampleAudio.addEventListener('pause', done)
    await sampleAudio.play()
  } catch (err) { button.textContent = '▶ Ouvir'; toast(err.message, 'error') }
}

function fmtDuration(seconds) {
  const s = Math.round(seconds || 0)
  return s >= 60 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${s} s`
}

function renderVoices(m, voices) {
  const card = (...children) => el('section', { class: 'card', 'aria-labelledby': 'h-voices' }, el('h2', { id: 'h-voices', text: 'Vozes' }), ...children)
  const reprocess = label => {
    const btn = el('button', { type: 'button', class: 'btn', text: label })
    btn.addEventListener('click', () => guarded(btn, 'Enviando…', async () => {
      await api(`meetings/${m.id}/reprocess`, {})
      toast('Processando o áudio da reunião. Pode levar alguns minutos.')
      await refresh(m.id)
    }))
    return btn
  }
  if (m.offline_status === 'pending') {
    return card(el('p', { class: 'muted', role: 'status', text: 'Áudio aguardando na fila. As reuniões são processadas uma por vez, com prioridade para a escuta ao vivo. A página atualiza sozinha.' }))
  }
  if (m.offline_status === 'running') {
    return card(el('p', { class: 'muted', role: 'status', text: 'Separando as vozes e transcrevendo o áudio gravado da reunião… a página atualiza sozinha. A ata sai em seguida.' }))
  }
  if (m.offline_status === 'error') {
    return card(el('p', { class: 'muted', text: 'Não foi possível processar o áudio desta reunião; a ata foi feita com a transcrição ao vivo.' }), reprocess('Tentar de novo'))
  }
  if (!voices.length) {
    return m.status === 'closed' ? card(el('p', { class: 'muted', text: 'Esta reunião não passou pela separação de vozes.' }), reprocess('Processar o áudio')) : null
  }
  const rows = voices
  const fields = new Map()
  const list = el('div', { class: 'voices' })
  const counts = el('details', {}, el('summary', { text: 'Quantidade de vozes (opcional)' }))
  const inputs = {}
  for (const [source, label] of [['mic', 'Vozes no microfone'], ['loopback', 'Vozes no áudio do computador']]) {
    const input = el('input', { type: 'number', min: '1', max: '20', placeholder: 'Automático', 'aria-label': label })
    input.value = m[`${source}_speakers`] || ''
    inputs[source] = input
    counts.append(el('label', {}, label, input))
  }
  const saveCounts = el('button', { type: 'button', class: 'btn', text: 'Salvar quantidade' })
  saveCounts.addEventListener('click', () => guarded(saveCounts, 'Salvando…', async () => {
    if (Object.values(inputs).some(input => !input.checkValidity())) throw new Error('Informe de 1 a 20 vozes.')
    const body = Object.fromEntries(Object.entries(inputs).map(([source, input]) => [`${source}_speakers`, input.value ? Number(input.value) : null]))
    await api(`meetings/${m.id}/speaker-counts`, body)
    toast('Quantidade salva. Use Reprocessar o áudio para aplicar.')
    await refresh(m.id)
  }))
  counts.append(el('p', { class: 'muted', text: 'Preencha somente se souber quantas vozes aparecem em cada fonte. Deixe vazio para detectar automaticamente.' }), saveCounts)
  for (const v of rows) {
    const play = el('button', { type: 'button', class: 'btn', text: '▶ Ouvir', disabled: !v.has_sample })
    play.addEventListener('click', () => playSample(v.id, play))
    const name = el('input', { type: 'text', value: v.name || '', placeholder: 'Quem é?', maxlength: '80', list: 'people-list', 'aria-label': `Nome da ${v.fallback}` })
    const merge = el('select', { 'aria-label': `${v.fallback} é a mesma pessoa que` },
      el('option', { value: '', text: 'Mesma pessoa que…' }),
      rows.filter(o => o.id !== v.id).map(o => el('option', { value: String(o.id), text: o.display_name })))
    merge.value = v.merged_into ? String(v.merged_into) : ''
    const discard = el('input', { type: 'checkbox' })
    discard.checked = !!v.discarded
    fields.set(v.id, { name, merge, discard })
    const suggest = v.suggested_name && !v.name
      ? el('button', { type: 'button', class: 'chip chip-suggest', onclick: () => { name.value = v.suggested_name } },
        `Parece ${v.suggested_name} `, el('small', { text: `(similaridade ${(v.suggested_score || 0).toFixed(2)})` }))
      : null
    list.append(el('div', { class: 'voice', 'data-discarded': v.is_discarded ? 'true' : 'false' },
      el('div', { class: 'voice-head' }, play,
        el('strong', { text: v.fallback }),
        el('span', { class: 'muted', text: `${fmtDuration(v.seconds)} · ${v.source === 'mic' ? 'sala' : 'chamada/PC'}` }), suggest),
      v.quality ? el('p', { class: 'muted', text: `${fmtDuration(v.quality.clean_seconds)} de fala limpa em ${v.quality.windows} trechos${v.quality.windows < 3 ? ' — insuficiente para nomear automaticamente' : ''}` }) : null,
      el('div', { class: 'voice-fields' }, name, merge,
        el('label', { class: 'radio' }, discard, 'Descartar (ruído, TV)'))))
  }
  const save = el('button', { type: 'button', class: 'btn btn-primary', text: 'Salvar vozes e refazer a ata' })
  save.addEventListener('click', () => guarded(save, 'Salvando…', async () => {
    const payload = rows.map(v => {
      const f = fields.get(v.id)
      return { id: v.id, name: f.name.value.trim(), merge_into: f.merge.value ? Number(f.merge.value) : null, discarded: f.discard.checked }
    })
    await api(`meetings/${m.id}/voices`, { voices: payload })
    toast('Vozes salvas. A ata está sendo refeita.')
    await refresh(m.id)
  }))
  return card(
    el('p', { class: 'hint', text: 'Ouça cada voz e diga quem é. Se duas vozes são a mesma pessoa, use "Mesma pessoa que…". Descarte o que for ruído ou TV.' }),
    counts, list, el('p', { class: 'muted', text: 'Confirmações suas ajudam nas próximas reuniões. Para melhorar o cadastro vocal, acrescente gravações limpas da pessoa em diferentes distâncias, uma pessoa por vez.' }),
    el('div', { class: 'actions voices-actions' }, save, reprocess('Reprocessar o áudio')))
}

function renderUtterances(utterances) {
  const list = el('ol', { class: 'lines utterances', 'aria-label': 'Conversa por falante' })
  const repetitive = el('ol', { class: 'lines utterances', 'aria-label': 'Trechos repetitivos para revisão' })
  let repetitiveCount = 0
  for (const u of utterances) {
    const repeated = u.review?.status === 'pending' && u.review.reasons.includes('texto repetitivo')
    if (repeated) repetitiveCount++
    const target = repeated ? repetitive : list
    target.append(el('li', { class: 'utterance', 'data-discarded': u.discarded ? 'true' : 'false' },
      el('time', { datetime: u.start_at, text: fmtTimeSec.format(new Date(u.start_at)) }),
      el('strong', { class: 'speaker', text: u.speaker }),
      el('p', { text: u.text || 'Fala detectada sem transcrição.' }),
      u.review ? renderReview(u) : null))
  }
  return el('section', { class: 'card', 'aria-labelledby': 'h-utt' }, el('h2', { id: 'h-utt', text: 'Conversa por falante' }), list,
    repetitiveCount ? el('details', {}, el('summary', { text: `${repetitiveCount} trechos repetitivos — possíveis ecos do Whisper` }),
      el('p', { class: 'muted', text: 'Originais e áudio preservados para conferência. Nenhuma correção foi aceita automaticamente.' }), repetitive) : null)
}

function renderReview(u) {
  const r = u.review
  const play = el('button', { type: 'button', class: 'btn', text: '▶ Ouvir', disabled: !r.sample_file })
  play.addEventListener('click', () => playSample(u.id, play, `/api/utterances/${u.id}/audio`))
  const fold = el('details', {},
    el('summary', { text: r.status === 'resolved' ? 'Trecho conferido' : 'Trecho duvidoso — conferir' }),
    el('p', { class: 'muted', text: r.reasons.join(' · ') }), play,
    el('p', { text: `Original: ${r.original_text || '(sem texto)'}` }),
    el('p', { text: `Segunda escuta: ${r.alternative_text || '(sem texto)'}` }))
  if (r.status === 'resolved') return fold
  const input = el('textarea', { rows: '3', maxlength: '10000', 'aria-label': 'Corrigir texto do trecho' })
  input.value = u.text
  const save = (label, choice, disabled = false) => {
    const btn = el('button', { type: 'button', class: 'btn', text: label, disabled })
    btn.addEventListener('click', () => guarded(btn, 'Salvando…', async () => {
      const mid = state.current.meeting.id
      await api(`meetings/${mid}/review`, { utterance_id: u.id, choice, text: input.value })
      toast('Trecho conferido. A ata será atualizada.')
      await refresh(mid)
    }))
    return btn
  }
  fold.append(input, el('div', { class: 'actions' }, save('Manter original', 'original'),
    save('Usar segunda escuta', 'alternative', !r.alternative_text), save('Salvar correção', 'custom')))
  return fold
}

function renderLiveFold(transcripts) {
  const fold = el('details', { class: 'card live-fold' },
    el('summary', { text: `Transcrição ao vivo (${transcripts.length} falas) — use para dividir a reunião` }))
  fold.append(renderTranscript(transcripts))
  return fold
}

function renderAta(ata, m) {
  if (m.status === 'closed' && m.ata_status === 'pending') {
    return el('section', { class: 'card' }, el('h2', { text: 'Ata' }), el('p', { class: 'muted', role: 'status', text: 'Gerando a ata… a página atualiza sozinha.' }))
  }
  if (m.ata_status === 'empty') {
    return el('section', { class: 'card' }, el('h2', { text: 'Ata' }), el('p', { class: 'muted', text: 'Sem ata: só havia ruído ou eco do PC nesta reunião.' }))
  }
  if (m.ata_status === 'error') {
    return el('section', { class: 'card' }, el('h2', { text: 'Ata' }), el('p', { class: 'muted', role: 'status', text: 'A ata não pôde ser gerada agora (serviço indisponível). Nova tentativa automática em alguns minutos, ou use "Refazer ata".' }))
  }
  if (!ata) return el('section', { class: 'card' }, el('h2', { text: 'Ata' }), el('p', { class: 'muted', text: 'A ata é gerada quando a reunião é encerrada.' }))
  const list = (label, text) => {
    const items = String(text || '').split('\n').map(s => s.trim()).filter(Boolean)
    return items.length ? [el('p', { class: 'subhead', text: label }), el('ul', {}, items.map(t => el('li', { text: t })))] : []
  }
  return el('section', { class: 'card', 'aria-labelledby': 'h-ata' },
    el('h2', { id: 'h-ata', text: 'Ata' }),
    el('p', { text: ata.summary || '—' }),
    ...list('Decisões', ata.key_decisions), ...list('Tarefas', ata.action_items))
}

function renderTranscript(rows) {
  const list = el('ol', { class: 'lines', 'aria-label': 'Transcrição' })
  rows.forEach((r, index) => {
    const box = el('input', { type: 'checkbox', 'aria-label': `Selecionar fala das ${fmtTimeSec.format(new Date(r.timestamp))}` })
    box.checked = state.selected.has(r.id)
    box.addEventListener('click', ev => toggleSelect(rows, index, ev.shiftKey, box.checked))
    list.append(el('li', { class: 'line', 'data-selected': state.selected.has(r.id) ? 'true' : 'false', 'data-id': String(r.id) },
      box, el('time', { datetime: r.timestamp, text: fmtTimeSec.format(new Date(r.timestamp)) }),
      el('span', { class: 'src', 'data-src': r.source, text: r.source === 'mic' ? 'sala' : 'chamada' }),
      el('p', { text: r.text })))
  })
  return el('section', { class: 'card', 'aria-labelledby': 'h-tr' },
    el('h2', { id: 'h-tr', text: 'Transcrição' }),
    el('p', { class: 'hint', text: 'Marque as falas que pertencem a outra reunião (Shift+clique seleciona um intervalo) e use "Enviar para outra reunião".' }),
    rows.length ? list : el('p', { class: 'muted', text: 'Sem falas.' }))
}

function toggleSelect(rows, index, range, checked) {
  if (range && state.lastClicked !== null) {
    const [a, b] = [Math.min(index, state.lastClicked), Math.max(index, state.lastClicked)]
    for (let i = a; i <= b; i++) checked ? state.selected.add(rows[i].id) : state.selected.delete(rows[i].id)
  } else {
    checked ? state.selected.add(rows[index].id) : state.selected.delete(rows[index].id)
  }
  state.lastClicked = index
  document.querySelectorAll('.line').forEach(li => {
    const on = state.selected.has(Number(li.dataset.id))
    li.dataset.selected = on ? 'true' : 'false'
    li.querySelector('input').checked = on
  })
  renderSelectionBar()
}

function renderSelectionBar() {
  document.querySelector('.selection-bar')?.remove()
  if (!state.selected.size) return
  document.body.append(el('div', { class: 'selection-bar', role: 'region', 'aria-label': 'Falas selecionadas' },
    `${state.selected.size} fala(s) selecionada(s)`,
    el('button', { type: 'button', class: 'btn btn-primary', text: 'Enviar para outra reunião', onclick: openMove }),
    el('button', { type: 'button', class: 'btn btn-ghost', text: 'Limpar', onclick: () => { state.selected.clear(); renderDetail() } })))
}

// ── diálogos ─────────────────────────────────────────────────────────────────
function otherMeetingsOptions(select) {
  const current = state.current.meeting
  const base = new Date(current.started_at).getTime()
  const others = state.meetings.filter(m => m.id !== current.id)
    .sort((a, b) => Math.abs(new Date(a.started_at) - base) - Math.abs(new Date(b.started_at) - base))
  select.replaceChildren(...others.map(m => el('option', { value: String(m.id) },
    `${dayKey(m.started_at).split('-').reverse().join('/')} ${span(m)} — ${titleOf(m)}`)))
  return others.length
}

function openMove() {
  const dialog = document.getElementById('move-dialog')
  const has = otherMeetingsOptions(document.getElementById('move-target'))
  document.getElementById('move-count').textContent = `${state.selected.size} fala(s) selecionada(s).`
  const radios = dialog.querySelectorAll('input[name=dest]')
  radios[0].disabled = !has
  radios[has ? 0 : 1].checked = true
  syncMoveInputs()
  dialog.showModal()
}

function syncMoveInputs() {
  const dest = document.querySelector('#move-dialog input[name=dest]:checked').value
  document.getElementById('move-target').disabled = dest !== 'existing'
  document.getElementById('move-new-title').disabled = dest !== 'new'
}

function openMerge() {
  const has = otherMeetingsOptions(document.getElementById('merge-target'))
  if (!has) return toast('Não há outra reunião para juntar.', 'error')
  document.getElementById('merge-dialog').showModal()
}

function wireDialogs() {
  document.querySelectorAll('dialog [data-close]').forEach(b => b.addEventListener('click', () => b.closest('dialog').close()))
  document.querySelectorAll('#move-dialog input[name=dest]').forEach(r => r.addEventListener('change', syncMoveInputs))
  document.getElementById('move-form').addEventListener('submit', ev => {
    ev.preventDefault()
    const dest = document.querySelector('#move-dialog input[name=dest]:checked').value
    const body = { transcript_ids: [...state.selected] }
    if (dest === 'existing') body.target_id = Number(document.getElementById('move-target').value)
    else body.new_title = document.getElementById('move-new-title').value
    guarded(document.getElementById('move-confirm'), 'Enviando…', async () => {
      const r = await api(`meetings/${state.current.meeting.id}/move`, body)
      document.getElementById('move-dialog').close()
      toast('Falas enviadas. As atas estão sendo refeitas.')
      await loadMeetings()
      await openMeeting(r.source ? r.source.id : r.target.id)
    })
  })
  document.getElementById('merge-form').addEventListener('submit', ev => {
    ev.preventDefault()
    const button = ev.submitter
    guarded(button, 'Juntando…', async () => {
      const otherId = Number(document.getElementById('merge-target').value)
      const r = await api(`meetings/${state.current.meeting.id}/merge`, { other_id: otherId })
      document.getElementById('merge-dialog').close()
      toast('Reuniões juntadas. A ata está sendo refeita.')
      await loadMeetings()
      await openMeeting(r.meeting.id)
    })
  })
}

// ── pergunta "encerrar a reunião?" ───────────────────────────────────────────
async function pollPrompt() {
  const box = document.getElementById('prompt')
  let prompt = null
  try { prompt = (await api('prompt')).prompt } catch { return }
  if (!prompt) { box.hidden = true; box.replaceChildren(); return }
  if (box.dataset.meeting === String(prompt.meeting.id) && !box.hidden) {
    box.querySelector('[data-countdown]').textContent = countdown(prompt.auto_close_in_s)
    return
  }
  box.dataset.meeting = String(prompt.meeting.id)
  const names = new Set(prompt.meeting.participants || [])
  const input = el('input', { type: 'text', placeholder: 'Com quem foi? (nomes separados por vírgula)', 'aria-label': 'Participantes' })
  input.value = [...names].join(', ')
  const close = el('button', { type: 'button', class: 'btn btn-primary', text: 'Encerrar e gerar ata' })
  close.addEventListener('click', () => guarded(close, 'Encerrando…', async () => {
    const participants = input.value.split(',').map(s => s.trim()).filter(Boolean)
    await api(`meetings/${prompt.meeting.id}/close`, { participants })
    box.hidden = true
    toast('Reunião encerrada.')
    await loadMeetings()
    await openMeeting(prompt.meeting.id)
  }))
  const keep = el('button', { type: 'button', class: 'btn', text: 'Ainda estamos em reunião' })
  keep.addEventListener('click', () => guarded(keep, '…', async () => {
    await api(`meetings/${prompt.meeting.id}/continue`, {})
    box.hidden = true
    toast('A reunião continua sendo gravada.')
  }))
  box.replaceChildren(
    el('h2', { text: `Reunião em silêncio há 2 minutos (${span(prompt.meeting)})` }),
    el('p', { class: 'muted' }, 'Encerrar a transcrição? Sem resposta, encerra sozinha em ', el('span', { 'data-countdown': '', text: countdown(prompt.auto_close_in_s) }), '.'),
    input,
    el('div', { class: 'chips' }, (prompt.suggestions || []).map(s => el('button', {
      type: 'button', class: 'chip chip-suggest', title: s.reason,
      onclick: () => { names.add(s.name); input.value = [...names].join(', ') },
    }, '+ ', s.name))),
    el('div', { class: 'actions' }, close, keep))
  box.hidden = false
}

function countdown(s) { const m = Math.floor(s / 60); return m ? `${m} min ${s % 60}s` : `${s}s` }

async function refresh(id, keepSelection = false) {
  const selected = new Set(state.selected)
  await loadMeetings()
  state.current = await api('meetings/' + id)
  if (keepSelection) state.selected = selected
  renderDetail()
  renderTree()
}

// ── início ───────────────────────────────────────────────────────────────────
document.getElementById('search').addEventListener('input', ev => { state.filter = ev.target.value; renderTree() })
wireDialogs()
loadMeetings().then(() => {
  const id = Number(location.hash.slice(1))
  if (id) openMeeting(id)
})
pollPrompt()
setInterval(pollPrompt, 10000)
setInterval(() => { if (!document.querySelector('dialog[open]') && !state.selected.size) loadMeetings() }, 30000)
