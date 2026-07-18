import { useCallback, useEffect, useRef, useState } from 'react'
import { css, useHoverStyle } from './css.js'
import { dict } from './i18n.js'
import Globe from './Globe.jsx'
import TelegramLogin from './TelegramLogin.jsx'
import { regionOf } from './countries.js'
import * as api from './api.js'

const BOT_NAME = 'AegisEcoVPN_bot'
const BOT_URL = `https://t.me/${BOT_NAME}`

// In-page nav scrolls to a section without ever touching the URL — no
// #features/#pricing hash junk in the address bar or browser history.
function scrollToSection(e, id) {
  e.preventDefault()
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

// Section backgrounds are all --bg (no alternating panel color between
// sections); the surface color that used to alternate the sections now
// belongs to the cards/panels INSIDE them instead (--card).
const LIGHT_VARS =
  '--bg:#F3F1EA; --ink:#1C1B19; --muted:#56524B; --muted2:#8A857B; --faint:#9A958B;' +
  '--hair:#E4E0D6; --hair2:#DFDBD0; --hair3:#D7D2C5; --accent:#C2613D; --accentSoft:#CC785C;' +
  '--btn:#1A1A1A; --btnHover:#38322D; --btnText:#ffffff; --card:#ECE8DD; --seg:#E0DBCF; --segActive:#FBFAF6;' +
  '--codeBg:#EDE9DF; --rowHover:#EDE9DF; --logoBg:#1A1A1A; --logoText:#ffffff;' +
  // Frosted-glass surfaces (header, the globe's CTA): a translucent tint of the
  // page plus a blur, an edge line, and a light top highlight — the highlight is
  // what makes it read as a pane of glass rather than a flat see-through box.
  '--glassBg:rgba(243,241,234,.62); --glassEdge:rgba(28,27,25,.09); --glassHi:rgba(255,255,255,.75);'

// Every background/border neutral below had blue nudged 1-2 points above red
// and green (a common "default dark UI" recipe) — individually invisible, but
// summed across every card/border/panel on the page it read as a cool/blue
// cast. Neutralized to true grey (R=G=B) at the same lightness; ink/muted/
// faint were already warm-leaning (R>G>B) and are untouched.
const DARK_VARS =
  '--bg:#161616; --ink:#ECEAE6; --muted:#A8A6A2; --muted2:#827F7A; --faint:#6E6B66;' +
  '--hair:#2E2E2E; --hair2:#262626; --hair3:#2E2E2E; --accent:#C2613D; --accentSoft:#CC785C;' +
  '--btn:#C2613D; --btnHover:#D07A55; --btnText:#ffffff; --card:#1F1F1F; --seg:#252525; --segActive:#373737;' +
  '--codeBg:#262626; --rowHover:#202020; --logoBg:#E6A085; --logoText:#1A140F;' +
  '--glassBg:rgba(22,22,22,.58); --glassEdge:rgba(255,255,255,.09); --glassHi:rgba(255,255,255,.07);'

const fmt = (n) => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ' ')

// Each section becomes a full-height slide on a phone: its content is centred
// in one screenful so the part gets room instead of stacking tightly against
// the next. Height is 100svh (the SMALL viewport — the height with the browser
// toolbar shown), NOT 100dvh: dvh tracks the toolbar live, so as it collapses
// and re-appears during a scroll the slides resize and the content jumps a few
// px against the scroll — the "teleport". svh is fixed, so it never reflows
// mid-scroll; the cost is a small strip of slack below a slide once the
// toolbar hides, which is invisible next to a jump. minHeight so a section
// taller than the screen (long location list) still grows past it.
const MOBILE_SLIDE = {
  minHeight: '100svh',
  display: 'flex',
  flexDirection: 'column',
  justifyContent: 'center',
  boxSizing: 'border-box',
}

// Glass is used ONLY by the sticky header, and that is not a style preference
// but the one place it does anything: frosted glass shows its character by
// blurring what is BEHIND it, and the header is the only surface that content
// scrolls under. Over the page's flat background a translucent tint plus a blur
// resolves to the same flat colour — visually identical to an opaque card, for
// the price of an extra GPU layer and ±1 compositing noise at every edge. It was
// tried on the cards, panels and pills, and it changed nothing on screen.

/* ---------------------------------------------------------------- primitives */

function HoverLink({ base, hover, children, ...rest }) {
  return <a {...useHoverStyle(base, hover)} {...rest}>{children}</a>
}

function HoverButton({ base, hover, children, ...rest }) {
  return <button {...useHoverStyle(base, hover)} {...rest}>{children}</button>
}

const MODAL_EXIT_MS = 160

// React unmounts a component the moment its flag flips, so a dialog cannot
// animate itself away from the outside: the element is already gone. The Modal
// therefore owns its own dismissal — it plays the exit, THEN tells the parent
// to unmount it. Every route out (backdrop, Escape, the ×) goes through
// requestClose, so none of them can skip the animation.
//
// onClose may be null, which means "not dismissible right now" (the checkout
// modal while a payment is being opened): every route out becomes a no-op.
function Modal({ onClose, children, maxWidth = 420 }) {
  const [closing, setClosing] = useState(false)

  const requestClose = useCallback(() => {
    if (!onClose || closing) return
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      onClose()
      return
    }
    setClosing(true)
    setTimeout(onClose, MODAL_EXIT_MS)
  }, [onClose, closing])

  useEffect(() => {
    const esc = (e) => e.key === 'Escape' && requestClose()
    window.addEventListener('keydown', esc)
    return () => window.removeEventListener('keydown', esc)
  }, [requestClose])

  return (
    <div
      onClick={requestClose}
      // The scrim is the PAGE's own background colour at 78%, not a dark film:
      // a black veil over a cream page turns it brown, which is what "the
      // background changes colour" was. This only fades the page toward its own
      // background. It cannot be dropped entirely — the modal card (--card)
      // sits a hair away from --bg in the light theme and would dissolve into a
      // merely blurred page — so the card carries a real shadow to stay lifted.
      style={{
        ...css('position:fixed; inset:0; z-index:120; display:flex; align-items:center; justify-content:center; padding:clamp(12px,4vw,24px);'),
        background: 'color-mix(in srgb, var(--bg) 78%, transparent)',
        backdropFilter: 'blur(6px)',
        WebkitBackdropFilter: 'blur(6px)',
        animation: closing
          ? `vpnScrimOut ${MODAL_EXIT_MS}ms ease forwards`
          : 'vpnScrimIn .18s ease',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          ...css('position:relative; width:100%; background:var(--card); border:1px solid var(--hair2); border-radius:20px; padding:32px 28px;'),
          maxWidth,
          boxShadow: '0 24px 60px -12px rgba(0,0,0,.35), 0 0 0 1px rgba(0,0,0,.04)',
          animation: closing
            ? `vpnDialogOut ${MODAL_EXIT_MS}ms ease forwards`
            : 'vpnDialogIn .22s cubic-bezier(.22,.61,.36,1)',
        }}
      >
        {children}
        <button
          onClick={requestClose}
          aria-label="Close"
          style={css('position:absolute; top:18px; right:18px; width:32px; height:32px; border:none; background:transparent; color:var(--muted2); font-size:22px; line-height:1; cursor:pointer; border-radius:8px; transition:color .16s ease;')}
        >×</button>
      </div>
    </div>
  )
}

const TG_ICON = (
  <svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor">
    <path d="M16 8A8 8 0 1 1 0 8a8 8 0 0 1 16 0M8.287 5.906q-1.168.486-4.666 2.01-.567.225-.595.442c-.03.243.275.339.69.47l.175.055c.408.133.958.288 1.243.294q.39.01.868-.32 3.269-2.206 3.374-2.23c.05-.012.12-.026.166.016s.042.12.037.141c-.03.129-1.227 1.241-1.846 1.817-.193.18-.33.307-.358.336a8 8 0 0 1-.188.186c-.38.366-.664.64.015 1.088.327.216.589.393.85.571.284.194.568.387.936.629q.14.092.27.187c.331.236.63.448.997.414.214-.02.435-.22.547-.82.265-1.417.786-4.486.906-5.751a1.4 1.4 0 0 0-.013-.315.34.34 0 0 0-.114-.217.53.53 0 0 0-.31-.093c-.3.005-.763.166-2.984 1.09" />
  </svg>
)

// The Telegram avatar, with the initial as a fallback: photo_url is absent for
// users with no profile photo (or a privacy setting that hides it), and its
// CDN link can rot, so a failed load falls back to the initial too.
function Avatar({ user, size, fallbackLabel }) {
  const [broken, setBroken] = useState(false)
  const initial = (user.display_name || fallbackLabel || '?')[0].toUpperCase()
  if (!user.photo_url || broken) {
    return (
      <span
        style={{
          ...css('display:flex; align-items:center; justify-content:center; border-radius:999px; background:var(--logoBg); color:var(--logoText); font-weight:600;'),
          width: `${size}px`,
          height: `${size}px`,
          fontSize: `${Math.round(size * 0.42)}px`,
        }}
      >
        {initial}
      </span>
    )
  }
  return (
    <img
      src={user.photo_url}
      alt=""
      onError={() => setBroken(true)}
      referrerPolicy="no-referrer"
      style={{ width: `${size}px`, height: `${size}px`, borderRadius: '999px', objectFit: 'cover', display: 'block' }}
    />
  )
}

