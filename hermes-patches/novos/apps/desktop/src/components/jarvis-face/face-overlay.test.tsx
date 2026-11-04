import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

const actions = vi.hoisted(() => ({ close: vi.fn(), submit: vi.fn(() => true), voice: vi.fn() }))

vi.mock('@/store/jarvis-face', async () => {
  const { atom } = await import('nanostores')

  return {
    $jarvisFaceOpen: atom(true),
    $cerebroGraph: atom(null),
    $jarvisVoice: atom({ status: 'idle', speaking: false, listening: false, delegation: null, pending: null }),
    bindJarvisVoiceEvents: () => () => {},
    decideJarvisPending: vi.fn(),
    faceEnergy: () => 0,
    setJarvisFaceOpen: actions.close,
    submitFromJarvisFace: actions.submit,
    watchCerebroGraph: () => () => {}
  }
})
vi.mock('@/store/session', async () => {
  const { atom } = await import('nanostores')

  return { $busy: atom(false), $messages: atom([]) }
})
vi.mock('@/lib/jarvis-face/galaxy-renderer', () => ({ createGalaxy: () => null }))
vi.mock('@/lib/jarvis-face/visible-interval', () => ({ everyWhileVisible: () => () => {} }))
vi.mock('@/components/jarvis-face/face-cards', () => ({ FaceCardsLayer: () => null }))
vi.mock('@/components/jarvis-face/face-panels', () => ({ FaceDock: () => null }))
vi.mock('@/components/jarvis-face/humanoid-face', () => ({ HumanoidFace: () => null }))
vi.mock('@/contrib/react/slot', () => ({
  Slot: () => (
    <button onClick={actions.voice} type="button">
      Ativar voz
    </button>
  )
}))

import { JarvisFaceOverlay } from './face-overlay'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

it('keeps voice controls outside the message form and submits only the draft', () => {
  render(<JarvisFaceOverlay />)
  const input = screen.getByRole('textbox', { name: 'Mensagem para o Jarvis' })
  const voice = screen.getByRole('button', { name: 'Ativar voz' })
  expect(voice.closest('form')).toBeNull()
  expect(screen.getByRole('button', { name: 'Enviar' }).hasAttribute('disabled')).toBe(true)

  fireEvent.change(input, { target: { value: 'Organizar minha agenda' } })
  fireEvent.click(voice)
  expect(actions.voice).toHaveBeenCalledOnce()
  expect(actions.submit).not.toHaveBeenCalled()
  expect((input as HTMLInputElement).value).toBe('Organizar minha agenda')

  fireEvent.submit(input.closest('form')!)
  expect(actions.submit).toHaveBeenCalledExactlyOnceWith('Organizar minha agenda')
  expect((input as HTMLInputElement).value).toBe('')
})

it('lets a tool consume Escape before closing face mode', () => {
  render(<JarvisFaceOverlay />)
  const event = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true })

  event.preventDefault()
  window.dispatchEvent(event)
  expect(actions.close).not.toHaveBeenCalled()

  fireEvent.keyDown(window, { key: 'Escape' })
  expect(actions.close).toHaveBeenCalledExactlyOnceWith(false)
})
