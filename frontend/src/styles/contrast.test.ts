import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { expect, test } from 'vitest'

// WCAG 2.2 AA (1.4.3): 4.5:1 for text. Computed from the real tokens, so a palette
// edit that breaks a theme fails here instead of in an audit.
const css = readFileSync(resolve(process.cwd(), 'src/styles/index.css'), 'utf8')

function tokens(selector: string): Map<string, string> {
  const start = css.indexOf(selector)
  const body = css.slice(css.indexOf('{', start) + 1, css.indexOf('}', start))
  return new Map(
    [...body.matchAll(/--([\w-]+):\s*(#[0-9a-f]{6})/gi)].map((m) => [m[1] ?? '', m[2] ?? '']),
  )
}

const themes = {
  dark: tokens("[data-theme='dark']"),
  light: tokens("[data-theme='light']"),
}

function luminance(hex: string) {
  const [r = 0, g = 0, b = 0] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

function ratio(theme: Map<string, string>, fg: string, bg: string) {
  const [a, b] = [fg, bg].map((name) => {
    const hex = theme.get(name)
    if (!hex) throw new Error(`token --${name} not found`)
    return luminance(hex)
  })
  const [hi = 0, lo = 0] = [a ?? 0, b ?? 0].sort((x, y) => y - x)
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

for (const [name, theme] of Object.entries(themes)) {
  test(`${name}: text tokens meet 4.5:1 on every surface`, () => {
    const failures = TEXT.flatMap((fg) =>
      SURFACES.map((bg) => [fg, bg, ratio(theme, fg, bg)] as const)
        .filter(([, , r]) => r < 4.5)
        .map(([f, b, r]) => `${f} on ${b}: ${r.toFixed(2)}`),
    )
    expect(failures).toEqual([])
  })

  test(`${name}: text on solid fills meets 4.5:1`, () => {
    const failures = ON_FILL.map(([fg, bg]) => [fg, bg, ratio(theme, fg, bg)] as const)
      .filter(([, , r]) => r < 4.5)
      .map(([f, b, r]) => `${f} on ${b}: ${r.toFixed(2)}`)
    expect(failures).toEqual([])
  })
}