// Real SVG flags (flag-icons), NOT the emoji: the emoji renders as a glossy
// sticker on some systems and as bare "DE" letters on others. Each flag is a
// separate lazy chunk (Vite splits import.meta.glob), so only the handful of
// active locations' flags are ever fetched. Bundled, not from a CDN — our
// users are behind blocks, and an external flag host would be one more thing
// that fails for exactly them. Falls back to the emoji if a code has no SVG.
const flagUrls = import.meta.glob('../node_modules/flag-icons/flags/4x3/*.svg', {
  query: '?url',
  import: 'default',
})
// Map by the two-letter code pulled from each filename, so the lookup does not
// depend on how Vite spells the glob key (the exact prefix has bitten this).
const flagLoaderByCode = {}
for (const key in flagUrls) {
  const m = key.match(/\/([a-z]{2})\.svg$/)
  if (m) flagLoaderByCode[m[1]] = flagUrls[key]
}
function Flag({ code, emoji, height = 34 }) {
  const [url, setUrl] = useState(null)
  useEffect(() => {
    setUrl(null)
    const loader = flagLoaderByCode[(code || '').toLowerCase()]
    if (!loader) return
    let alive = true
    loader().then((u) => alive && setUrl(u))
    return () => {
      alive = false
    }
  }, [code])
  if (!url) return <span style={{ fontSize: `${height}px`, lineHeight: 1 }}>{emoji}</span>
  return (
    <img
      src={url}
      alt=""
      style={{
        height: `${height}px`,
        width: `${Math.round((height * 4) / 3)}px`,
        objectFit: 'cover',
        borderRadius: '5px',
        display: 'block',
        boxShadow: '0 0 0 1px rgba(0,0,0,.14)',
      }}
    />
  )
}

// Fade-and-rise as a block scrolls into view. One IntersectionObserver per
// block, disconnected after it fires — nothing keeps running once the page has
// been seen. Anyone who asked their OS for less motion gets the content
// immediately, with no transition at all.
function Reveal({ children, delay = 0, style }) {
  const ref = useRef(null)
  const [shown, setShown] = useState(false)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      setShown(true)
      return
    }
    const io = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setShown(true)
          io.disconnect()
        }
      },
      // Fire a little before the block is fully in view, so the motion is
      // finishing as the reader's eye arrives rather than starting then.
      { threshold: 0.08, rootMargin: '0px 0px -8% 0px' },
    )
    io.observe(el)
    return () => io.disconnect()
  }, [])

  return (
    <div
      ref={ref}
      style={{
        ...style,
        opacity: shown ? 1 : 0,
        transform: shown ? 'none' : 'translateY(16px)',
        transition: `opacity .55s ease ${delay}ms, transform .55s cubic-bezier(.22,.61,.36,1) ${delay}ms`,
      }}
    >
      {children}
    </div>
  )
}

// The consent box. A native checkbox cannot be animated (the browser paints
// the tick), so the input is kept for semantics and keyboard/screen-reader
// behaviour but visually replaced: the box fills with the accent and the tick
// draws itself along its own path.
function Checkbox({ checked, onChange }) {
  return (
    <span style={{ position: 'relative', display: 'inline-flex', flexShrink: 0, marginTop: '1px' }}>
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        style={{ position: 'absolute', inset: 0, width: '18px', height: '18px', margin: 0, opacity: 0, cursor: 'pointer' }}
      />
      <span
        aria-hidden="true"
        style={{
          width: '18px',
          height: '18px',
          borderRadius: '6px',
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: checked ? 'var(--accent)' : 'transparent',
          border: `1px solid ${checked ? 'var(--accent)' : 'var(--hair3)'}`,
          transition: 'background .16s ease, border-color .16s ease',
        }}
      >
        {checked && (
          <svg width="11" height="11" viewBox="0 0 12 12" fill="none">
            <path
              d="M2 6.2L4.6 8.8L10 3.4"
              stroke="#fff"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
              style={{ strokeDasharray: 22, animation: 'vpnCheckDraw .22s ease forwards' }}
            />
          </svg>
        )}
      </span>
    </span>
  )
}

