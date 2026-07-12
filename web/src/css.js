import { useState, useMemo } from 'react'

const cache = new Map()

/**
 * Parse an inline-CSS string into a React style object.
 *
 * The design was authored with `style="display:flex; gap:22px"` on every node.
 * Rewriting all of it into object literals by hand would have been a few hundred
 * silent opportunities to mistype a value, so the strings are kept verbatim and
 * parsed once per unique string. Custom properties (--bg) pass through as-is;
 * React only camelCases known properties.
 */
export function css(str) {
  if (!str) return undefined
  const hit = cache.get(str)
  if (hit) return hit

  const out = {}
  for (const decl of str.split(';')) {
    const i = decl.indexOf(':')
    if (i < 0) continue
    const prop = decl.slice(0, i).trim()
    const value = decl.slice(i + 1).trim()
    if (!prop || !value) continue
    out[prop.startsWith('--') ? prop : toCamel(prop)] = value
  }
  cache.set(str, out)
  return out
}

function toCamel(prop) {
  // -webkit-font-smoothing -> WebkitFontSmoothing (React wants the vendor
  // prefix capitalised); backdrop-filter -> backdropFilter.
  const camel = prop.replace(/-([a-z])/g, (_, c) => c.toUpperCase())
  return prop.startsWith('-') ? camel[0].toUpperCase() + camel.slice(1) : camel
}

/**
 * Merge a base style string with a hover style string, applied on pointer over.
 * Mirrors the `style-hover` attribute the design tool used.
 *
 * Usage: <button {...hover('color:red', 'color:blue')}>
 */
export function useHoverStyle(base, hovered) {
  const [on, setOn] = useState(false)
  const style = useMemo(
    () => (on && hovered ? { ...css(base), ...css(hovered) } : css(base)),
    [base, hovered, on],
  )
  return {
    style,
    onMouseEnter: () => setOn(true),
    onMouseLeave: () => setOn(false),
  }
}
