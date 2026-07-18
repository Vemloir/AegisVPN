import { useEffect, useMemo, useRef } from 'react'
import { geoOrthographic, geoPath, geoGraticule, geoCentroid, geoDistance } from 'd3-geo'
import { feature, mesh } from 'topojson-client'
import world from 'world-atlas/countries-110m.json'
import { CENTROID_OVERRIDE, ISO_NUMERIC } from './countries.js'
import hkFeature from './hk.json'

// The canvas is TRANSPARENT: every flat pixel in the globe block — the
// "ocean", everything around the artwork — is the page's own DOM pixel, so
// it can never differ from the page background in any browser. This rule
// was earned empirically: on some Chromium + monitor-profile combinations
// DOM rasterization and canvas rasterization are color-converted through
// DIFFERENT paths (measured on a real machine via /colortest.html: the DOM
// renders #161616 as #151515 while a canvas renders it as #161616 — one
// step apart; Firefox shows no split). Which surface is "right" doesn't
// matter — any design where a flat canvas area meets a flat DOM area shows
// a seam there. Transparent canvas means flat = DOM everywhere, seams
// impossible; the price is ±1 rounding on PARTIALLY transparent artwork
// pixels (line antialiasing, the dissolve), which sit inside the drawing
// where ±1 is meaningless.
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

// The globe's lower half dissolves into the page: an alpha erase inside the
// canvas (the canvas is transparent, see above — erased pixels become the
// page's own DOM pixels). Applied in two stages so the map and the
// highlights dissolve on their own schedules: the map starts fading at 58%
// of the height, the highlights keep their true color almost to the bottom
// edge (85%) and only fade there to avoid a hard clip at the canvas edge.
function fadeOut(ctx, w, h, from) {
  const g = ctx.createLinearGradient(0, h * from, 0, h)
  g.addColorStop(0, 'rgba(0,0,0,0)')
  g.addColorStop(1, 'rgba(0,0,0,1)')
  ctx.globalCompositeOperation = 'destination-out'
  ctx.fillStyle = g
  ctx.fillRect(0, 0, w, h)
  ctx.globalCompositeOperation = 'source-over'
}
const MAP_FADE_START = 0.58
const HIGHLIGHT_FADE_START = 0.85

// The tour flight, driven by one normalized progress p in [0,1]. The camera is
// zoomed IN on a location (its surface fills the frame) and pulls back to the
// whole planet mid-flight, so you see the globe travel then dive into the next
// location. Both figures are multiples of the fit radius: REST > 1 overflows
// the circular frame (a close-up), TRAVEL < 1 sits inside it (the planet floats
// in space). The frame itself is a hard circular clip — nothing renders past it.
const TOUR_FLIGHT_MS = 1400 // time to travel between two locations

// The rest zoom is computed PER LOCATION so the country plus a margin fits the
// frame: the country fills TOUR_FILL of the frame radius, the rest is breathing
// room ("a little more than the territory"). Clamped so a tiny country doesn't
// zoom into blocky 110m coastlines and a huge one still reads as a close-up.
const TOUR_FILL = 0.55
const TOUR_ZOOM_MIN = 1.15
const TOUR_ZOOM_MAX = 6.0
// A country of angular radius angRad, framed to TOUR_FILL of a baseR frame.
const fitScale = (baseR, angRad) => {
  const r = (baseR * TOUR_FILL) / Math.sin(Math.min(angRad, 1.2))
  return Math.max(baseR * TOUR_ZOOM_MIN, Math.min(baseR * TOUR_ZOOM_MAX, r))
}

// One gentle S-curve — sine ease-in-out — drives BOTH the turn and the zoom, in
// lock-step over the same flight time: soft start, soft end, a rounded (not
// abrupt) middle. No arc, so the zoom just travels A→B once, no out-and-back.
const easeInOut = (p) => (1 - Math.cos(Math.PI * p)) / 2

// Blend two [r,g,b] arrays; used to cross-fade a location's highlight colour as
// the selection moves, instead of snapping.
const lerpRgb = (a, b, t) => [
  Math.round(a[0] + (b[0] - a[0]) * t),
  Math.round(a[1] + (b[1] - a[1]) * t),
  Math.round(a[2] + (b[2] - a[2]) * t),
]

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

// Territories missing from the 110m atlas (city-states / SARs), with their
// real outline pre-extracted from the 50m atlas into a tiny committed JSON,
// and a scale factor. At true scale Hong Kong is ~5px on this globe —
// technically honest, practically invisible. Scaling the real outline around
// its own centroid keeps the recognizable silhouette and the same visual
// language as every other country, just readable. Common cartographic
// practice for city-states; tune or set to 1 to go purist.
const EXTRA_FEATURES = { HK: { feat: hkFeature, scale: 2.25 } }

function scaleFeature(feat, factor) {
  const [cx, cy] = geoCentroid(feat)
  const scale = (c) =>
    Array.isArray(c[0]) ? c.map(scale) : [cx + (c[0] - cx) * factor, cy + (c[1] - cy) * factor]
  return {
    ...feat,
    geometry: { ...feat.geometry, coordinates: scale(feat.geometry.coordinates) },
  }
}

