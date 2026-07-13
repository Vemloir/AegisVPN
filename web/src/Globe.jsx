import { useEffect, useMemo, useRef } from 'react'
import { geoOrthographic, geoPath, geoGraticule, geoCentroid, geoDistance } from 'd3-geo'
import { feature, mesh } from 'topojson-client'
import world from 'world-atlas/countries-110m.json'
import { CENTROID_OVERRIDE, ISO_NUMERIC, POINT_LOCATION } from './countries.js'

// The canvas is fully OPAQUE and paints the page background (--bg, resolved
// from CSS at runtime) itself. A transparent canvas looks equivalent, but
// its semi-transparent pixels (antialiasing, the dissolve) get blended with
// the page by the GPU compositor in linear space and re-quantized, landing
// one RGB step off (#151515 on a #161616 page) — an eyedropper-visible seam
// that software rasterization doesn't reproduce. An opaque layer is copied
// byte-for-byte instead of blended, so every blend happens inside this 2D
// context with one consistent rounding.
//
// The highlight FILLS are theme-dependent on purpose: the same hex reads
// noticeably brighter against a near-black page than against cream
// (simultaneous contrast). The dark values are the light values scaled to
// ~0.65 luminance with the hue kept — equivalent to compositing one shared
// translucent base color over each theme's background, which is the model
// the user picked (between swatches C #8F5540 and D #7C4936 on the strip).
// The OUTLINES read the same on both backgrounds and stay shared.
const OUTLINE = { hiLine: '#A34E2F', hiSelLine: '#A34E2F' }
const PALETTE = {
  light: { land: '#D5D0C2', grat: '#DBD6C9', border: '#C6C0B0', coast: '#BDB7A6', hi: '#CC785C', hiSel: '#C2613D', ...OUTLINE },
  dark: { land: '#1D1D1D', grat: '#232323', border: '#333333', coast: '#333333', hi: '#864F3C', hiSel: '#7F4028', ...OUTLINE },
}

// The globe's lower half dissolves into the page. This used to be a CSS
// mask-image over the whole canvas, which was wrong in two ways: it faded the
// highlight fills together with the map, so the SAME highlight hex read
// salmon in the light theme and muddy brown in the dark one (it was being
// blended into a cream vs a near-black page), and the half-transparent map
// itself read as "the globe block has a different background". The fade is
// painted inside the canvas instead — as a gradient of the BACKGROUND color
// over the content (not an alpha erase; the canvas must stay opaque, see
// above) — so the map and the highlights dissolve on their own schedules:
// the map starts fading at 58% of the height, the highlights keep their true
// color almost to the bottom edge (85%) and only fade there to avoid a hard
// clip at the canvas edge.
function fadeToBg(ctx, w, h, from, rgb) {
  const g = ctx.createLinearGradient(0, h * from, 0, h)
  g.addColorStop(0, `rgba(${rgb[0]},${rgb[1]},${rgb[2]},0)`)
  g.addColorStop(1, `rgba(${rgb[0]},${rgb[1]},${rgb[2]},1)`)
  ctx.fillStyle = g
  ctx.fillRect(0, 0, w, h)
}
const MAP_FADE_START = 0.58
const HIGHLIGHT_FADE_START = 0.85

function hexToRgb(hex) {
  const h = hex.replace('#', '')
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)]
}

const PAL_RGB = Object.fromEntries(
  ['light', 'dark'].map((th) => [
    th,
    Object.fromEntries(Object.entries(PALETTE[th]).map(([k, v]) => [k, hexToRgb(v)])),
  ]),
)

// The page background transitions between themes with CSS `background .4s
// ease`. The canvas is opaque (see above), so it no longer inherits that
// transition for free — it must animate its own palette with the SAME
// duration and curve, or the globe block visibly lags the page for 400ms on
// every theme toggle. This evaluates CSS 'ease' = cubic-bezier(.25,.1,.25,1).
const THEME_TRANSITION_MS = 400
function cssEase(t) {
  const p1x = 0.25, p1y = 0.1, p2x = 0.25, p2y = 1
  const cx = 3 * p1x, bx = 3 * (p2x - p1x) - cx, ax = 1 - cx - bx
  const cy = 3 * p1y, by = 3 * (p2y - p1y) - cy, ay = 1 - cy - by
  let u = t
  for (let i = 0; i < 5; i++) {
    const x = ((ax * u + bx) * u + cx) * u - t
    if (Math.abs(x) < 1e-4) break
    const d = (3 * ax * u + 2 * bx) * u + cx
    if (Math.abs(d) < 1e-6) break
    u -= x / d
  }
  u = Math.min(1, Math.max(0, u))
  return ((ay * u + by) * u + cy) * u
}

function lerpPal(from, to, e) {
  const out = {}
  for (const k of Object.keys(to)) {
    out[k] = to[k].map((c, i) => Math.round(from[k][i] + (c - from[k][i]) * e))
  }
  return out
}

