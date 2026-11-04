/**
 * Frame cadence for the face renderers. The desktop window is never background-throttled
 * (the voice needs it), so an unbounded requestAnimationFrame loop would keep the GPU busy
 * at 60 fps forever, minimized or not. Draw smoothly only while something happens, slowly
 * while the face just breathes, and not at all when nobody can see it.
 */

export const ACTIVE_FPS = 30
export const IDLE_FPS = 10
export const REDUCED_MOTION_IDLE_FPS = 5
/** Pointer, wheel or key input keeps the smooth cadence this long. */
export const INTERACTION_MS = 2_500
/** Unfocused (or minimized) with no voice and no input this long: stop drawing. */
export const BLUR_STOP_MS = 2 * 60_000
/** Canvases render at most at this device-pixel ratio. */
export const MAX_CANVAS_DPR = 1.25
/** A frame never advances the animation by more than this, so a resume never jumps. */
export const MAX_FRAME_DT_S = 0.25

// A vsync that lands this close before the due time is taken instead of waiting a whole extra frame.
const FRAME_SLACK_MS = 8
// Longer waits sleep on a timer instead of spinning requestAnimationFrame at the display rate.
const TIMER_WAIT_MS = 24

export interface FrameSignals {
  now: number
  hidden: boolean
  focused: boolean
  /** When the window lost focus; ignored while focused. */
  blurredAt: number
  interactedAt: number
  /** The Jarvis is speaking (or working): wake and stay smooth even without focus. */
  voiceActive: boolean
  /** The renderer is in the middle of a transition (easing, blink, mouth closing). */
  animating: boolean
  reducedMotion: boolean
}

/** Milliseconds between frames, or null to stop drawing until something wakes the loop. */
export function frameInterval(signals: FrameSignals): null | number {
  const { now, hidden, focused, blurredAt, interactedAt, voiceActive, animating, reducedMotion } = signals

  if (hidden) {
    return null
  }

  if (!focused && !voiceActive && now - Math.max(blurredAt, interactedAt) >= BLUR_STOP_MS) {
    return null
  }

  if (voiceActive || animating || now - interactedAt < INTERACTION_MS) {
    return 1000 / ACTIVE_FPS
  }

  return 1000 / (reducedMotion ? REDUCED_MOTION_IDLE_FPS : IDLE_FPS)
}

export function canvasPixelRatio(devicePixelRatio: number): number {
  return Math.min(devicePixelRatio || 1, MAX_CANVAS_DPR)
}

export interface FrameClock {
  now: () => number
  requestFrame: (callback: (now: number) => void) => number
  cancelFrame: (id: number) => void
  setTimer: (callback: () => void, ms: number) => number
  clearTimer: (id: number) => void
}

type Listen = (type: string, listener: () => void, options?: AddEventListenerOptions) => void

export interface FrameEnvironment {
  clock: FrameClock
  document: { visibilityState: string; hasFocus: () => boolean; addEventListener: Listen; removeEventListener: Listen }
  window: { addEventListener: Listen; removeEventListener: Listen }
  reducedMotion: null | { matches: boolean; addEventListener: Listen; removeEventListener: Listen }
}

export interface FrameLoopOptions {
  /** Treat pointer, wheel and key input anywhere in the window as interaction. */
  trackInput?: boolean
  environment?: FrameEnvironment
}

export interface FrameLoop {
  /** Interaction or a resize: draw smoothly for a moment, restarting a stopped loop. */
  wake: () => void
  setVoiceActive: (active: boolean) => void
  setPaused: (paused: boolean) => void
  dispose: () => void
}

/**
 * `render(now, dt)` draws one frame (`dt` in seconds, 0 after a stop) and returns whether it is
 * mid-transition and wants the smooth cadence for the next frame.
 */
export type FrameRender = (now: number, dt: number) => boolean

const INPUT_EVENTS = ['pointermove', 'pointerdown', 'wheel', 'keydown'] as const