// Fully custom dropdown for the plan term. The native <select>'s closed
// state can be restyled (appearance:none), but the OPEN menu is drawn by the
// browser and clashes with the site. The list is a handful of terms, so a
// hand-rolled listbox with outside-click / Esc / arrow-key handling beats
// pulling in a component library.
function TermSelect({ plans, value, onChange, lang, t, savingsPct }) {
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(-1) // keyboard/hover-highlighted index
  const rootRef = useRef(null)

  const idx = plans.findIndex((p) => p.id === value)
  const label = (p) => termLabel(p.days, t, lang)
  const pick = (p) => {
    onChange(p.id)
    setOpen(false)
  }

  useEffect(() => {
    if (!open) return
    const onDoc = (e) => {
      if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false)
    }
    const onKey = (e) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  // The button keeps focus while the menu is open (listbox pattern): arrows
  // move the highlight, Enter/Space picks. preventDefault on Enter/Space
  // stops the button's synthetic click from immediately re-toggling.
  const onButtonKey = (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      if (!open) {
        setOpen(true)
        setActive(idx)
        return
      }
      const d = e.key === 'ArrowDown' ? 1 : -1
      setActive((a) => ((a < 0 ? idx : a) + d + plans.length) % plans.length)
    } else if ((e.key === 'Enter' || e.key === ' ') && open) {
      e.preventDefault()
      if (active >= 0) pick(plans[active])
      else setOpen(false)
    }
  }

  return (
    <div ref={rootRef} style={{ position: 'relative' }}>
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => {
          setOpen(!open)
          setActive(idx)
        }}
        onKeyDown={onButtonKey}
        style={css(
          'display:inline-flex; align-items:center; gap:10px; font-family:inherit; ' +
          'font-size:14px; font-weight:600; color:var(--ink); background:var(--seg); ' +
          'border:1px solid var(--hair2); border-radius:999px; padding:8px 16px; cursor:pointer;',
        )}
      >
        {idx >= 0 ? label(plans[idx]) : ''}
        <svg
          width="10" height="6" viewBox="0 0 10 6" fill="none"
          style={{ transition: 'transform .15s ease', transform: open ? 'rotate(180deg)' : 'none' }}
        >
          <path d="M1 1L5 5L9 1" stroke="var(--muted2)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {/* The menu DROPS DOWN below the button as the same pill, stretched:
          the button's width and horizontal position, its background/border,
          and its CORNER radius (18px = half the 36px button height —
          quarter-circle corners, not a capsule's semicircular caps). The
          selected row's check sits where the button's chevron sits. */}
      {open && (
        <div
          role="listbox"
          style={{
            ...css(
              'position:absolute; right:0; top:calc(100% + 6px); z-index:30; min-width:100%; ' +
              'background:var(--seg); border:1px solid var(--hair2); border-radius:18px; ' +
              'padding:4px; display:flex; flex-direction:column; gap:2px;',
            ),
            animation: 'vpnMenuIn .12s ease',
          }}
        >
          {plans.map((p, i) => (
            <button
              key={p.id}
              type="button"
              role="option"
              aria-selected={p.id === value}
              onClick={() => pick(p)}
              onMouseEnter={() => setActive(i)}
              style={css(
                'display:flex; align-items:center; justify-content:space-between; gap:10px; ' +
                'padding:6px 12px; border:none; border-radius:999px; white-space:nowrap; ' +
                'font-family:inherit; font-size:14px; font-weight:600; cursor:pointer; ' +
                `background:${active === i ? 'var(--segActive)' : 'transparent'}; ` +
                `color:${p.id === value ? 'var(--accent)' : 'var(--ink)'};`,
              )}
            >
              <span style={css('display:inline-flex; align-items:center; gap:8px;')}>
                {label(p)}
                {/* The saving belongs HERE as much as on the card: the dropdown
                    is where the terms are actually compared. */}
                {savingsPct?.(p) > 0 && (
                  <span style={css('font-size:11.5px; font-weight:600; color:var(--accent);')}>
                    −{savingsPct(p)}%
                  </span>
                )}
              </span>
              <span style={{ visibility: p.id === value ? 'visible' : 'hidden', color: 'var(--accent)' }}>✓</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

// The card itself: fills the Reveal wrapper that carries the flex sizing, and
// lifts a couple of pixels under the pointer so the row feels responsive
// rather than printed.
//
// The resting transform is translateY(0), NOT 'none': switching between 'none'
// and a transform makes the browser add/remove a compositing layer, and text
// re-rasterized on and off that layer lands on slightly different subpixels —
// the "some rows jump 1px on hover" the design showed. Keeping a transform at
// rest keeps the card on one layer the whole time, so only the offset animates.
function PlanCard({ children }) {
  const [hover, setHover] = useState(false)
  return (
    <div
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        // Header pinned to the top, button to the bottom, and the feature list
        // between them GROWS to fill the height (flex:1), spreading its rows
        // evenly through the space rather than leaving a couple of big gaps.
        ...css('display:flex; flex-direction:column; width:100%; min-height:440px; padding:30px 34px; border-radius:24px; background:var(--card); text-align:left;'),
        transform: hover ? 'translateY(-3px)' : 'translateY(0)',
        transition: 'transform .18s ease',
        willChange: 'transform',
      }}
    >
      {children}
    </div>
  )
}

// A plan of 0 days is the lifetime plan, not a zero-day one — every place that
// prints a term must know that, or it renders "0 дн.".
const termLabel = (days, t, lang) =>
  days === 0 ? t.plan_lifetime : `${days} ${lang === 'en' ? 'days' : 'дн.'}`

// Drawn, not the ⭐ emoji: the emoji is a bright yellow blob that belongs to no
// palette here, and it renders as a tofu box wherever an emoji font is missing.
// This inherits currentColor, so it takes the colour of whatever text it sits in.
const Star = ({ size = 15 }) => (
  <svg
    viewBox="0 0 24 24"
    width={size}
    height={size}
    fill="currentColor"
    aria-hidden="true"
    style={{ verticalAlign: '-0.08em', flexShrink: 0 }}
  >
    <path d="M12 2.6l2.72 5.86 6.28.8-4.63 4.35 1.2 6.29L12 16.8l-5.57 3.1 1.2-6.29L3 9.26l6.28-.8z" />
  </svg>
)


/* ---------------------------------------------------------------------- app  */

export default function App() {
  const [lang, setLang] = useState(() => localStorage.getItem('aegis_lang') || 'ru')
  const [theme, setTheme] = useState(
    () =>
      localStorage.getItem('aegis_theme') ||
      (window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'),
  )
  const [selected, setSelected] = useState(null)
  const [isMobile, setIsMobile] = useState(() => window.innerWidth <= 900)
  const [menuOpen, setMenuOpen] = useState(false)

  // The hero description must visually align with the H1 beside it: the cap
  // top of its first line level with the H1's cap top, the BASELINE of its
  // last line level with the H1's last baseline. Comparing boxes is not
  // enough — a line box includes leading above the caps and room below the
  // baseline for descenders (the hooks of y/g/р), and those must not count.
  // So real font metrics (cap height, ascent, descent) are measured via
  // canvas, and the paragraph's line-height and top offset are solved so
  // caps and baselines land exactly level: from "para cap top == H1 cap top"
  // and "para last baseline == H1 last baseline" the half-leading terms
  // cancel and line-height = (H1 capTop→lastBaseline span − para cap height)
  // / (para lines − 1). Falls back to plain flow when the solution would be
  // unreadably tight (narrow viewports wrap the text into many short lines).
  const heroH1Ref = useRef(null)
  const heroSubRef = useRef(null)
  const [heroSubFit, setHeroSubFit] = useState(null) // { lh, mt } in px
  useEffect(() => {
    const h1 = heroH1Ref.current
    const p = heroSubRef.current
    if (!h1 || !p) return
    const ctx = document.createElement('canvas').getContext('2d')
    // capChar comes from the element's own text so Cyrillic content measures
    // the fallback serif that actually renders it, not Newsreader.
    const metricsOf = (el) => {
      const cs = getComputedStyle(el)
      ctx.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`
      const m = ctx.measureText('Hg')
      return {
        lh: parseFloat(cs.lineHeight),
        fs: parseFloat(cs.fontSize),
        ascent: m.fontBoundingBoxAscent,
        descent: m.fontBoundingBoxDescent,
        cap: ctx.measureText((el.textContent || 'H').trim()[0] || 'H').actualBoundingBoxAscent,
      }
    }
    const measure = () => {
      if (isMobile) { setHeroSubFit(null); return }
      const m1 = metricsOf(h1)
      const mp = metricsOf(p)
      if (!m1.ascent || !mp.ascent) { setHeroSubFit(null); return }
      const lines1 = Math.max(1, Math.round(h1.getBoundingClientRect().height / m1.lh))
      const linesP = Math.max(1, Math.round(p.scrollHeight / mp.lh))
      if (linesP < 2) { setHeroSubFit(null); return }
      const halfLead1 = (m1.lh - m1.ascent - m1.descent) / 2
      const capTop1 = halfLead1 + m1.ascent - m1.cap
      const baseLast1 = (lines1 - 1) * m1.lh + halfLead1 + m1.ascent
      const lh = (baseLast1 - capTop1 - mp.cap) / (linesP - 1)
      if (!(lh >= mp.fs * 1.2)) { setHeroSubFit(null); return }
      const halfLeadP = (lh - mp.ascent - mp.descent) / 2
      const capTopP = halfLeadP + mp.ascent - mp.cap
      setHeroSubFit({ lh, mt: capTop1 - capTopP })
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(h1)
    ro.observe(p)
    return () => ro.disconnect()
  }, [isMobile, lang])

  const [locations, setLocations] = useState([])
  const [plans, setPlans] = useState([])
  const [selectedPlanId, setSelectedPlanId] = useState(null)
  const [user, setUser] = useState(null)
  const [authOpen, setAuthOpen] = useState(false)
  const [accountOpen, setAccountOpen] = useState(false)
  const [authError, setAuthError] = useState(false)
  const [copied, setCopied] = useState(false)

  // Which unit prices are DISPLAYED in; the payment method is chosen at
  // checkout, so this is presentation only.
  const [priceUnit, setPriceUnit] = useState('rub')

  const [checkoutOpen, setCheckoutOpen] = useState(false)
  const [pendingCheckout, setPendingCheckout] = useState(false) // signing in to buy
  const [consent, setConsent] = useState(false)
  const [payBusy, setPayBusy] = useState(null) // 'sbp' | 'stars' while redirecting
  const [payError, setPayError] = useState(null)
  const [legalDoc, setLegalDoc] = useState(null) // { title, text }
  // Set when the payer comes back from the payment page; the subscription is
  // granted asynchronously (Platega callback → bot), so the site polls for it.
  const [awaitingPayment, setAwaitingPayment] = useState(
    () => new URLSearchParams(window.location.search).get('paid') === '1',
  )

  const t = dict(lang)

  useEffect(() => { localStorage.setItem('aegis_lang', lang) }, [lang])

  useEffect(() => {
    localStorage.setItem('aegis_theme', theme)
    // Paint the theme onto <html>, not just our root div: the page background
    // outside the app (overscroll, scrollbars) would otherwise stay white, and
    // native controls would render in the wrong scheme.
    const root = document.documentElement
    root.style.colorScheme = theme
    root.style.background = theme === 'dark' ? '#161616' : '#F3F1EA'
  }, [theme])

  useEffect(() => {
    const onResize = () => setIsMobile(window.innerWidth <= 900)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  // The three sources of truth, all server-side. A failure leaves the arrays
  // empty rather than falling back to stale hardcoded copies.
  useEffect(() => {
    api.getLocations().then(setLocations).catch(() => setLocations([]))
    api.getPlans().then(setPlans).catch(() => setPlans([]))
    api.getMe().then(setUser).catch(() => setUser(null))
  }, [])

  // Signing in mid-purchase must not lose the purchase: the auth modal
  // remembers it was opened from checkout and hands control back afterwards.
  const onTelegramAuth = useCallback(async (payload) => {
    setAuthError(false)
    try {
      const me = await api.loginWithTelegram(payload)
      if (!me) throw new Error('rejected')
      setUser(me)
      setAuthOpen(false)
      if (pendingCheckout) {
        setPendingCheckout(false)
        setCheckoutOpen(true)
      } else {
        setAccountOpen(true)
      }
    } catch {
      setAuthError(true)
    }
  }, [pendingCheckout])

  const doLogout = useCallback(async () => {
    await api.logout().catch(() => {})
    setUser(null)
    setAccountOpen(false)
  }, [])

  const copySub = useCallback((url) => {
    navigator.clipboard?.writeText(url)
    setCopied(true)
    setTimeout(() => setCopied(false), 1600)
  }, [])

  // Plans differ only in term (days) and price — one card, term picked via a
  // dropdown, rather than a card per plan. Falls back to the first plan until
  // the user picks one (or if the picked id no longer exists in a fresh fetch).
  // Declared BEFORE the checkout callbacks: a useCallback dependency array is
  // evaluated at definition time, so referencing it later would hit the TDZ
  // and take the whole app down with a ReferenceError.
  // The plan the page opens on is the one the admin marked as base — the term
  // the business actually wants to sell. Falls back to the first plan only when
  // no base is set.
  const basePlan = plans.find((p) => p.is_base) || null
  const selectedPlan =
    plans.find((p) => p.id === selectedPlanId) || basePlan || plans[0]

  // How much cheaper (positive) or dearer (negative) a plan is PER MONTH than
  // the base one. Computed in whichever unit is on screen, so the toggle keeps
  // the comparison honest. A lifetime plan (days 0) has no per-month price and
  // is never compared.
  const savingsPct = useCallback(
    (plan) => {
      if (!basePlan || !plan || plan.id === basePlan.id) return null
      const priceOf = (p) => (priceUnit === 'stars' ? p.stars_price : p.rub_price)
      const monthly = (p) => (priceOf(p) && p.days ? priceOf(p) / (p.days / 30) : null)
      const a = monthly(plan)
      const b = monthly(basePlan)
      if (!a || !b) return null
      const pct = Math.round(((b - a) / b) * 100)
      return pct === 0 ? null : pct
    },
    [basePlan, priceUnit],
  )

  // "Buy" from a specific card: remember which plan, sign in first if needed,
  // then open the payment modal (which pays for the remembered plan).
  const startCheckout = useCallback((plan) => {
    setPayError(null)
    setConsent(false)
    if (plan) setSelectedPlanId(plan.id)
    if (!user) {
      setPendingCheckout(true)
      setAuthOpen(true)
      return
    }
    setCheckoutOpen(true)
  }, [user])

  const pay = useCallback(
    async (method) => {
      if (!selectedPlan || payBusy) return
      setPayError(null)
      setPayBusy(method)
      try {
        // Consent is recorded BEFORE the payment is created — the backend
        // refuses to sell to a user who hasn't accepted, and it must not be
        // possible to pay and only then be asked.
        if (user && !user.terms_accepted) {
          await api.acceptTerms()
          setUser({ ...user, terms_accepted: true })
        }
        const { url } = await api.checkout(selectedPlan.id, method)
        window.location.href = url
      } catch (e) {
        setPayBusy(null)
        setPayError(e.code === 'provider_error' ? t.pay_err_provider : t.pay_err_generic)
      }
    },
    [selectedPlan, payBusy, user, t],
  )

  const openLegal = useCallback(
    async (doc) => {
      const title = doc === 'tos' ? t.legal_tos : t.legal_privacy
      setLegalDoc({ title, text: null })
      try {
        const body = await api.getLegal(doc, lang)
        setLegalDoc({ title, text: body?.text || '' })
      } catch {
        setLegalDoc({ title, text: '' })
      }
    },
    [lang, t],
  )

  // Confirmation is asynchronous (provider → bot → DB), so a payer returning to
  // the site usually arrives a beat BEFORE the subscription exists. Poll a
  // while, then give up quietly rather than claiming failure — the money is not
  // lost, the bot will message them.
  useEffect(() => {
    if (!awaitingPayment) return
    let tries = 0
    const id = setInterval(async () => {
      tries += 1
      const me = await api.getMe().catch(() => null)
      if (me) setUser(me)
      if (me?.subscription?.is_active || tries >= 20) {
        clearInterval(id)
        setAwaitingPayment(false)
        if (me?.subscription?.is_active) setAccountOpen(true)
        window.history.replaceState(null, '', window.location.pathname)
      }
    }, 3000)
    return () => clearInterval(id)
  }, [awaitingPayment])

  // Mobile globe auto-tour: step `selected` through the active locations on a
  // timer so the globe flies from one to the next and the card names each. Only
  // on mobile — desktop keeps click-to-select. Deps are [isMobile, locations],
  // NOT selected, or every tick would restart the timer.
  const tourIdx = useRef(0)
  useEffect(() => {
    if (!isMobile || locations.length === 0) return
    // Land on one immediately so the card is never empty, then advance.
    const start = Math.max(0, locations.findIndex((l) => l.id === selected))
    tourIdx.current = start
    setSelected(locations[start].id)
    const id = setInterval(() => {
      tourIdx.current = (tourIdx.current + 1) % locations.length
      setSelected(locations[tourIdx.current].id)
    }, 3000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isMobile, locations])

  // The location the tour (or a desktop click) currently sits on, for the card.
  const tourLocation = locations.find((l) => l.id === selected) || null

  const rootStyle = {
    ...css(LIGHT_VARS),
    ...css(theme === 'dark' ? DARK_VARS : ''),
    // overflow-x:hidden here would establish this div as a scroll container,
    // which breaks `position:sticky` on the header (it starts sticking to THIS
    // box instead of the viewport). `clip` prevents the same horizontal
    // overflow without creating that container.
    //
    ...css("font-family:'Onest',-apple-system,sans-serif; color:var(--ink); background:var(--bg); overflow-x:clip; transition:background .4s ease, color .4s ease;"),
  }

  // Segmented controls fade between states instead of snapping.
  const langBtn = (on) =>
    'border:none;padding:6px 13px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit;' +
    'transition:background .18s ease, color .18s ease;' +
    (on ? 'background:var(--ink);color:var(--bg);' : 'background:transparent;color:var(--muted2);')

  const navLink = 'color:inherit; text-decoration:none; transition:color .16s ease;'
  const primaryBtn = 'display:inline-flex; align-items:center; justify-content:center; gap:8px; background:var(--btn); color:var(--btnText); border:none; font-size:15px; font-weight:500; padding:13px 22px; border-radius:999px; cursor:pointer; font-family:inherit; text-decoration:none; transition:background .18s ease, transform .12s ease;'

  return (
    // The build stamp lives on the root element, not in the footer: a reader
    // has no use for it, but "which build am I actually looking at?" stays
    // answerable in one glance at the DOM (it is what settled the long
    // 'nothing changed' hunt).
    <div style={rootStyle} data-build={__BUILD_STAMP__}>
      {/* ============================ HEADER ============================
          Frosted glass: a translucent tint of the page, a heavy blur, an edge
          line and a top highlight. NOTE this is knowingly re-introducing a
          semi-transparent layer, which the GPU compositor blends and re-
          quantizes ±1 — the eyedropper may again read a one-step difference at
          the header's edge. That was measured and judged acceptable for the
          look; the flat page background elsewhere is unaffected. */}
      <header
        style={{
          ...css('position:sticky; top:0; z-index:60; border-bottom:1px solid var(--glassEdge); transition:background .4s ease, border-color .4s ease;'),
          background: 'var(--glassBg)',
          backdropFilter: 'blur(20px) saturate(170%)',
          WebkitBackdropFilter: 'blur(20px) saturate(170%)',
          boxShadow: 'inset 0 1px 0 var(--glassHi)',
        }}
      >
        <div style={css('max-width:1180px; margin:0 auto; padding:15px clamp(16px,4vw,28px); display:flex; align-items:center; justify-content:space-between; gap:24px;')}>
          <a href="#top" onClick={(e) => scrollToSection(e, 'top')} style={css('display:flex; align-items:center; text-decoration:none; color:inherit;')}>
            <span style={css('font-weight:600; font-size:18px; letter-spacing:-.015em;')}>AegisVPN</span>
          </a>

          {!isMobile && (
            <nav style={css('display:flex; align-items:center; gap:22px; font-size:14.5px; color:var(--muted); flex-shrink:0;')}>
              {/* Nav follows the page order, which now puts pricing first. */}
              <HoverLink href="#pricing" onClick={(e) => scrollToSection(e, 'pricing')} base={navLink} hover="color:var(--accent);">{t.nav_pricing}</HoverLink>
              <HoverLink href="#servers" onClick={(e) => scrollToSection(e, 'servers')} base={navLink} hover="color:var(--accent);">{t.nav_servers}</HoverLink>

              <button
                onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
                aria-label="Theme"
                style={css('display:flex; align-items:center; justify-content:center; width:34px; height:34px; border:none; border-radius:999px; background:var(--seg); cursor:pointer; color:var(--ink); padding:0;')}
              >
                {theme === 'dark' ? '☾' : '☀'}
              </button>

              <div style={css('display:flex; border:none; border-radius:999px; overflow:hidden; background:var(--seg);')}>
                <button onClick={() => setLang('ru')} style={css(langBtn(lang === 'ru'))}>RU</button>
                <button onClick={() => setLang('en')} style={css(langBtn(lang === 'en'))}>EN</button>
              </div>

              {user ? (
                <button
                  onClick={() => setAccountOpen(true)}
                  aria-label="Account"
                  style={css('width:34px; height:34px; padding:0; overflow:hidden; border-radius:999px; border:none; cursor:pointer; background:var(--logoBg); color:var(--logoText); font-weight:600; font-size:14px; font-family:inherit;')}
                >
                  <Avatar user={user} size={34} fallbackLabel={t.acc_guest_label} />
                </button>
              ) : (
                <HoverButton
                  onClick={() => setAuthOpen(true)}
                  base={primaryBtn + 'white-space:nowrap; flex-shrink:0; padding:10px 18px; font-size:14px;'}
                  hover="background:var(--btnHover);"
                >
                  {t.auth_login}
                </HoverButton>
              )}
            </nav>
          )}

          {isMobile && (
            <button
              onClick={() => setMenuOpen(true)}
              aria-label="Menu"
              style={css('display:flex; align-items:center; justify-content:center; width:40px; height:40px; border:1px solid var(--hair2); border-radius:12px; background:transparent; cursor:pointer; color:var(--ink); padding:0;')}
            >☰</button>
          )}
        </div>
      </header>

      {menuOpen && (
        <Modal onClose={() => setMenuOpen(false)} maxWidth={480}>
          <nav style={css('display:flex; flex-direction:column; gap:4px; margin-bottom:28px; font-size:17px;')}>
            {[['pricing', t.nav_pricing], ['servers', t.nav_servers]].map(([id, label]) => (
              <a
                key={id}
                href={`#${id}`}
                onClick={(e) => { scrollToSection(e, id); setMenuOpen(false) }}
                style={css('color:inherit; text-decoration:none; padding:12px 0; border-bottom:1px solid var(--hair);')}
              >{label}</a>
            ))}
          </nav>
          <div style={css('display:flex; gap:10px; margin-bottom:14px;')}>
            <button onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')} style={css('width:40px; height:40px; border:1px solid var(--hair2); border-radius:12px; background:transparent; cursor:pointer; color:var(--ink);')}>{theme === 'dark' ? '☾' : '☀'}</button>
            <div style={css('display:flex; flex:1; border:1px solid var(--hair2); border-radius:12px; overflow:hidden;')}>
              <button onClick={() => setLang('ru')} style={css(langBtn(lang === 'ru') + 'flex:1; padding:10px 20px; font-size:14px;')}>RU</button>
              <button onClick={() => setLang('en')} style={css(langBtn(lang === 'en') + 'flex:1; padding:10px 20px; font-size:14px;')}>EN</button>
            </div>
          </div>
          <button
            onClick={() => { setMenuOpen(false); user ? setAccountOpen(true) : setAuthOpen(true) }}
            style={css('width:100%; border:1px solid var(--hair2); background:transparent; color:var(--ink); font-size:15.5px; font-weight:500; padding:14px; border-radius:12px; cursor:pointer; font-family:inherit;')}
          >
            {user ? t.acc_greeting : t.auth_login}
          </button>
        </Modal>
      )}

      {/* ============================= HERO ============================= */}
      {/* The clamp minimums are what a phone actually gets (10vw of 390px is
          below every one of them), so they are the mobile spacing — they were
          desktop-sized and left ~112px of dead air between sections. */}
      {/* On a phone every section is its own full-height slide: the content
          is vertically centred in 100dvh (dvh, not vh, so the mobile browser's
          collapsing toolbar doesn't leave a gap), giving each part room to
          breathe instead of stacking tightly. Desktop keeps the normal flow. */}
      <section
        id="top"
        style={{
          ...css('max-width:1180px; margin:0 auto; padding:clamp(28px,8vw,84px) clamp(16px,4.5vw,28px) clamp(20px,5vw,48px);'),
          // The hero holds the least content of the three slides, so centring
          // it in a full screen left a big empty band ABOVE the text — which
          // reads as a placeholder for a missing image. Anchor it in the upper
          // third instead: the slack falls BELOW the content, where empty space
          // just invites a scroll to the next screen.
          // Mobile only (desktop untouched): the merged hero+globe is tall
          // enough on its own, so drop the forced 100svh — that full-height
          // floated the content with empty air below. minHeight:auto hugs the
          // content; small top and bottom padding is the only breathing room.
          ...(isMobile ? { ...MOBILE_SLIDE, minHeight: 'auto', justifyContent: 'flex-start', paddingTop: '3vh', paddingBottom: '0' } : null),
        }}
      >
        {/* Centred on a phone (one column), left-aligned on the two-column
            desktop layout where the copy pairs with the sub-text column. */}
        <div style={{ display: 'grid', gap: isMobile ? '22px' : '48px', alignItems: 'start', gridTemplateColumns: isMobile ? '1fr' : '1.45fr 1fr', textAlign: isMobile ? 'center' : 'left' }}>
          <div style={css('min-width:0;')}>
            {/* The Xray/VLESS eyebrow is desktop-only now: on a phone the hero
                merges with the globe into the first screen, and this protocol
                line was cut to make room. */}
            {!isMobile && (
              <div style={css('display:inline-flex; align-items:center; gap:8px; font-size:13px; letter-spacing:.04em; text-transform:uppercase; color:var(--accent); font-weight:600; margin-bottom:24px;')}>
                <span style={css('width:6px; height:6px; border-radius:50%; background:var(--accentSoft); display:inline-block;')} />
                {t.hero_eyebrow}
              </div>
            )}
            <h1 ref={heroH1Ref} style={css("font-family:'Newsreader','EB Garamond',serif; font-weight:500; font-size:clamp(40px,5.2vw,66px); line-height:1.03; letter-spacing:-.02em; margin:0 0 28px; color:var(--ink);")}>
              {t.hero_l1}<br />
              <span style={css('font-style:italic; color:var(--accent);')}>{t.hero_l2}</span>
            </h1>
            {/* On a phone the two CTAs stack full-width — a big primary pill
                and a bordered secondary below it — instead of a big button
                cramped next to a small text link. Desktop keeps them side by
                side. */}
            <div style={css(
              isMobile
                ? 'display:flex; flex-direction:column; gap:12px; margin-top:6px;'
                : 'display:flex; align-items:center; gap:20px; flex-wrap:wrap;'
            )}>
              <HoverLink
                href="#pricing"
                onClick={(e) => scrollToSection(e, 'pricing')}
                base={'display:inline-flex; align-items:center; justify-content:center; gap:9px; background:var(--btn); color:var(--btnText); font-size:16px; font-weight:500; padding:14px 26px; border-radius:999px; text-decoration:none;' + (isMobile ? ' width:100%;' : '')}
                hover="background:var(--btnHover);"
              >
                {t.hero_pay_site}
              </HoverLink>
              <HoverLink
                href={BOT_URL}
                target="_blank"
                rel="noopener"
                base={
                  isMobile
                    ? 'display:inline-flex; align-items:center; justify-content:center; gap:7px; width:100%; font-size:16px; font-weight:500; color:var(--ink); text-decoration:none; padding:13px 26px; border:1px solid var(--hair2); border-radius:999px;'
                    : 'display:inline-flex; align-items:center; gap:7px; font-size:16px; font-weight:500; color:var(--ink); text-decoration:none; border-bottom:1px solid transparent; padding-bottom:2px;'
                }
                hover={isMobile ? 'background:var(--seg);' : 'border-bottom-color:var(--ink);'}
              >
                {TG_ICON} {t.cta_try}
              </HoverLink>
            </div>
          </div>
          {/* Sub-paragraph is desktop-only: on mobile the globe takes the space
              below the title, and this copy (which also led with Xray/VLESS)
              was cut with the eyebrow. */}
          {!isMobile && (
          <div style={css('min-width:0;')}>
            {!isMobile && (
              // Invisible clone of the eyebrow badge — not measured, just the
              // SAME markup, so it always occupies exactly the same height
              // (any margin included) regardless of language or viewport, and
              // pushes hero_sub's start down to line up with the H1's own
              // top, not the eyebrow's.
              <div
                aria-hidden="true"
                style={css('visibility:hidden; display:inline-flex; align-items:center; gap:8px; font-size:13px; letter-spacing:.04em; text-transform:uppercase; font-weight:600; margin-bottom:24px;')}
              >
                <span style={css('width:6px; height:6px; border-radius:50%; display:inline-block;')} />
                {t.hero_eyebrow}
              </div>
            )}
            {/* The 1.85 line-height is the desktop value, tuned to fill the
                H1's height in the two-column layout. On a phone there is no H1
                beside it, so that airiness just reads as loose — tighten it. */}
            <p
              ref={heroSubRef}
              style={{
                ...css("font-family:'Newsreader','EB Garamond',serif; font-size:clamp(19px,4.8vw,26px); line-height:1.85; color:var(--muted); margin:0;"),
                ...(isMobile ? { lineHeight: 1.5 } : null),
                ...(!isMobile && heroSubFit
                  ? { lineHeight: `${heroSubFit.lh}px`, marginTop: `${heroSubFit.mt}px` }
                  : null),
              }}
            >
              {t.hero_sub}
            </p>
          </div>
          )}
        </div>

        {/* Mobile: the globe joins the hero as the first screen — no separate
            slide, no heading. It runs the auto-tour and the card names each
            location. Reveal fades it up on load, a beat after the title. */}
        {isMobile && (
          <Reveal delay={140} style={{ marginTop: '18px' }}>
            <div style={{ position: 'relative', width: '100%', height: '40svh' }}>
              <Globe
                locations={locations}
                selected={selected}
                onSelect={setSelected}
                theme={theme}
                autoRotate={false}
                variant="full"
                maxDpr={3}
                style={css('position:absolute; inset:0; z-index:0;')}
              />
            </div>
            <div style={css('display:flex; align-items:center; justify-content:center; min-height:56px; margin-top:2px;')}>
              {tourLocation && (() => {
                const [country, city] = tourLocation.name.split(' | ')
                return (
                  <div key={tourLocation.id} style={{ ...css('display:flex; align-items:center; gap:16px;'), animation: 'vpnFadeIn .35s ease' }}>
                    <div style={css('text-align:left;')}>
                      <div style={css("font-family:'Newsreader','EB Garamond',serif; font-size:24px; font-weight:500; line-height:1.1; color:var(--ink);")}>{country}</div>
                      {city && <div style={css('font-size:13.5px; color:var(--muted2); margin-top:3px;')}>{city}</div>}
                    </div>
                    <Flag code={tourLocation.code} emoji={tourLocation.flag} height={38} />
                  </div>
                )
              })()}
            </div>
          </Reveal>
        )}
      </section>

      {/* ============================= GLOBE ============================
          The canvas is a backdrop, not a card: the projection centre sits at
          1.18x the canvas height, so only the upper arc of a very large sphere
          shows — a horizon behind the copy — and the mask fades its lower half
          into the page. Putting this in a bordered box makes the globe read as
          "shifted up", because the sphere's centre is below the box. */}
      {/* Mobile shows the globe up in the hero instead (a merged first screen),
          so this backdrop-horizon version is desktop only. */}
      {!isMobile && (
      <section style={css('position:relative; padding:0 0 clamp(48px,10vw,92px);')}>
        {(
          <div style={css('position:relative; width:100%; min-height:clamp(360px,72vw,600px);')}>
            <Globe
              locations={locations}
              selected={selected}
              onSelect={setSelected}
              theme={theme}
              style={css('position:absolute; inset:0; z-index:0;')}
            />
            <div style={css('position:relative; z-index:2; max-width:720px; margin:0 auto; padding:clamp(32px,9vw,48px) clamp(16px,4.5vw,28px) 0; text-align:center; pointer-events:none;')}>
              {/* The wrapper is pointer-events:none so clicks fall through to
                  the canvas (country picking); the texts themselves re-enable
                  pointer events, otherwise they can't be selected or copied. */}
              <div style={css('pointer-events:auto; font-size:13px; letter-spacing:.06em; text-transform:uppercase; color:var(--accent); font-weight:600; margin-bottom:16px;')}>{t.globe_kicker}</div>
              <h2 style={css("pointer-events:auto; font-family:'Newsreader','EB Garamond',serif; font-weight:500; font-size:clamp(28px,3.6vw,40px); line-height:1.2; letter-spacing:-.01em; margin:0 0 24px; color:var(--ink);")}>{t.globe_title}</h2>
              {t.globe_sub ? <p style={css('font-size:17px; line-height:1.55; color:var(--muted); max-width:480px; margin:0 auto 24px;')}>{t.globe_sub}</p> : null}
              {/* No border, no arrow, and no shadow: the shadow was there to
                  lift a pane of glass, and under a solid pill on a dark page it
                  is just a murky blot. Nothing else on this page casts one. */}
              <HoverLink
                href="#servers"
                onClick={(e) => scrollToSection(e, 'servers')}
                base={'pointer-events:auto; display:inline-flex; align-items:center; background:var(--card); color:var(--ink); font-size:15px; font-weight:500; padding:12px 26px; border-radius:999px; text-decoration:none; border:none; transition:background .18s ease;'}
                hover="background:var(--seg);"
              >
                {t.globe_cta}
              </HoverLink>
            </div>
          </div>
        )}
      </section>
      )}

      {/* ============================ PRICING =========================== */}
      {/* No own background: the root already paints var(--bg), and a second
          layer painting the same color can rasterize one RGB step off on
          GPU-composited browsers, showing a faint seam at the section edge. */}
      <section id="pricing">
        <div
          style={{
            ...css('max-width:1180px; margin:0 auto; padding:clamp(34px,8vw,96px) clamp(16px,4.5vw,28px); text-align:center;'),
            // Mobile only (desktop untouched): top-anchor + hug content so the
            // block doesn't float in a full-height slide (which left air above
            // it), but keep the section's natural top padding for breathing room.
            ...(isMobile ? { ...MOBILE_SLIDE, minHeight: 'auto', justifyContent: 'flex-start' } : null),
          }}
        >
          <Reveal>
            <div style={css('font-size:12.5px; font-weight:600; letter-spacing:.08em; text-transform:uppercase; color:var(--accent); margin-bottom:14px;')}>{t.price_kicker}</div>
            <h2 style={css("font-family:'Newsreader','EB Garamond',serif; font-weight:500; font-size:clamp(32px,4vw,48px); line-height:1.08; letter-spacing:-.02em; margin:0 auto 16px; max-width:640px; color:var(--ink);")}>{t.price_title}</h2>
            <p style={css('font-size:16px; line-height:1.6; color:var(--muted); margin:0 auto 40px; max-width:600px;')}>{t.price_sub}</p>
          </Reveal>

          {/* ONE card, with the term picked from a dropdown — plans differ
              only in term and price, so a row of near-identical cards spent its
              width repeating itself. The card carries everything the plan
              includes, on every screen size. */}
          {plans.length === 0 || !selectedPlan ? (
            <div style={css('padding:28px; color:var(--faint); font-size:14.5px;')}>{t.loading}</div>
          ) : (
            <>
            {/* Which unit prices are SHOWN in. The payment method itself is
                still chosen at checkout — a plan can be bought either way. */}
            {/* align-self:center so it keeps its content width — inside the
                mobile slide's flex column it would otherwise stretch full-width. */}
            <div style={css('display:inline-flex; align-self:center; padding:3px; margin-bottom:26px; border:none; border-radius:999px; background:var(--seg);')}>
              {['rub', 'stars'].map((unit) => (
                <button
                  key={unit}
                  onClick={() => setPriceUnit(unit)}
                  aria-label={unit === 'stars' ? 'Telegram Stars' : 'RUB'}
                  style={css(
                    'display:inline-flex; align-items:center; justify-content:center; border:none; cursor:pointer; ' +
                    'font-family:inherit; font-size:14px; font-weight:600; padding:7px 20px; border-radius:999px; ' +
                    'transition:background .18s ease, color .18s ease; ' +
                    (priceUnit === unit
                      ? 'background:var(--segActive); color:var(--ink);'
                      : 'background:transparent; color:var(--muted2);'),
                  )}
                >
                  {unit === 'stars' ? <Star size={15} /> : '₽'}
                </button>
              ))}
            </div>

            <Reveal style={{ maxWidth: '360px', width: '100%', margin: '0 auto' }}>
              <PlanCard>
                {/* One group so space-between sees three items (header,
                    features, button), not five, and puts the air between them. */}
                <div>
                <div style={css('display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:20px;')}>
                  <span style={css('font-size:13px; color:var(--muted2); font-weight:500;')}>{t.plan_term}</span>
                  <TermSelect
                    plans={plans}
                    value={selectedPlan.id}
                    onChange={setSelectedPlanId}
                    lang={lang}
                    t={t}
                    savingsPct={savingsPct}
                  />
                </div>

                <div style={css('display:flex; align-items:center; gap:7px; margin-bottom:6px;')}>
                  <span style={css("font-family:'Newsreader','EB Garamond',serif; font-size:46px; font-weight:500; letter-spacing:-.02em; line-height:1; color:var(--ink);")}>
                    {priceUnit === 'stars' ? fmt(selectedPlan.stars_price) : `${fmt(selectedPlan.rub_price)} ₽`}
                  </span>
                  {priceUnit === 'stars' && (
                    <span style={css('color:var(--ink); display:inline-flex;')}><Star size={28} /></span>
                  )}
                </div>

                {/* The per-month figure, and how it compares with the base plan
                    — the only comparison that means anything across different
                    terms. Cheaper is stated in the accent colour; dearer is
                    stated too, in plain grey: a short term that costs more per
                    month should say so rather than stay quiet about it. */}
                {/* FIXED height, and no wrap: the contents vary by term ("≈
                    ₽/mo" + a savings badge for long terms, "Standard plan" for
                    the base one), and on a narrow card a wrap to two lines is
                    what made the whole card grow and shrink as you switched
                    term. One reserved line, same height for every plan. */}
                {/* Bottom-aligned, not centred: the 26px reserved height (which
                    keeps the card from resizing between terms) otherwise leaves
                    ~6px of leading below this text, and space-evenly then reads
                    that as extra air above the feature list — the top gap looked
                    a letter taller than the gap down to the button. */}
                <div style={css('display:flex; align-items:flex-end; flex-wrap:nowrap; white-space:nowrap; gap:10px; font-size:13px; color:var(--faint); height:26px; line-height:1;')}>
                  {(() => {
                    const price = priceUnit === 'stars' ? selectedPlan.stars_price : selectedPlan.rub_price
                    if (!price || selectedPlan.days === 0) return null
                    const per = Math.round(price / (selectedPlan.days / 30))
                    const pct = savingsPct(selectedPlan)
                    return (
                      <>
                        {selectedPlan.days !== 30 && (
                          <span style={css('display:inline-flex; align-items:center; gap:4px;')}>
                            ≈ {fmt(per)}
                            {priceUnit === 'stars' ? <Star size={12} /> : ' ₽'} {t.plan_per_month}
                          </span>
                        )}
                        {pct !== null && (
                          <span
                            style={css(
                              'display:inline-flex; align-items:center; padding:2px 8px; border-radius:999px; font-weight:600; font-size:12px; ' +
                              (pct > 0
                                ? 'background:color-mix(in srgb, var(--accent) 16%, transparent); color:var(--accent);'
                                : 'background:var(--seg); color:var(--muted2);'),
                            )}
                          >
                            {pct > 0 ? t.plan_cheaper(pct) : t.plan_dearer(-pct)}
                          </span>
                        )}
                        {/* The base plan has no per-month figure (it IS the
                            month) and no saving (it's the yardstick), so this
                            fills the same line rather than leaving it empty. */}
                        {selectedPlan.is_base && (
                          <span style={css('color:var(--muted2);')}>{t.plan_is_base}</span>
                        )}
                      </>
                    )
                  })()}
                </div>
                </div>

                {/* flex:1 so the list eats the slack between header and button.
                    NO outer margin: space-evenly already puts an equal gap
                    above the first row and below the last, so the distance to
                    the header and to the button matches. An extra margin here
                    added to the top gap only, making the top look roomier than
                    the bottom. */}
                <div style={css('flex:1; display:flex; flex-direction:column; justify-content:space-evenly; font-size:14.5px; line-height:1.45; color:var(--muted);')}>
                  <div style={css('display:flex; gap:9px;')}>
                    <span style={css('color:var(--accent); flex-shrink:0;')}>✓</span>
                    {selectedPlan.conn_limit ? t.plan_conns(selectedPlan.conn_limit) : t.plan_conns_unlimited}
                  </div>
                  {t.included.map((line) => (
                    <div key={line} style={css('display:flex; gap:9px;')}>
                      <span style={css('color:var(--accent); flex-shrink:0;')}>✓</span>{line}
                    </div>
                  ))}
                </div>

                <HoverButton
                  onClick={() => startCheckout(selectedPlan)}
                  base={primaryBtn + 'width:100%;'}
                  hover="background:var(--btnHover);"
                >
                  {t.plan_cta}
                </HoverButton>
              </PlanCard>
            </Reveal>
            </>
          )}
          <div style={css('margin-top:26px; font-size:13px; color:var(--faint);')}>{t.price_cancel}</div>
        </div>
      </section>

      {/* =========================== LOCATIONS ========================= */}
      <section
        id="servers"
        style={{
          ...css('max-width:1180px; margin:0 auto; padding:clamp(34px,8vw,96px) clamp(16px,4.5vw,28px);'),
          // Mobile: hug the content instead of a forced 100svh centred slide —
          // the sparse locations panel left a huge void above and below it.
          ...(isMobile ? { ...MOBILE_SLIDE, minHeight: 'auto', justifyContent: 'flex-start' } : null),
        }}
      >
        {/* Heading centred; the location rows below keep their left alignment. */}
        <Reveal style={{ textAlign: 'center' }}>
          <div style={css('font-size:12.5px; font-weight:600; letter-spacing:.08em; text-transform:uppercase; color:var(--accent); margin-bottom:14px;')}>{t.srv_kicker}</div>
          <h2 style={css("font-family:'Newsreader','EB Garamond',serif; font-weight:500; font-size:clamp(30px,3.6vw,44px); line-height:1.1; letter-spacing:-.02em; margin:0 0 14px; color:var(--ink);")}>{t.srv_title}</h2>
          <p style={css('font-size:16px; line-height:1.6; color:var(--muted); margin:0 auto 28px; max-width:620px;')}>{t.srv_sub}</p>
        </Reveal>

        {/* Glass panel; the row separators are painted in the PAGE background,
            so they read as gaps cut through it rather than as drawn rules. */}
        <div style={css('border-radius:16px; overflow:hidden; background:var(--card);')}>
          {locations.length === 0 && (
            <div style={css('padding:28px; text-align:center; color:var(--faint); font-size:14.5px;')}>{t.loading}</div>
          )}
          {locations.map((s, i) => (
            <Reveal
              key={s.id}
              delay={i * 60}
              style={css(
                'display:flex; align-items:center; gap:14px; padding:16px 18px; font-size:15px; color:var(--ink);' +
                (i < locations.length - 1 ? ' border-bottom:1px solid var(--bg);' : ''),
              )}
            >
              <span style={css('font-weight:500;')}>{s.name}</span>
              <span style={css('margin-left:auto; font-size:13px; color:var(--faint);')}>
                {t[`reg_${regionOf(s.code)}_l`]}
              </span>
            </Reveal>
          ))}
        </div>
      </section>

      {/* ============================= FOOTER =========================== */}
      <footer style={css('border-top:1px solid var(--hair);')}>
        <div style={css('max-width:1180px; margin:0 auto; padding:clamp(30px,6vw,72px) clamp(16px,4.5vw,28px) 32px; display:flex; flex-wrap:wrap; gap:clamp(24px,8vw,48px); justify-content:space-between;')}>
          <div style={css('max-width:320px;')}>
            <div style={css('display:flex; align-items:center; margin-bottom:16px;')}>
              <span style={css('font-weight:600; font-size:18px; letter-spacing:-.015em;')}>AegisVPN</span>
            </div>
            <p style={css('font-size:14.5px; line-height:1.55; color:var(--muted2); margin:0 0 18px;')}>{t.foot_brand}</p>
            <HoverLink href={BOT_URL} target="_blank" rel="noopener" base={primaryBtn + 'font-size:14px; padding:10px 18px;'} hover="background:var(--btnHover);">
              {TG_ICON} {t.foot_tg}
            </HoverLink>
          </div>
          <div style={css('display:flex; gap:clamp(32px,8vw,80px); flex-wrap:wrap;')}>
            <div>
              <div style={css('font-size:13px; font-weight:600; color:var(--ink); margin-bottom:14px;')}>{t.foot_product}</div>
              <div style={css('display:flex; flex-direction:column; gap:11px; font-size:14.5px; color:var(--muted);')}>
                <HoverLink href="#pricing" onClick={(e) => scrollToSection(e, 'pricing')} base={navLink} hover="color:var(--accent);">{t.nav_pricing}</HoverLink>
                <HoverLink href="#servers" onClick={(e) => scrollToSection(e, 'servers')} base={navLink} hover="color:var(--accent);">{t.nav_servers}</HoverLink>
              </div>
            </div>
            <div>
              <div style={css('font-size:13px; font-weight:600; color:var(--ink); margin-bottom:14px;')}>{t.foot_company}</div>
              <div style={css('display:flex; flex-direction:column; gap:11px; font-size:14.5px; color:var(--muted);')}>
                <HoverLink href={BOT_URL} base={navLink} hover="color:var(--accent);">{t.foot_support}</HoverLink>
              </div>
            </div>
          </div>
        </div>
        <div style={css('max-width:1180px; margin:0 auto; padding:0 clamp(16px,4.5vw,28px) 40px; font-size:12.5px; color:var(--faint);')}>
          © {new Date().getFullYear()} AegisVPN. {t.foot_rights}
        </div>
      </footer>

      {/* ============================ CHECKOUT ==========================
          onClose is null while the payment page is being opened: that tells the
          Modal it is not dismissible, and it ignores backdrop, Escape and ×. */}
      {checkoutOpen && selectedPlan && user && (
        <Modal onClose={payBusy ? null : () => setCheckoutOpen(false)} maxWidth={440}>
          <h3 style={css("font-family:'Newsreader','EB Garamond',serif; font-weight:500; font-size:26px; letter-spacing:-.01em; margin:4px 0 18px; color:var(--ink);")}>{t.pay_title}</h3>

          <div style={css('display:flex; align-items:baseline; justify-content:space-between; gap:12px; padding:14px 0; border-top:1px solid var(--hair); border-bottom:1px solid var(--hair); margin-bottom:20px;')}>
            <span style={css('font-size:14.5px; color:var(--muted);')}>
              {termLabel(selectedPlan.days, t, lang)}
            </span>
            <span style={css("font-family:'Newsreader','EB Garamond',serif; font-size:28px; font-weight:500; color:var(--ink);")}>
              {fmt(selectedPlan.rub_price)} ₽
            </span>
          </div>

          {/* The consent gate the bot also enforces. Shown only to users who
              have not accepted yet — anyone who accepted in the bot is not
              asked twice. */}
          {!user.terms_accepted && (
            <label style={css('display:flex; gap:10px; align-items:flex-start; margin-bottom:18px; font-size:13px; line-height:1.5; color:var(--muted); cursor:pointer;')}>
              <Checkbox checked={consent} onChange={setConsent} />
              <span>
                {t.pay_consent_pre}{' '}
                <button type="button" onClick={() => openLegal('tos')} style={css('background:none; border:none; padding:0; font:inherit; color:var(--accent); cursor:pointer; text-decoration:underline;')}>{t.legal_tos}</button>
                {' '}{t.pay_consent_and}{' '}
                <button type="button" onClick={() => openLegal('privacy')} style={css('background:none; border:none; padding:0; font:inherit; color:var(--accent); cursor:pointer; text-decoration:underline;')}>{t.legal_privacy}</button>
              </span>
            </label>
          )}

          {(() => {
            const blocked = !user.terms_accepted && !consent
            const dim = blocked ? 'opacity:.45; pointer-events:none;' : ''
            return (
              <div style={css('display:flex; flex-direction:column; gap:10px;')}>
                {selectedPlan.rub_price ? (
                  <button
                    onClick={() => pay('sbp')}
                    disabled={blocked || !!payBusy}
                    style={css(primaryBtn + 'width:100%; padding:14px; cursor:pointer;' + dim)}
                  >
                    {payBusy === 'sbp' ? t.pay_redirecting : t.pay_sbp}
                  </button>
                ) : null}
                {selectedPlan.stars_price ? (
                  <button
                    onClick={() => pay('stars')}
                    disabled={blocked || !!payBusy}
                    style={css(
                      'display:inline-flex; align-items:center; justify-content:center; gap:8px; width:100%; padding:14px; ' +
                      'background:var(--seg); color:var(--ink); border:1px solid var(--hair2); border-radius:999px; ' +
                      'font-size:15px; font-weight:500; font-family:inherit; cursor:pointer;' + dim,
                    )}
                  >
                    {payBusy === 'stars' ? (
                      t.pay_redirecting
                    ) : (
                      <>
                        {t.pay_stars} — {fmt(selectedPlan.stars_price)}
                        <Star size={15} />
                      </>
                    )}
                  </button>
                ) : null}
              </div>
            )
          })()}

          {payError && (
            <p style={css('margin:14px 0 0; font-size:13.5px; color:var(--accent); text-align:center;')}>{payError}</p>
          )}
          <p style={css('margin:16px 0 0; font-size:12.5px; line-height:1.5; color:var(--faint); text-align:center;')}>{t.pay_stars_note}</p>
        </Modal>
      )}

      {/* Waiting for the provider → bot → DB confirmation after coming back. */}
      {awaitingPayment && (
        <Modal onClose={() => setAwaitingPayment(false)} maxWidth={380}>
          <div style={css('text-align:center; padding:8px 0;')}>
            <div style={{ ...css('width:28px; height:28px; margin:0 auto 18px; border:2px solid var(--hair2); border-top-color:var(--accent); border-radius:50%;'), animation: 'vpnSpin .8s linear infinite' }} />
            <h3 style={css("font-family:'Newsreader','EB Garamond',serif; font-weight:500; font-size:22px; margin:0 0 8px; color:var(--ink);")}>{t.pay_wait_title}</h3>
            <p style={css('font-size:14px; line-height:1.55; color:var(--muted2); margin:0;')}>{t.pay_wait_sub}</p>
          </div>
        </Modal>
      )}

      {/* Terms of Service / Privacy Policy, from the same files the bot serves. */}
      {legalDoc && (
        <Modal onClose={() => setLegalDoc(null)} maxWidth={720}>
          <h3 style={css("font-family:'Newsreader','EB Garamond',serif; font-weight:500; font-size:24px; margin:4px 0 16px; color:var(--ink);")}>{legalDoc.title}</h3>
          <div style={css('max-height:60vh; overflow-y:auto; font-size:14px; line-height:1.65; color:var(--muted); white-space:pre-wrap;')}>
            {legalDoc.text === null ? t.loading : legalDoc.text || t.legal_unavailable}
          </div>
        </Modal>
      )}

      {/* ============================== AUTH ============================ */}
      {authOpen && (
        <Modal onClose={() => { setAuthOpen(false); setPendingCheckout(false) }}>
          <h3 style={css("font-family:'Newsreader','EB Garamond',serif; font-weight:500; font-size:26px; letter-spacing:-.01em; margin:4px 0 10px; color:var(--ink);")}>{t.auth_title}</h3>
          <p style={css('font-size:14.5px; line-height:1.55; color:var(--muted2); margin:0 0 24px;')}>
            {pendingCheckout ? t.auth_sub_pay : t.auth_sub}
          </p>

          <TelegramLogin botName={BOT_NAME} onAuth={onTelegramAuth} lang={lang} />

          {authError && (
            <p style={css('margin:16px 0 0; font-size:13.5px; color:var(--accent); text-align:center;')}>{t.auth_failed}</p>
          )}
          <p style={css('margin:22px 0 0; font-size:12.5px; line-height:1.5; color:var(--faint); text-align:center;')}>{t.auth_terms}</p>
        </Modal>
      )}

      {/* ============================ ACCOUNT ========================== */}
      {accountOpen && user && (
        <Modal onClose={() => setAccountOpen(false)} maxWidth={460}>
          <div style={css('display:flex; align-items:center; gap:14px; margin-bottom:22px;')}>
            <Avatar user={user} size={48} fallbackLabel={t.acc_guest_label} />
            <div style={css('min-width:0;')}>
              <div style={css('font-size:13px; color:var(--muted2); margin-bottom:2px;')}>{t.acc_greeting}</div>
              <h3 style={css("font-family:'Newsreader','EB Garamond',serif; font-weight:500; font-size:24px; letter-spacing:-.01em; margin:0; color:var(--ink); overflow:hidden; text-overflow:ellipsis;")}>
                {user.display_name || t.acc_guest_label}
              </h3>
              {user.tg && (
                <div style={css('font-size:13px; color:var(--faint); margin-top:2px;')}>{user.tg}</div>
              )}
            </div>
          </div>

          <div style={css('display:flex; justify-content:space-between; padding:14px 0; border-top:1px solid var(--hair); font-size:14.5px;')}>
            <span style={css('color:var(--muted);')}>{t.acc_status}</span>
            <span style={css('font-weight:500; color:' + (user.subscription?.is_active ? 'var(--accent)' : 'var(--muted2)') + ';')}>
              {user.subscription?.is_active ? t.acc_active : t.acc_inactive}
            </span>
          </div>
          {user.subscription?.expires_at && (
            <div style={css('display:flex; justify-content:space-between; padding:14px 0; border-top:1px solid var(--hair); font-size:14.5px;')}>
              {/* A lifetime subscription carries a sentinel date (2099-12-31);
                  printing it as "valid until 31.12.2099" reads like a bug. */}
              <span style={css('color:var(--muted);')}>
                {user.subscription.is_lifetime ? t.acc_term : t.acc_expires}
              </span>
              <span style={css('font-weight:500;')}>
                {user.subscription.is_lifetime
                  ? t.acc_lifetime
                  : new Date(user.subscription.expires_at).toLocaleDateString(lang === 'en' ? 'en-GB' : 'ru-RU')}
              </span>
            </div>
          )}

          {user.subscription?.sub_url && (
            <>
              <div style={css('margin:22px 0 8px; font-size:13px; font-weight:600; color:var(--ink);')}>{t.acc_sub_url}</div>
              <div style={css('display:flex; gap:8px;')}>
                <input
                  readOnly
                  value={user.subscription.sub_url}
                  style={css('flex:1; min-width:0; background:var(--codeBg); border:1px solid var(--hair2); border-radius:10px; padding:12px; font-size:12.5px; color:var(--muted); font-family:ui-monospace,monospace;')}
                />
                <button onClick={() => copySub(user.subscription.sub_url)} style={css('flex-shrink:0; background:var(--ink); color:var(--bg); border:none; border-radius:10px; padding:0 16px; font-size:13.5px; font-weight:500; cursor:pointer; font-family:inherit;')}>
                  {copied ? t.acc_copied : t.acc_copy}
                </button>
              </div>
            </>
          )}

          <HoverLink href={BOT_URL} target="_blank" rel="noopener" base={primaryBtn + 'width:100%; margin:24px 0 10px; padding:14px;'} hover="background:var(--btnHover);">
            {t.acc_manage_tg}
          </HoverLink>
          <button onClick={doLogout} style={css('width:100%; background:transparent; border:1px solid var(--hair2); color:var(--muted); font-size:14px; padding:12px; border-radius:12px; cursor:pointer; font-family:inherit;')}>
            {t.acc_logout}
          </button>
        </Modal>
      )}
    </div>
  )
}
