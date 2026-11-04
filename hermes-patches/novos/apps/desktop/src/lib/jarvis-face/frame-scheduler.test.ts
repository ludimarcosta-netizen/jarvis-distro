import { describe, expect, it } from 'vitest'

import {
  ACTIVE_FPS,
  BLUR_STOP_MS,
  canvasPixelRatio,
  createFrameLoop,
  type FrameEnvironment,
  frameInterval,
  type FrameSignals,
  IDLE_FPS,
  INTERACTION_MS,
  REDUCED_MOTION_IDLE_FPS
} from './frame-scheduler'

const ACTIVE = 1000 / ACTIVE_FPS
const IDLE = 1000 / IDLE_FPS

const rest: FrameSignals = {
  now: 1_000_000,
  hidden: false,
  focused: true,
  blurredAt: 0,
  interactedAt: 0,
  voiceActive: false,
  animating: false,
  reducedMotion: false
}

describe('frameInterval', () => {
  it('breathes slowly at rest and goes smooth for voice, transitions and recent input', () => {
    expect(frameInterval(rest)).toBe(IDLE)
    expect(frameInterval({ ...rest, voiceActive: true })).toBe(ACTIVE)
    expect(frameInterval({ ...rest, animating: true })).toBe(ACTIVE)
    expect(frameInterval({ ...rest, interactedAt: rest.now - INTERACTION_MS + 1 })).toBe(ACTIVE)
    expect(frameInterval({ ...rest, interactedAt: rest.now - INTERACTION_MS })).toBe(IDLE)
  })

  it('stops while hidden, even when the Jarvis is speaking', () => {
    expect(frameInterval({ ...rest, hidden: true })).toBeNull()
    expect(frameInterval({ ...rest, hidden: true, voiceActive: true })).toBeNull()
  })

  it('stops a few minutes after losing focus unless the voice or the pointer is active', () => {
    const blurred = { ...rest, focused: false, blurredAt: rest.now - BLUR_STOP_MS }

    expect(frameInterval({ ...blurred, blurredAt: rest.now - BLUR_STOP_MS + 1 })).toBe(IDLE)
    expect(frameInterval(blurred)).toBeNull()
    expect(frameInterval({ ...blurred, voiceActive: true })).toBe(ACTIVE)
    expect(frameInterval({ ...blurred, interactedAt: rest.now - 10 })).toBe(ACTIVE)
    // Focused windows never stop on their own, however long nobody touches them.
    expect(frameInterval({ ...blurred, focused: true })).toBe(IDLE)
  })

  it('idles even slower with reduced motion', () => {
    expect(frameInterval({ ...rest, reducedMotion: true })).toBe(1000 / REDUCED_MOTION_IDLE_FPS)
    expect(frameInterval({ ...rest, reducedMotion: true, voiceActive: true })).toBe(ACTIVE)
  })

  it('caps the canvas pixel ratio', () => {
    expect(canvasPixelRatio(2)).toBe(1.25)
    expect(canvasPixelRatio(1)).toBe(1)
    expect(canvasPixelRatio(0)).toBe(1)
  })
})

type Handler = () => void

class Target {
  private handlers = new Map<string, Set<Handler>>()

  addEventListener = (type: string, handler: Handler) => {
    const set = this.handlers.get(type) ?? new Set<Handler>()
    set.add(handler)
    this.handlers.set(type, set)
  }

  removeEventListener = (type: string, handler: Handler) => {
    this.handlers.get(type)?.delete(handler)
  }

  emit(type: string) {
    this.handlers.get(type)?.forEach(handler => handler())
  }

  count(type: string) {
    return this.handlers.get(type)?.size ?? 0
  }
}

/** A 60 Hz display with timers, advanced by hand. */
function simulation() {
  const VSYNC = 1000 / 60
  let now = 0
  let nextId = 1
  const frames = new Map<number, (t: number) => void>()
  const timers = new Map<number, { at: number; run: Handler }>()
  const doc = Object.assign(new Target(), { visibilityState: 'visible', focus: true, hasFocus: () => doc.focus })
  const win = new Target()
  const motion = Object.assign(new Target(), { matches: false })

  const environment: FrameEnvironment = {
    clock: {
      now: () => now,
      requestFrame: callback => {
        const id = nextId++
        frames.set(id, callback)

        return id
      },
      cancelFrame: id => void frames.delete(id),
      setTimer: (run, ms) => {
        const id = nextId++
        timers.set(id, { at: now + ms, run })

        return id
      },
      clearTimer: id => void timers.delete(id)
    },
    document: doc,
    window: win,
    reducedMotion: motion
  }

  const advance = (ms: number) => {
    const end = now + ms

    for (;;) {
      const timer = [...timers].sort((a, b) => a[1].at - b[1].at)[0]
      const vsync = frames.size > 0 ? (Math.floor(now / VSYNC + 1e-9) + 1) * VSYNC : Infinity
      const at = Math.min(timer?.[1].at ?? Infinity, vsync)

      if (at > end) {
        break
      }

      now = at

      if (timer && timer[1].at <= vsync) {
        timers.delete(timer[0])
        timer[1].run()
      } else {
        const due = [...frames.values()]
        frames.clear()
        due.forEach(callback => callback(now))
      }
    }

    now = end
  }

  const pending = () => frames.size + timers.size

  return { advance, doc, environment, motion, pending, win }
}