const countriesFC = feature(world, world.objects.countries)
const borders = mesh(world, world.objects.countries, (a, b) => a !== b)
const coast = mesh(world, world.objects.countries, (a, b) => a === b)

function buildHighlights(locations) {
  return locations
    .map((loc) => {
      const numeric = ISO_NUMERIC[loc.code]
      const feat = numeric ? countriesFC.features.find((f) => f.id === numeric) : null
      if (feat) {
        const centroid = CENTROID_OVERRIDE[loc.code] || geoCentroid(feat)
        return { id: loc.id, feat, centroid }
      }
      // No atlas polygon (city-states / SARs like Hong Kong) → a point marker.
      const point = POINT_LOCATION[loc.code]
      if (point) return { id: loc.id, feat: null, centroid: point }
      return null
    })
    .filter(Boolean)
}

export default function Globe({ locations, selected, onSelect, theme, autoRotate = true, style }) {
  const canvasRef = useRef(null)
  // Animation state lives in a ref, not React state: the draw loop runs at 60fps
  // and must never trigger a re-render.
  const anim = useRef({ rotation: [-14, -11], flyTarget: null })

  // Resolving a location to an atlas feature scans 177 geometries and runs
  // geoCentroid, so it happens once per location change — never inside the loop.
  const highlights = useMemo(() => buildHighlights(locations), [locations])

  const latest = useRef({ highlights, selected, theme, autoRotate })
  latest.current = { highlights, selected, theme, autoRotate }

  // Fly to whichever location the surrounding UI selected.
  useEffect(() => {
    if (selected == null) return
    const hl = highlights.find((h) => h.id === selected)
    if (hl) anim.current.flyTarget = [-hl.centroid[0], -hl.centroid[1]]
  }, [selected, highlights])

  useEffect(() => {
    const cv = canvasRef.current
    if (!cv) return
    let raf = 0
    let projection = null
    let w = 0, h = 0, dpr = 1

    // Measure the canvas itself, never the window. The sphere is deliberately
    // larger than its box and centred below it (translate y = 1.18h), so a
    // wrong h shifts the visible arc up or down by hundreds of pixels. A single
    // measurement at mount is not enough: the box can still be settling (web
    // fonts, layout) and a stale h leaves the backing store the wrong aspect.
    const resize = () => {
      const nw = cv.clientWidth
      const nh = cv.clientHeight
      if (!nw || !nh) return // not laid out yet; the observer will call again
      w = nw
      h = nh
      dpr = Math.min(2, window.devicePixelRatio || 1)
      cv.width = w * dpr
      cv.height = h * dpr

      // The sphere's centre sits at 1.18h, below the box, so only its crown and
      // upper arc show. Radius follows the design's max(0.42w, 0.95h) — but that
      // was authored against a 1180px preview, and full-bleed the 0.42w term wins
      // on any wide screen: at w=1900 the crown lands 90px ABOVE the box and is
      // clipped away. Cap R so the crown always clears the top edge.
      const CROWN_HEADROOM = 28
      const centreY = h * 1.18
      const R = Math.min(Math.max(w * 0.42, h * 0.95), centreY - CROWN_HEADROOM)
      projection = geoOrthographic().scale(R).translate([w * 0.5, centreY]).clipAngle(90)
    }
    resize()
    const ro = new ResizeObserver(resize)
    ro.observe(cv)

    const draw = () => {
      const { highlights, selected, theme, autoRotate } = latest.current
      const st = anim.current
      if (!projection) { raf = requestAnimationFrame(draw); return }
      if (st.flyTarget) {
        const [r0, r1] = st.rotation
        let d0 = st.flyTarget[0] - r0
        while (d0 > 180) d0 -= 360
        while (d0 < -180) d0 += 360
        const d1 = st.flyTarget[1] - r1
        st.rotation = [r0 + d0 * 0.09, r1 + d1 * 0.09]
        if (Math.abs(d0) < 0.4 && Math.abs(d1) < 0.4) st.flyTarget = null
      } else if (autoRotate) {
        st.rotation = [st.rotation[0] + 0.1, st.rotation[1]]
      }

      const proj = projection.rotate(st.rotation)
      const ctx = cv.getContext('2d', { alpha: false })

      // Theme change → start a palette transition matching the page's CSS
      // one. The bg target is resolved from the live --bg var so the opaque
      // base can never drift from the page.
      const themeKey = theme === 'dark' ? 'dark' : 'light'
      if (st.palTheme !== themeKey) {
        st.palTheme = themeKey
        const v = getComputedStyle(cv).getPropertyValue('--bg').trim()
        const bgHex = /^#[0-9a-fA-F]{6}$/.test(v) ? v : themeKey === 'dark' ? '#161616' : '#F3F1EA'
        st.palTarget = { ...PAL_RGB[themeKey], bg: hexToRgb(bgHex) }
        if (st.pal) {
          st.palFrom = { ...st.pal }
          st.palStart = performance.now()
        } else {
          st.pal = st.palTarget // first frame: no animation
        }
      }
      if (st.palFrom) {
        const p = Math.min(1, (performance.now() - st.palStart) / THEME_TRANSITION_MS)
        st.pal = lerpPal(st.palFrom, st.palTarget, cssEase(p))
        if (p >= 1) st.palFrom = null
      }
      const C = (k) => `rgb(${st.pal[k][0]},${st.pal[k][1]},${st.pal[k][2]})`

      ctx.save()
      ctx.scale(dpr, dpr)
      ctx.fillStyle = C('bg')
      ctx.fillRect(0, 0, w, h)
      const path = geoPath(proj, ctx)

      // Structural map first — graticule, land, borders — then its fade, so
      // the highlights drawn AFTER are untouched by it.
      ctx.beginPath(); path(geoGraticule().step([20, 20])()); ctx.strokeStyle = C('grat'); ctx.lineWidth = 0.6; ctx.stroke()
      ctx.beginPath(); path(countriesFC); ctx.fillStyle = C('land'); ctx.fill()
      ctx.beginPath(); path(borders); ctx.strokeStyle = C('border'); ctx.lineWidth = 0.5; ctx.stroke()
      ctx.beginPath(); path(coast); ctx.strokeStyle = C('coast'); ctx.lineWidth = 0.7; ctx.stroke()
      fadeToBg(ctx, w, h, MAP_FADE_START, st.pal.bg)

      for (const hl of highlights) {
        if (!hl.feat) continue
        ctx.beginPath(); path(hl.feat)
        ctx.fillStyle = hl.id === selected ? C('hiSel') : C('hi')
        ctx.fill()
        // All ~5 locations are highlighted at once (not just the selected one),
        // so this color renders 5x every frame regardless of selection — it
        // must vary with selection too, not just the line width.
        ctx.strokeStyle = hl.id === selected ? C('hiSelLine') : C('hiLine')
        ctx.lineWidth = hl.id === selected ? 1.2 : 0.8
        ctx.stroke()
      }
      // Point markers for locations with no atlas polygon (e.g. Hong Kong). A
      // marker on the far side of the sphere is hidden by the geoDistance gate.
      const near = [-st.rotation[0], -st.rotation[1]]
      for (const hl of highlights) {
        if (hl.feat) continue
        if (geoDistance(hl.centroid, near) > Math.PI / 2) continue
        const xy = proj(hl.centroid)
        if (!xy) continue
        const sel = hl.id === selected
        // Flat fill + thin outline — the same visual language as the country
        // highlights, so a city-state reads as one of them, not a glowing pin.
        ctx.beginPath(); ctx.arc(xy[0], xy[1], sel ? 5 : 4.5, 0, 2 * Math.PI)
        ctx.fillStyle = sel ? C('hiSel') : C('hi')
        ctx.fill()
        ctx.strokeStyle = sel ? C('hiSelLine') : C('hiLine')
        ctx.lineWidth = sel ? 1 : 0.8
        ctx.stroke()
      }
      fadeToBg(ctx, w, h, HIGHLIGHT_FADE_START, st.pal.bg)
      ctx.restore()

      raf = requestAnimationFrame(draw)
    }
    draw()

    // The globe no longer rotates by drag — only auto-rotation and flying to a
    // selection move it. A plain click just picks the nearest highlighted
    // country; a normal click doesn't fire if the user was actually scrolling
    // the page over the canvas, so this doesn't fight page scroll.
    const onClick = (e) => {
      if (!projection) return
      const st = anim.current
      const rect = cv.getBoundingClientRect()
      const px = e.clientX - rect.left
      const py = e.clientY - rect.top
      const center = [-st.rotation[0], -st.rotation[1]]
      let best = null
      let bestD = 26
      for (const hl of latest.current.highlights) {
        // A country on the far side of the sphere projects onto the near side too;
        // geoDistance rejects it before the pixel test can pick it by accident.
        if (geoDistance(hl.centroid, center) > Math.PI / 2) continue
        const xy = projection.rotate(st.rotation)(hl.centroid)
        if (!xy) continue
        const d = Math.hypot(xy[0] - px, xy[1] - py)
        if (d < bestD) { bestD = d; best = hl }
      }
      if (best) onSelect?.(best.id)
    }

    cv.addEventListener('click', onClick)

    return () => {
      cancelAnimationFrame(raf)
      ro.disconnect()
      cv.removeEventListener('click', onClick)
    }
  }, [onSelect])

  return (
    <canvas
      ref={canvasRef}
      style={{ width: '100%', height: '100%', display: 'block', ...style }}
    />
  )
}
