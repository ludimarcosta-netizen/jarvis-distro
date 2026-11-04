type Listen = (type: string, listener: () => void) => void

export interface VisibilitySource {
  visibilityState: string
  addEventListener: Listen
  removeEventListener: Listen
}

/**
 * `setInterval` that sleeps while the window is hidden. A tick that fell due while hidden runs
 * as soon as the window shows again, so the data is fresh the moment someone can look at it.
 * Returns the stop.
 */
export function everyWhileVisible(run: () => void, intervalMs: number, source: VisibilitySource = document): () => void {
  let timer: null | ReturnType<typeof setInterval> = null
  let lastRun = Date.now()

  const tick = () => {
    lastRun = Date.now()
    run()
  }

  const sync = () => {
    if (source.visibilityState === 'hidden') {
      if (timer !== null) {
        clearInterval(timer)
        timer = null
      }

      return
    }

    if (timer === null) {
      if (Date.now() - lastRun >= intervalMs) {
        tick()
      }

      timer = setInterval(tick, intervalMs)
    }
  }

  source.addEventListener('visibilitychange', sync)
  sync()

  return () => {
    source.removeEventListener('visibilitychange', sync)

    if (timer !== null) {
      clearInterval(timer)
    }
  }
}
