import type { Clip, Key } from './model.js'

// Public tail phase follows the same five-joint curve on the atlas and Alexia.
export function tailRotation(phase: number, joint: number): number {
  const lag = .7 * joint
  return 10 * (Math.sin(Math.PI * phase + lag) - Math.sin(lag))
}

function wag(id: string, label: string, duration: number, turns: number, amplitude: number): Clip {
  const steps = turns * 32
  const keys = Array.from({length: steps + 1}, (_, i): Key => [
    duration * i / steps, amplitude * Math.sin(2 * Math.PI * turns * i / steps),
  ])
  keys[steps][1] = 0
  return {id, label, duration, tracks: {'tail.curl': keys}}
}

export const tailGestures: Clip[] = [
  wag('tail_wag_slow', 'Slow tail wag', 4, 1, .16),
  wag('tail_wag_fast', 'Fast tail wag', 2, 3, .46),
  {id: 'tail_flick', label: 'Teasing tail flick', duration: 2, tracks: {
    'tail.curl': [[0,0],[.14,-.025],[.34,-.18],[.52,-.37],[.65,-.42],
      [1.25,-.42],[1.4,-.39],[1.68,-.16],[1.9,-.018],[2,0]],
  }},
]
