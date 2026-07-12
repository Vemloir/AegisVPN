import { useEffect, useMemo, useRef } from 'react'
import { geoOrthographic, geoPath, geoGraticule, geoCentroid, geoDistance } from 'd3-geo'
import { feature, mesh } from 'topojson-client'
import world from 'world-atlas/countries-110m.json'
import { CENTROID_OVERRIDE, ISO_NUMERIC, POINT_LOCATION } from './countries.js'

// Ocean matches the page's --bg exactly in each theme. Land/grat/border/coast
// (dark) had blue nudged 1-2 points above red/green, same bias as the old
// DARK_VARS — neutralized to true grey. Land is pulled MUCH closer to ocean
// (gap of ~7 instead of ~36) so the whole globe reads as a subtler part of
// the page rather than a distinct light-grey block sitting on it.
//
// hi/hiSel/hiLine/hiSelLine (fill and outline of highlighted locations) are
// IDENTICAL in both themes by explicit request — the highlight is the brand
// accent and must not shift between themes. Only the map's structural colors
// (ocean/land/graticule/borders) differ per theme.
const HIGHLIGHT = { hi: '#CC785C', hiSel: '#C2613D', hiLine: '#A34E2F', hiSelLine: '#A34E2F' }
const PALETTE = {
  light: { ocean: '#F3F1EA', land: '#D5D0C2', grat: '#DBD6C9', border: '#C6C0B0', coast: '#BDB7A6', ...HIGHLIGHT },
  dark: { ocean: '#161616', land: '#1D1D1D', grat: '#232323', border: '#333333', coast: '#333333', ...HIGHLIGHT },
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
      const ctx = cv.getContext('2d')
      const pal = PALETTE[theme === 'dark' ? 'dark' : 'light']

      ctx.save()
      ctx.scale(dpr, dpr)
      ctx.clearRect(0, 0, w, h)
      const path = geoPath(proj, ctx)

      ctx.beginPath(); path({ type: 'Sphere' }); ctx.fillStyle = pal.ocean; ctx.fill()
      ctx.beginPath(); path(geoGraticule().step([20, 20])()); ctx.strokeStyle = pal.grat; ctx.lineWidth = 0.6; ctx.stroke()
      ctx.beginPath(); path(countriesFC); ctx.fillStyle = pal.land; ctx.fill()

      for (const hl of highlights) {
        if (!hl.feat) continue
        ctx.beginPath(); path(hl.feat)
        ctx.fillStyle = hl.id === selected ? pal.hiSel : pal.hi
        ctx.fill()
      }
      ctx.beginPath(); path(borders); ctx.strokeStyle = pal.border; ctx.lineWidth = 0.5; ctx.stroke()
      ctx.beginPath(); path(coast); ctx.strokeStyle = pal.coast; ctx.lineWidth = 0.7; ctx.stroke()
      for (const hl of highlights) {
        if (!hl.feat) continue
        ctx.beginPath(); path(hl.feat)
        // All ~5 locations are highlighted at once (not just the selected one),
        // so this color renders 5x every frame regardless of selection — it
        // must vary with selection too, not just the line width.
        ctx.strokeStyle = hl.id === selected ? pal.hiSelLine : pal.hiLine
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
        ctx.fillStyle = sel ? pal.hiSel : pal.hi
        ctx.fill()
        ctx.strokeStyle = sel ? pal.hiSelLine : pal.hiLine
        ctx.lineWidth = sel ? 1 : 0.8
        ctx.stroke()
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