// The angular radius of a feature about a point: the greatest great-circle
// distance (radians) from that point to any of its vertices. It tells the tour
// how far to zoom so the whole country — plus a margin — fits the frame.
function featureAngularRadius(feat, about) {
  let max = 0
  const walk = (c) => {
    if (typeof c[0] === 'number') {
      const d = geoDistance(about, c)
      if (d > max) max = d
    } else {
      for (const x of c) walk(x)
    }
  }
  walk(feat.geometry.coordinates)
  return max
}

function buildHighlights(locations) {
  return locations
    .map((loc) => {
      const numeric = ISO_NUMERIC[loc.code]
      let feat = numeric ? countriesFC.features.find((f) => f.id === numeric) : null
      if (!feat && EXTRA_FEATURES[loc.code]) {
        const extra = EXTRA_FEATURES[loc.code]
        feat = scaleFeature(extra.feat, extra.scale)
      }
      if (!feat) return null
      const centroid = CENTROID_OVERRIDE[loc.code] || geoCentroid(feat)
      // Radius about the CAMERA target (centroid override for the US, etc.), so
      // the fit accounts for where the camera actually points.
      const angRad = Math.max(0.03, featureAngularRadius(feat, centroid))
      return { id: loc.id, feat, centroid, angRad }
    })
    .filter(Boolean)
}

