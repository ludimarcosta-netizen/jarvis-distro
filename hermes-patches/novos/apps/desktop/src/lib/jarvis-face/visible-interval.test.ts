import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { everyWhileVisible } from './visible-interval'

function page() {
  const handlers = new Set<() => void>()

  const source = {
    visibilityState: 'visible',
    addEventListener: (_: string, handler: () => void) => void handlers.add(handler),
    removeEventListener: (_: string, handler: () => void) => void handlers.delete(handler)
  }

  const show = (state: 'hidden' | 'visible') => {
    source.visibilityState = state
    handlers.forEach(handler => handler())
  }

  return { handlers, show, source }
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('everyWhileVisible', () => {
  it('ticks on the interval while visible', () => {
    const run = vi.fn()
    const { source } = page()
    everyWhileVisible(run, 1000, source)

    expect(run).not.toHaveBeenCalled()
    vi.advanceTimersByTime(3000)
    expect(run).toHaveBeenCalledTimes(3)
  })

  it('sleeps while hidden and catches up once when shown again', () => {
    const run = vi.fn()
    const { show, source } = page()
    everyWhileVisible(run, 1000, source)

    show('hidden')
    vi.advanceTimersByTime(60_000)
    expect(run).not.toHaveBeenCalled()

    show('visible')
    expect(run).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(1000)
    expect(run).toHaveBeenCalledTimes(2)
  })

  it('does not run early when the window was hidden only briefly', () => {
    const run = vi.fn()
    const { show, source } = page()
    everyWhileVisible(run, 60_000, source)

    vi.advanceTimersByTime(10_000)
    show('hidden')
    show('visible')
    expect(run).not.toHaveBeenCalled()
  })

  it('stops ticking and listening when stopped', () => {
    const run = vi.fn()
    const { handlers, source } = page()
    const stop = everyWhileVisible(run, 1000, source)

    stop()
    vi.advanceTimersByTime(5000)
    expect(run).not.toHaveBeenCalled()
    expect(handlers.size).toBe(0)
  })
})
