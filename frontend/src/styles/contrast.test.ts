import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { expect, test } from 'vitest'

// WCAG 2.2 AA (1.4.3): 4.5:1 for text. Computed from the real tokens, so a palette
// edit that breaks a theme fails here instead of in an audit.
const css = readFileSync(resolve(process.cwd(), 'src/styles/index.css'), 'utf8')

type Rgba = [number, number, number, number]
type Theme = Map<string, Rgba>

// `#rrggbb` and `rgb(r g b / a)` are the two forms the token blocks use.
function parseColor(value: string): Rgba | null {
  const hex = /^#([0-9a-f]{6})$/i.exec(value)
  if (hex?.[1]) {
    const n = parseInt(hex[1], 16)
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255, 1]
  }
  const fn = /^rgb\(\s*(\d+)\s+(\d+)\s+(\d+)\s*(?:\/\s*([\d.]+))?\s*\)$/.exec(value)
  if (fn) return [Number(fn[1]), Number(fn[2]), Number(fn[3]), Number(fn[4] ?? 1)]
  return null
}

function tokens(selector: string): Theme {
  const start = css.indexOf(selector)
  const body = css.slice(css.indexOf('{', start) + 1, css.indexOf('}', start))
  const entries: [string, Rgba][] = []
  for (const m of body.matchAll(/--([\w-]+):\s*([^;]+);/g)) {
    const color = parseColor((m[2] ?? '').trim())
    if (color) entries.push([m[1] ?? '', color])
  }
  return new Map(entries)
}

function get(theme: Theme, name: string): Rgba {
  const color = theme.get(name)
  if (!color) throw new Error(`token --${name} not found`)
  return color
}

/** `top` laid over the opaque `under`: what a `bg-warning/10` fill actually looks like. */
function over(top: Rgba, under: Rgba, alpha = top[3]): Rgba {
  const mix = (i: 0 | 1 | 2) => top[i] * alpha + under[i] * (1 - alpha)
  return [mix(0), mix(1), mix(2), 1]
}

const themes = {
  dark: tokens("[data-theme='dark']"),
  light: tokens("[data-theme='light']"),
}

function luminance([r, g, b]: Rgba) {
  const [lr = 0, lg = 0, lb = 0] = [r, g, b].map((v) => {
    const c = v / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * lr + 0.7152 * lg + 0.0722 * lb
}

function contrast(fg: Rgba, bg: Rgba) {
  const [hi = 0, lo = 0] = [luminance(fg), luminance(bg)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

const TEXT = [
  'ink',
  'ink2',
  'muted',
  'muted2',
  'tealink',
  'accent',
  'danger',
  'warning',
  'success',
  'info',
]
const SURFACES = ['bg', 'panel', 'panel2', 'card', 'card2', 'aibg']
// Text that sits on a solid status/accent fill.
const ON_FILL = [
  ['on-accent', 'accent'],
  ['on-danger', 'danger'],
  ['on-warning', 'warning'],
] as const

const STATUS = ['accent', 'danger', 'warning', 'success', 'info']
// The opacities the app puts a status colour at behind its own text (`bg-warning/10`, ...).
const TINTS = [0.05, 0.08, 0.1, 0.12]

for (const [name, theme] of Object.entries(themes)) {
  const report = (rows: (readonly [string, string, number])[]) =>
    rows.filter(([, , r]) => r < 4.5).map(([f, b, r]) => `${f} on ${b}: ${r.toFixed(2)}`)

  test(`${name}: text tokens meet 4.5:1 on every surface`, () => {
    const rows = TEXT.flatMap((fg) =>
      SURFACES.map((bg) => [fg, bg, contrast(get(theme, fg), get(theme, bg))] as const),
    )
    expect(report(rows)).toEqual([])
  })

  test(`${name}: text on solid fills meets 4.5:1`, () => {
    const rows = ON_FILL.map(
      ([fg, bg]) => [fg, bg, contrast(get(theme, fg), get(theme, bg))] as const,
    )
    expect(report(rows)).toEqual([])
  })

  test(`${name}: a status colour on its own tint meets 4.5:1`, () => {
    const rows = STATUS.flatMap((fg) =>
      TINTS.flatMap((alpha) =>
        SURFACES.map(
          (surface) =>
            [
              fg,
              `${fg}/${alpha * 100} over ${surface}`,
              contrast(get(theme, fg), over(get(theme, fg), get(theme, surface), alpha)),
            ] as const,
        ),
      ),
    )
    expect(report(rows)).toEqual([])
  })

  test(`${name}: highlight text meets 4.5:1 on its highlight`, () => {
    const rows = (['mark', 'claim'] as const).flatMap((kind) =>
      SURFACES.map(
        (surface) =>
          [
            `${kind}-fg`,
            `${kind}-bg over ${surface}`,
            contrast(get(theme, `${kind}-fg`), over(get(theme, `${kind}-bg`), get(theme, surface))),
          ] as const,
      ),
    )
    expect(report(rows)).toEqual([])
  })

  // AA pushed `muted2` up to ~4.7:1; without a gap above it the two steps read as one.
  test(`${name}: muted stays a visible step above muted2`, () => {
    // Per surface, so both tokens are always compared on the same background.
    const steps = SURFACES.map(
      (bg) =>
        [
          bg,
          contrast(get(theme, 'muted'), get(theme, bg)) /
            contrast(get(theme, 'muted2'), get(theme, bg)),
        ] as const,
    )
    const collapsed = steps
      .filter(([, step]) => step < 1.3)
      .map(([bg, step]) => `${bg}: ${step.toFixed(2)}`)
    // A failure here usually means a palette edit pulled --muted2 up (or --muted down) toward the other.
    expect(collapsed, '--muted must stay >= 1.3x the contrast of --muted2').toEqual([])
  })
}