// variant: 'backdrop' (default) is the desktop hero horizon — the sphere sits
// below the canvas and only its crown shows behind the copy. 'full' centres the
// whole sphere in the canvas, so the location flown to face the viewer lands in
// the middle where it can actually be seen — the mode the mobile auto-tour uses.
// maxDpr caps the backing-store resolution (a 60fps canvas at dpr 3+ on a phone
// is pure heat); 'full' also skips the dissolve, which only makes sense for a
// horizon fading into the page.
export default function Globe({
  locations,
  selected,
  onSelect,
  theme,
  autoRotate = true,
  variant = 'backdrop',
  maxDpr = 2,
  style,
}) {
  const canvasRef = useRef(null)
  // Animation state lives in a ref, not React state: the draw loop runs at 60fps
  // and must never trigger a re-render.
  // The sphere's centre sits BELOW the canvas (see the projection), so what
  // shows is the crown: points more than ~11° from the projection centre. At
  // the original 11°N centre, Hong Kong (22°N) fell barely 11° out — right on
  // the bottom edge, inside the dissolve, where it read as a faint smudge.
  // 6°N is the middle ground: Hong Kong clears the edge by 16°, and the tilt
  // stays close to the original (the equator felt like too much of a swing).
  const anim = useRef({ rotation: [-14, -6], flyTarget: null })

  // Resolving a location to an atlas feature scans 177 geometries and runs
  // geoCentroid, so it happens once per location change — never inside the loop.
  const highlights = useMemo(() => buildHighlights(locations), [locations])

  const latest = useRef({ highlights, selected, theme, autoRotate })
  latest.current = { highlights, selected, theme, autoRotate }

  // Fly to whichever location the surrounding UI selected. The 'full' tour
  // flies on an explicit timed progress (so one curve drives both the turn
  // speed and the zoom arc); the 'backdrop' desktop globe keeps its simple
  // ease-to-target. `from`/`t0` are captured on the first frame of the flight.
  useEffect(() => {
    if (selected == null) return
    const hl = highlights.find((h) => h.id === selected)
    if (!hl) return
    const to = [-hl.centroid[0], -hl.centroid[1]]
    if (variant === 'full') anim.current.flight = { to, from: null, t0: 0, angRad: hl.angRad }
    else anim.current.flyTarget = to
  }, [selected, highlights, variant])

  useEffect(() => {
    const cv = canvasRef.current
    if (!cv) return
    let raf = 0
    let projection = null
    let w = 0, h = 0, dpr = 1
    // The fit radius for the 'full' variant; the draw loop eases the live scale
    // around it for the tour's zoom (pull back to travel, push in on arrival).
    let baseR = 0


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
      dpr = Math.min(maxDpr, window.devicePixelRatio || 1)
      cv.width = w * dpr
      cv.height = h * dpr

      if (variant === 'full') {
        // Whole sphere centred in the box, sized to fit with a small margin so
        // the flown-to location sits dead centre and fully visible.
        baseR = Math.min(w, h) * 0.5
        projection = geoOrthographic().scale(baseR).translate([w * 0.5, h * 0.5]).clipAngle(90)
        return
      }

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

      if (variant === 'full') {
        const fl = st.flight
        if (st.scaleNow == null) st.scaleNow = baseR * TOUR_ZOOM_MIN
        if (fl) {
          const now = performance.now()
          if (fl.from == null) {
            // First frame: anchor turn and zoom to where they are now, and take
            // the shortest way round in longitude. The zoom target is this
            // location's own fit scale — big country, gentle zoom; small, close.
            fl.from = [st.rotation[0], st.rotation[1]]
            fl.t0 = now
            let dl = fl.to[0] - fl.from[0]
            while (dl > 180) dl -= 360
            while (dl < -180) dl += 360
            fl.d = [dl, fl.to[1] - fl.from[1]]
            fl.fromScale = st.scaleNow
            fl.toScale = fitScale(baseR, fl.angRad)
          }
          const p = Math.min(1, (now - fl.t0) / TOUR_FLIGHT_MS)
          const e = easeInOut(p) // one gentle curve drives both, in lock-step
          st.rotation = [fl.from[0] + fl.d[0] * e, fl.from[1] + fl.d[1] * e]
          // Zoom is perceived logarithmically — equal RATIOS read as equal steps —
          // so interpolate scale geometrically (from * (to/from)^e), not linearly.
          // A linear lerp of a 6x->1.4x change crawls near the high zoom and races
          // near the low one, which is the "exponential" feel; log-space is even.
          st.scaleNow = fl.fromScale * Math.pow(fl.toScale / fl.fromScale, e)
          if (p >= 1) {
            st.rotation = [fl.to[0], fl.to[1]]
            st.scaleNow = fl.toScale
            st.flight = null
          }
        }
        projection.scale(st.scaleNow)
      } else if (st.flyTarget) {
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
      const ctx = cv.getContext('2d')

      // The page's own theme crossfade is plain CSS (App.jsx). The canvas is
      // transparent, so its flat area IS the page and needs nothing; only
      // the drawn artwork's colors are lerped here, over the same 400ms and
      // curve as the CSS transition. A small phase difference is possible
      // and fine — these colors live inside the artwork, never meeting the
      // page background across a flat edge.
      const themeKey = theme === 'dark' ? 'dark' : 'light'
      if (st.palTheme !== themeKey) {
        st.palTheme = themeKey
        st.palTarget = PAL_RGB[themeKey]
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
      ctx.clearRect(0, 0, w, h)
      // The frame: nothing renders past a fixed circle the size of the fit
      // radius. When zoomed in past it (a close-up) the surface is cropped to
      // this porthole; when the planet floats inside it (travel) there is space
      // around it. Full variant only — the backdrop horizon has no frame.
      if (variant === 'full') {
        ctx.beginPath()
        ctx.arc(w / 2, h / 2, baseR, 0, 2 * Math.PI)
        ctx.clip()
      }
      const path = geoPath(proj, ctx)

      // Structural map first — graticule, land, borders — then its fade, so
      // the highlights drawn AFTER are untouched by it.
      ctx.beginPath(); path(geoGraticule().step([20, 20])()); ctx.strokeStyle = C('grat'); ctx.lineWidth = 0.6; ctx.stroke()
      ctx.beginPath(); path(countriesFC); ctx.fillStyle = C('land'); ctx.fill()
      ctx.beginPath(); path(borders); ctx.strokeStyle = C('border'); ctx.lineWidth = 0.5; ctx.stroke()
      ctx.beginPath(); path(coast); ctx.strokeStyle = C('coast'); ctx.lineWidth = 0.7; ctx.stroke()
      // The dissolve only makes sense for the backdrop horizon fading into the
      // page; a fully-shown sphere keeps its whole disc.
      if (variant !== 'full') fadeOut(ctx, w, h, MAP_FADE_START)

      // Each location eases its own "selected amount" 0..1 toward whether it is
      // the current one, so the highlight colour cross-fades as the tour moves
      // instead of snapping between hi and hiSel.
      if (!st.selAmt) st.selAmt = {}
      const rgb = (k) => st.pal[k]
      for (const hl of highlights) {
        const goal = hl.id === selected ? 1 : 0
        const cur = st.selAmt[hl.id] ?? goal
        const amt = cur + (goal - cur) * 0.09
        st.selAmt[hl.id] = amt
        const fill = lerpRgb(rgb('hi'), rgb('hiSel'), amt)
        const line = lerpRgb(rgb('hiLine'), rgb('hiSelLine'), amt)
        ctx.beginPath(); path(hl.feat)
        ctx.fillStyle = `rgb(${fill[0]},${fill[1]},${fill[2]})`
        ctx.fill()
        ctx.strokeStyle = `rgb(${line[0]},${line[1]},${line[2]})`
        ctx.lineWidth = 0.8 + 0.4 * amt
        ctx.stroke()
      }
      if (variant !== 'full') fadeOut(ctx, w, h, HIGHLIGHT_FADE_START)

      // Edge vignette: a radial ERASE toward the rim (destination-out), so the
      // globe dissolves into the page toward the frame's edge instead of
      // ending on a hard circle. On the dark theme that reads as the darkening
      // the design asked for; it works on either theme because it fades to the
      // page's own background, not to a fixed colour. Full variant only.
      if (variant === 'full') {
        const g = ctx.createRadialGradient(w / 2, h / 2, baseR * 0.68, w / 2, h / 2, baseR)
        g.addColorStop(0, 'rgba(0,0,0,0)')
        g.addColorStop(1, 'rgba(0,0,0,1)')
        ctx.globalCompositeOperation = 'destination-out'
        ctx.fillStyle = g
        ctx.beginPath(); ctx.arc(w / 2, h / 2, baseR, 0, 2 * Math.PI); ctx.fill()
        ctx.globalCompositeOperation = 'source-over'
      }
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
