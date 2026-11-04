const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const { test } = require('node:test')
const vm = require('node:vm')

class Element {
  children = []
  attributes = {}
  setAttribute(key, value) { this.attributes[key] = value }
  append(child) { this.children.push(child) }
  addEventListener() {}
}

const source = readFileSync(join(__dirname, '../web/app.js'), 'utf8')
const startup = source.indexOf('// ── início')
assert.ok(startup > 0)
const context = vm.createContext({
  Node: Element,
  document: { querySelector: () => ({ content: '' }), createElement: () => new Element() },
})
vm.runInContext(source.slice(0, startup), context)
const { statusBadge, renderVoices, renderUtterances } = vm.runInContext('({ statusBadge, renderVoices, renderUtterances })', context)
const text = node => [node.textContent || '', ...node.children.map(text)].join(' ')

test('fila, execução e falha têm indicadores distintos', () => {
  for (const [offline_status, expected] of [
    ['pending', 'áudio na fila'], ['running', 'processando áudio'], ['error', 'falha no áudio'],
  ]) {
    assert.equal(statusBadge({ status: 'closed', offline_status }).textContent, expected)
  }
})

test('detalhe de uma reunião na fila não afirma que o áudio já está sendo separado', () => {
  const pending = text(renderVoices({ status: 'closed', offline_status: 'pending' }, []))
  assert.match(pending, /aguardando na fila/)
  assert.doesNotMatch(pending, /Separando as vozes/)
  const running = text(renderVoices({ status: 'closed', offline_status: 'running' }, []))
  assert.match(running, /Separando as vozes/)
})

test('eco repetitivo pendente fica recolhido, com original e revisão preservados', () => {
  const original = { id: 1, speaker: 'Voz 1', start_at: '2026-10-12T16:10:00Z', text: '__JARVIS_USER__ __JARVIS_CIDADE__',
    review: { status: 'pending', reasons: ['texto repetitivo'], original_text: '__JARVIS_USER__ __JARVIS_CIDADE__', alternative_text: '' } }
  const confirmed = { ...original, id: 2, text: 'fala confirmada', review: { ...original.review, status: 'resolved' } }
  const tree = renderUtterances([original, confirmed])
  assert.equal(tree.children[1].children.length, 1)
  assert.match(text(tree.children[1]), /fala confirmada/)
  assert.match(text(tree.children[2]), /1 trechos repetitivos/)
  assert.match(text(tree.children[2]), /__JARVIS_USER__ __JARVIS_CIDADE__/)
  assert.equal(original.text, '__JARVIS_USER__ __JARVIS_CIDADE__')
})