function browserEnvironment(): FrameEnvironment {
  return {
    clock: {
      now: () => performance.now(),
      requestFrame: callback => requestAnimationFrame(callback),
      cancelFrame: id => cancelAnimationFrame(id),
      setTimer: (callback, ms) => window.setTimeout(callback, ms),
      clearTimer: id => window.clearTimeout(id)
    },
    document,
    window,
    reducedMotion: window.matchMedia?.('(prefers-reduced-motion: reduce)') ?? null
  }
}

export function createFrameLoop(render: FrameRender, options: FrameLoopOptions = {}): FrameLoop {
  const env = options.environment ?? browserEnvironment()
  const { clock } = env
  let focused = env.document.hasFocus()
  let blurredAt = clock.now()
  // Opening the face is a transition of its own.
  let interactedAt = clock.now()
  let voiceActive = false
  let animating = false
  let paused = false
  let disposed = false
  let lastFrameAt: null | number = null
  let frameId: null | number = null
  let timerId: null | number = null

  const interval = (now: number) =>
    frameInterval({
      now,
      hidden: env.document.visibilityState === 'hidden',
      focused,
      blurredAt,
      interactedAt,
      voiceActive,
      animating,
      reducedMotion: env.reducedMotion?.matches ?? false
    })

  const cancel = () => {
    if (frameId !== null) {
      clock.cancelFrame(frameId)
      frameId = null
    }

    if (timerId !== null) {
      clock.clearTimer(timerId)
      timerId = null
    }
  }

  const stop = () => {
    cancel()
    lastFrameAt = null
  }

  const tick = (now: number) => {
    frameId = null

    if (disposed || paused) {
      return
    }

    const every = interval(now)

    if (every === null) {
      stop()

      return
    }

    if (lastFrameAt !== null && now - lastFrameAt < every - FRAME_SLACK_MS) {
      frameId = clock.requestFrame(tick)

      return
    }

    const dt = lastFrameAt === null ? 0 : Math.min(MAX_FRAME_DT_S, (now - lastFrameAt) / 1000)
    lastFrameAt = now
    animating = render(now, dt)
    schedule()
  }

  const schedule = () => {
    cancel()

    if (disposed || paused) {
      lastFrameAt = null

      return
    }

    const now = clock.now()
    const every = interval(now)

    if (every === null) {
      lastFrameAt = null

      return
    }

    const wait = lastFrameAt === null ? 0 : lastFrameAt + every - now

    if (wait > TIMER_WAIT_MS) {
      timerId = clock.setTimer(() => {
        timerId = null
        frameId = clock.requestFrame(tick)
      }, wait - FRAME_SLACK_MS)
    } else {
      frameId = clock.requestFrame(tick)
    }
  }

  // A pending vsync already draws as soon as it is due; only a sleeping or stopped loop needs a nudge.
  const nudge = () => {
    if (frameId === null) {
      schedule()
    }
  }

  const wake = () => {
    interactedAt = clock.now()
    nudge()
  }

  const onFocus = () => {
    focused = true
    nudge()
  }

  const onBlur = () => {
    focused = false
    blurredAt = clock.now()
  }

  const onVisibility = () => {
    if (env.document.visibilityState === 'hidden') {
      stop()
    } else {
      nudge()
    }
  }

  env.document.addEventListener('visibilitychange', onVisibility)
  env.window.addEventListener('focus', onFocus)
  env.window.addEventListener('blur', onBlur)
  env.reducedMotion?.addEventListener('change', schedule)

  if (options.trackInput) {
    INPUT_EVENTS.forEach(type => env.window.addEventListener(type, wake, { passive: true }))
  }

  schedule()

  return {
    wake,
    setVoiceActive(active) {
      if (voiceActive !== active) {
        voiceActive = active
        nudge()
      }
    },
    setPaused(value) {
      if (disposed || paused === value) {
        return
      }

      paused = value

      if (paused) {
        stop()
      } else {
        schedule()
      }
    },
    dispose() {
      disposed = true
      stop()
      env.document.removeEventListener('visibilitychange', onVisibility)
      env.window.removeEventListener('focus', onFocus)
      env.window.removeEventListener('blur', onBlur)
      env.reducedMotion?.removeEventListener('change', schedule)

      if (options.trackInput) {
        INPUT_EVENTS.forEach(type => env.window.removeEventListener(type, wake))
      }
    }
  }
}