function counted(sim: ReturnType<typeof simulation>, options: { trackInput?: boolean; animating?: () => boolean } = {}) {
  const calls: { now: number; dt: number }[] = []

  const loop = createFrameLoop(
    (now, dt) => {
      calls.push({ now, dt })

      return options.animating?.() ?? false
    },
    { environment: sim.environment, trackInput: options.trackInput }
  )

  /** Frames drawn during the next `ms`. */
  const during = (ms: number) => {
    const before = calls.length
    sim.advance(ms)

    return calls.length - before
  }

  return { calls, during, loop }
}

describe('createFrameLoop', () => {
  it('draws at ~30 fps right after opening, then ~10 fps at rest', () => {
    const sim = simulation()
    const { during } = counted(sim)

    expect(during(1000)).toBeGreaterThanOrEqual(28)
    sim.advance(INTERACTION_MS)
    expect(during(1000)).toBeGreaterThanOrEqual(9)
    expect(during(1000)).toBeLessThanOrEqual(11)
  })

  it('wakes instantly to ~30 fps when the Jarvis starts speaking', () => {
    const sim = simulation()
    const { calls, during, loop } = counted(sim)
    sim.advance(INTERACTION_MS + 1000)

    const before = calls.length
    loop.setVoiceActive(true)
    sim.advance(20)
    expect(calls.length).toBe(before + 1)
    expect(during(1000)).toBeGreaterThanOrEqual(28)
    expect(during(1000)).toBeLessThanOrEqual(31)

    loop.setVoiceActive(false)
    expect(during(1000)).toBeLessThanOrEqual(11)
  })

  it('keeps the smooth cadence while the renderer reports a transition', () => {
    const sim = simulation()
    let easing = false
    const { during } = counted(sim, { animating: () => easing })
    sim.advance(INTERACTION_MS + 500)

    easing = true
    sim.advance(200)
    expect(during(1000)).toBeGreaterThanOrEqual(28)
  })

  it('stops while hidden and resumes without a time jump', () => {
    const sim = simulation()
    const { calls, during } = counted(sim)
    sim.advance(500)

    sim.doc.visibilityState = 'hidden'
    sim.doc.emit('visibilitychange')
    expect(sim.pending()).toBe(0)
    expect(during(10_000)).toBe(0)

    sim.doc.visibilityState = 'visible'
    sim.doc.emit('visibilitychange')
    expect(during(500)).toBeGreaterThan(0)
    expect(calls.every(call => call.dt <= 0.25)).toBe(true)
  })

  it('stops a while after the window loses focus and restarts on focus or voice', () => {
    const sim = simulation()
    const { during, loop } = counted(sim)

    sim.doc.focus = false
    sim.win.emit('blur')
    sim.advance(BLUR_STOP_MS + 1000)
    expect(sim.pending()).toBe(0)
    expect(during(5000)).toBe(0)

    loop.setVoiceActive(true)
    expect(during(1000)).toBeGreaterThanOrEqual(28)
    loop.setVoiceActive(false)
    expect(during(1000)).toBe(0)

    sim.doc.focus = true
    sim.win.emit('focus')
    expect(during(1000)).toBeGreaterThan(0)
  })

  it('treats window input as interaction only when asked to', () => {
    const sim = simulation()
    const tracked = counted(sim, { trackInput: true })
    sim.advance(INTERACTION_MS + 500)

    sim.win.emit('pointermove')
    expect(tracked.during(1000)).toBeGreaterThanOrEqual(28)

    tracked.loop.dispose()
    expect(sim.win.count('pointermove')).toBe(0)

    const quietSim = simulation()
    const quiet = counted(quietSim)
    expect(quietSim.win.count('keydown')).toBe(0)
    quiet.loop.dispose()
  })

  it('honours setPaused and releases everything on dispose', () => {
    const sim = simulation()
    const { during, loop } = counted(sim)

    loop.setPaused(true)
    expect(sim.pending()).toBe(0)
    expect(during(2000)).toBe(0)
    loop.setVoiceActive(true)
    expect(during(1000)).toBe(0)

    loop.setPaused(false)
    expect(during(1000)).toBeGreaterThan(0)

    loop.dispose()
    expect(sim.pending()).toBe(0)
    expect(during(1000)).toBe(0)
    expect(sim.doc.count('visibilitychange')).toBe(0)
    expect(sim.win.count('focus')).toBe(0)
    expect(sim.motion.count('change')).toBe(0)
  })

  it('reschedules when reduced motion is switched on', () => {
    const sim = simulation()
    const { during } = counted(sim)
    sim.advance(INTERACTION_MS + 500)

    sim.motion.matches = true
    sim.motion.emit('change')
    sim.advance(500)
    expect(during(2000)).toBeLessThanOrEqual(2 * REDUCED_MOTION_IDLE_FPS + 1)
  })
})
