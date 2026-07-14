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
  '--codeBg:#EDE9DF; --rowHover:#EDE9DF; --logoBg:#1A1A1A; --logoText:#ffffff;'

// Every background/border neutral below had blue nudged 1-2 points above red
// and green (a common "default dark UI" recipe) — individually invisible, but
// summed across every card/border/panel on the page it read as a cool/blue
// cast. Neutralized to true grey (R=G=B) at the same lightness; ink/muted/
// faint were already warm-leaning (R>G>B) and are untouched.
const DARK_VARS =
  '--bg:#161616; --ink:#ECEAE6; --muted:#A8A6A2; --muted2:#827F7A; --faint:#6E6B66;' +
  '--hair:#2E2E2E; --hair2:#262626; --hair3:#2E2E2E; --accent:#C2613D; --accentSoft:#CC785C;' +
  '--btn:#C2613D; --btnHover:#D07A55; --btnText:#ffffff; --card:#1F1F1F; --seg:#252525; --segActive:#373737;' +
  '--codeBg:#262626; --rowHover:#202020; --logoBg:#E6A085; --logoText:#1A140F;'

const fmt = (n) => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ' ')

/* ---------------------------------------------------------------- primitives */

function HoverLink({ base, hover, children, ...rest }) {
  return <a {...useHoverStyle(base, hover)} {...rest}>{children}</a>
}

function HoverButton({ base, hover, children, ...rest }) {
  return <button {...useHoverStyle(base, hover)} {...rest}>{children}</button>
}

function Modal({ onClose, children, maxWidth = 420 }) {
  useEffect(() => {
    const esc = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', esc)
    return () => window.removeEventListener('keydown', esc)
  }, [onClose])

  return (
    <div
      onClick={onClose}
      style={css('position:fixed; inset:0; z-index:120; background:rgba(18,16,12,.46); backdrop-filter:blur(3px); -webkit-backdrop-filter:blur(3px); display:flex; align-items:center; justify-content:center; padding:clamp(12px,4vw,24px);')}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{ ...css('position:relative; width:100%; background:var(--card); border:1px solid var(--hair2); border-radius:20px; padding:32px 28px;'), maxWidth }}
      >
        {children}
        <button
          onClick={onClose}
          aria-label="Close"
          style={css('position:absolute; top:18px; right:18px; width:32px; height:32px; border:none; background:transparent; color:var(--muted2); font-size:22px; line-height:1; cursor:pointer; border-radius:8px;')}
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

// Fully custom dropdown for the plan term. The native <select>'s closed
// state can be restyled (appearance:none), but the OPEN menu is drawn by the
// browser and clashes with the site. The list is a handful of terms, so a
// hand-rolled listbox with outside-click / Esc / arrow-key handling beats
// pulling in a component library.
function TermSelect({ plans, value, onChange, lang }) {
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(-1) // keyboard/hover-highlighted index
  const rootRef = useRef(null)

  const idx = plans.findIndex((p) => p.id === value)
  const label = (p) => `${p.days} ${lang === 'en' ? 'days' : 'дн.'}`
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
              {label(p)}
              <span style={{ visibility: p.id === value ? 'visible' : 'hidden', color: 'var(--accent)' }}>✓</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

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

  const onTelegramAuth = useCallback(async (payload) => {
    setAuthError(false)
    try {
      const me = await api.loginWithTelegram(payload)
      if (!me) throw new Error('rejected')
      setUser(me)
      setAuthOpen(false)
      setAccountOpen(true)
    } catch {
      setAuthError(true)
    }
  }, [])

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
  const selectedPlan = plans.find((p) => p.id === selectedPlanId) || plans[0]

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

  const langBtn = (on) =>
    'border:none;padding:6px 13px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit;' +
    (on ? 'background:var(--ink);color:var(--bg);' : 'background:transparent;color:var(--muted2);')

  const navLink = 'color:inherit; text-decoration:none;'
  const primaryBtn = 'display:inline-flex; align-items:center; justify-content:center; gap:8px; background:var(--btn); color:var(--btnText); border:none; font-size:15px; font-weight:500; padding:13px 22px; border-radius:999px; cursor:pointer; font-family:inherit; text-decoration:none;'

  return (
    <div style={rootStyle}>
      {/* ============================ HEADER ============================ */}
      {/* Solid var(--bg), NOT a translucent film + backdrop blur: any
          semi-transparent layer is blended by the GPU compositor and
          re-quantized ±1 (eyedropper-visible specks against the page). The
          background transition matches the root's exactly (same property,
          duration, curve), and two CSS transitions with identical parameters
          interpolate in lockstep — no seam during the theme crossfade. */}
      <header style={css('position:sticky; top:0; z-index:60; background:var(--bg); border-bottom:1px solid var(--hair); transition:background .4s ease, border-color .4s ease;')}>
        <div style={css('max-width:1180px; margin:0 auto; padding:15px clamp(16px,4vw,28px); display:flex; align-items:center; justify-content:space-between; gap:24px;')}>
          <a href="#top" onClick={(e) => scrollToSection(e, 'top')} style={css('display:flex; align-items:center; text-decoration:none; color:inherit;')}>
            <span style={css('font-weight:600; font-size:18px; letter-spacing:-.015em;')}>AegisVPN</span>
          </a>

          {!isMobile && (
            <nav style={css('display:flex; align-items:center; gap:22px; font-size:14.5px; color:var(--muted); flex-shrink:0;')}>
              <HoverLink href="#features" onClick={(e) => scrollToSection(e, 'features')} base={navLink} hover="color:var(--accent);">{t.nav_features}</HoverLink>
              <HoverLink href="#servers" onClick={(e) => scrollToSection(e, 'servers')} base={navLink} hover="color:var(--accent);">{t.nav_servers}</HoverLink>
              <HoverLink href="#pricing" onClick={(e) => scrollToSection(e, 'pricing')} base={navLink} hover="color:var(--accent);">{t.nav_pricing}</HoverLink>

              <button
                onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
                aria-label="Theme"
                style={css('display:flex; align-items:center; justify-content:center; width:34px; height:34px; border:1px solid var(--hair2); border-radius:999px; background:transparent; cursor:pointer; color:var(--ink); padding:0;')}
              >
                {theme === 'dark' ? '☾' : '☀'}
              </button>

              <div style={css('display:flex; border:1px solid var(--hair2); border-radius:999px; overflow:hidden;')}>
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
            {[['features', t.nav_features], ['servers', t.nav_servers], ['pricing', t.nav_pricing]].map(([id, label]) => (
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
      <section id="top" style={css('max-width:1180px; margin:0 auto; padding:clamp(48px,10vw,84px) clamp(16px,4.5vw,28px) clamp(32px,6vw,48px);')}>
        <div style={{ display: 'grid', gap: isMobile ? '22px' : '48px', alignItems: 'start', gridTemplateColumns: isMobile ? '1fr' : '1.45fr 1fr' }}>
          <div style={css('min-width:0;')}>
            <div style={css('display:inline-flex; align-items:center; gap:8px; font-size:13px; letter-spacing:.04em; text-transform:uppercase; color:var(--accent); font-weight:600; margin-bottom:24px;')}>
              <span style={css('width:6px; height:6px; border-radius:50%; background:var(--accentSoft); display:inline-block;')} />
              {t.hero_eyebrow}
            </div>
            <h1 ref={heroH1Ref} style={css("font-family:'Newsreader','EB Garamond',serif; font-weight:500; font-size:clamp(40px,5.2vw,66px); line-height:1.03; letter-spacing:-.02em; margin:0 0 28px; color:var(--ink);")}>
              {t.hero_l1}<br />
              <span style={css('font-style:italic; color:var(--accent);')}>{t.hero_l2}</span>
            </h1>
            <div style={css('display:flex; align-items:center; gap:20px; flex-wrap:wrap;')}>
              <HoverLink
                href="#pricing"
                onClick={(e) => scrollToSection(e, 'pricing')}
                base={'display:inline-flex; align-items:center; gap:9px; background:var(--btn); color:var(--btnText); font-size:16px; font-weight:500; padding:14px 26px; border-radius:999px; text-decoration:none;'}
                hover="background:var(--btnHover);"
              >
                {t.hero_pay_site}
              </HoverLink>
              <HoverLink
                href={BOT_URL}
                target="_blank"
                rel="noopener"
                base={'display:inline-flex; align-items:center; gap:7px; font-size:16px; font-weight:500; color:var(--ink); text-decoration:none; border-bottom:1px solid transparent; padding-bottom:2px;'}
                hover="border-bottom-color:var(--ink);"
              >
                {TG_ICON} {t.cta_try}
              </HoverLink>
            </div>
          </div>
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
            <p
              ref={heroSubRef}
              style={{
                ...css("font-family:'Newsreader','EB Garamond',serif; font-size:clamp(20px,2.4vw,26px); line-height:1.85; color:var(--muted); margin:0;"),
                ...(!isMobile && heroSubFit
                  ? { lineHeight: `${heroSubFit.lh}px`, marginTop: `${heroSubFit.mt}px` }
                  : null),
              }}
            >
              {t.hero_sub}
            </p>
          </div>
        </div>
      </section>

      {/* ============================= GLOBE ============================
          The canvas is a backdrop, not a card: the projection centre sits at
          1.18x the canvas height, so only the upper arc of a very large sphere
          shows — a horizon behind the copy — and the mask fades its lower half
          into the page. Putting this in a bordered box makes the globe read as
          "shifted up", because the sphere's centre is below the box. */}
      <section style={css('position:relative; padding:0 0 clamp(48px,10vw,92px);')}>
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
            <HoverLink
              href="#servers"
              onClick={(e) => scrollToSection(e, 'servers')}
              base={'pointer-events:auto; display:inline-flex; align-items:center; gap:7px; background:var(--card); color:var(--ink); border:1px solid var(--hair); font-size:15px; font-weight:500; padding:11px 22px; border-radius:999px; text-decoration:none;'}
              hover="background:var(--bg);"
            >
              {t.globe_cta} <span style={css('color:var(--accent);')}>→</span>
            </HoverLink>
          </div>
        </div>
      </section>

      {/* =========================== FEATURES ========================== */}
      <section id="features" style={css('max-width:1180px; margin:0 auto; padding:clamp(48px,9vw,84px) clamp(16px,4.5vw,28px) clamp(56px,10vw,96px);')}>
        <div style={css('font-size:12.5px; font-weight:600; letter-spacing:.08em; text-transform:uppercase; color:var(--accent); margin-bottom:14px;')}>{t.feat_kicker}</div>
        <h2 style={css("font-family:'Newsreader','EB Garamond',serif; font-weight:500; font-size:clamp(30px,3.6vw,44px); line-height:1.1; letter-spacing:-.02em; margin:0 0 44px; color:var(--ink);")}>{t.feat_title}</h2>
        <div style={{ display: 'grid', gap: '34px 40px', gridTemplateColumns: isMobile ? '1fr' : 'repeat(3, 1fr)' }}>
          {t.features.map((f) => (
            <div key={f.num}>
              <div style={css('font-size:12.5px; font-weight:600; color:var(--accent); margin-bottom:12px;')}>{f.num}</div>
              <h3 style={css("font-family:'Newsreader','EB Garamond',serif; font-weight:500; font-size:21px; letter-spacing:-.01em; margin:0 0 9px; color:var(--ink);")}>{f.title}</h3>
              <p style={css('font-size:14.8px; line-height:1.6; color:var(--muted); margin:0;')}>{f.desc}</p>
            </div>
          ))}
        </div>
      </section>

      {/* =========================== LOCATIONS ========================= */}
      <section id="servers" style={css('max-width:1180px; margin:0 auto; padding:clamp(56px,10vw,96px) clamp(16px,4.5vw,28px);')}>
        <div style={css('font-size:12.5px; font-weight:600; letter-spacing:.08em; text-transform:uppercase; color:var(--accent); margin-bottom:14px;')}>{t.srv_kicker}</div>
        <h2 style={css("font-family:'Newsreader','EB Garamond',serif; font-weight:500; font-size:clamp(30px,3.6vw,44px); line-height:1.1; letter-spacing:-.02em; margin:0 0 14px; color:var(--ink);")}>{t.srv_title}</h2>
        <p style={css('font-size:16px; line-height:1.6; color:var(--muted); margin:0 0 28px; max-width:620px;')}>{t.srv_sub}</p>

        <div style={css('background:var(--card); border:1px solid var(--hair2); border-radius:16px; overflow:hidden;')}>
          {locations.length === 0 && (
            <div style={css('padding:28px; text-align:center; color:var(--faint); font-size:14.5px;')}>{t.loading}</div>
          )}
          {locations.map((s) => (
            <div
              key={s.id}
              style={css('display:flex; align-items:center; gap:14px; padding:16px 18px; border-bottom:1px solid var(--hair); font-size:15px; color:var(--ink);')}
            >
              <span style={css('font-weight:500;')}>{s.name}</span>
              <span style={css('margin-left:auto; font-size:13px; color:var(--faint);')}>
                {t[`reg_${regionOf(s.code)}_l`]}
              </span>
            </div>
          ))}
        </div>
      </section>

      {/* ============================ PRICING =========================== */}
      {/* No own background: the root already paints var(--bg), and a second
          layer painting the same color can rasterize one RGB step off on
          GPU-composited browsers, showing a faint seam at the section edge. */}
      <section id="pricing">
        <div style={css('max-width:1180px; margin:0 auto; padding:clamp(56px,10vw,96px) clamp(16px,4.5vw,28px); text-align:center;')}>
          <div style={css('font-size:12.5px; font-weight:600; letter-spacing:.08em; text-transform:uppercase; color:var(--accent); margin-bottom:14px;')}>{t.price_kicker}</div>
          <h2 style={css("font-family:'Newsreader','EB Garamond',serif; font-weight:500; font-size:clamp(32px,4vw,48px); line-height:1.08; letter-spacing:-.02em; margin:0 auto 16px; max-width:640px; color:var(--ink);")}>{t.price_title}</h2>
          <p style={css('font-size:16px; line-height:1.6; color:var(--muted); margin:0 auto 40px; max-width:600px;')}>{t.price_sub}</p>

          {/* Plans differ only in term and price, so it's one card with a term
              dropdown rather than a row of near-identical cards. */}
          <div style={{ maxWidth: 400, margin: '0 auto', textAlign: 'left' }}>
            {plans.length === 0 && (
              <div style={css('padding:28px; color:var(--faint); font-size:14.5px;')}>{t.loading}</div>
            )}
            {selectedPlan && (
              <div style={css('display:flex; flex-direction:column; padding:28px; border:1px solid var(--hair2); border-radius:20px; background:var(--card);')}>
                <div style={css('display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:20px;')}>
                  <span style={css('font-size:13px; color:var(--muted2); font-weight:500;')}>{t.plan_term}</span>
                  <TermSelect
                    plans={plans}
                    value={selectedPlan.id}
                    onChange={setSelectedPlanId}
                    lang={lang}
                  />
                </div>
                <div style={css('display:flex; align-items:baseline; gap:8px; margin-bottom:20px;')}>
                  <span style={css("font-family:'Newsreader','EB Garamond',serif; font-size:46px; font-weight:500; letter-spacing:-.02em; line-height:1; color:var(--ink);")}>{fmt(selectedPlan.rub_price)} ₽</span>
                  <span style={css('font-size:14px; color:var(--muted2);')}>/ {selectedPlan.days} {lang === 'en' ? 'days' : 'дн.'}</span>
                </div>
                <div style={css('display:flex; flex-direction:column; gap:10px; margin-bottom:24px; font-size:14.5px; color:var(--muted);')}>
                  {selectedPlan.devices ? (
                    <div style={css('display:flex; gap:9px;')}>
                      <span style={css('color:var(--accent);')}>✓</span>
                      {lang === 'en' ? `Up to ${selectedPlan.devices} devices` : `До ${selectedPlan.devices} устройств`}
                    </div>
                  ) : null}
                  {t.included.map((line) => (
                    <div key={line} style={css('display:flex; gap:9px;')}>
                      <span style={css('color:var(--accent);')}>✓</span>{line}
                    </div>
                  ))}
                </div>
                <HoverLink
                  href={BOT_URL}
                  target="_blank"
                  rel="noopener"
                  base={primaryBtn + 'margin-top:auto; width:100%;'}
                  hover="background:var(--btnHover);"
                >
                  {t.plan_cta}
                </HoverLink>
              </div>
            )}
          </div>
          <div style={css('margin-top:22px; font-size:13px; color:var(--faint);')}>{t.price_cancel}</div>
        </div>
      </section>

      {/* ============================= FOOTER =========================== */}
      <footer style={css('border-top:1px solid var(--hair);')}>
        <div style={css('max-width:1180px; margin:0 auto; padding:clamp(44px,7vw,72px) clamp(16px,4.5vw,28px) 40px; display:flex; flex-wrap:wrap; gap:48px; justify-content:space-between;')}>
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
                <HoverLink href="#features" onClick={(e) => scrollToSection(e, 'features')} base={navLink} hover="color:var(--accent);">{t.nav_features}</HoverLink>
                <HoverLink href="#servers" onClick={(e) => scrollToSection(e, 'servers')} base={navLink} hover="color:var(--accent);">{t.nav_servers}</HoverLink>
                <HoverLink href="#pricing" onClick={(e) => scrollToSection(e, 'pricing')} base={navLink} hover="color:var(--accent);">{t.nav_pricing}</HoverLink>
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
          © {new Date().getFullYear()} AegisVPN. {t.foot_rights} · build {__BUILD_STAMP__}
        </div>
      </footer>

      {/* ============================== AUTH ============================ */}
      {authOpen && (
        <Modal onClose={() => setAuthOpen(false)}>
          <h3 style={css("font-family:'Newsreader','EB Garamond',serif; font-weight:500; font-size:26px; letter-spacing:-.01em; margin:4px 0 10px; color:var(--ink);")}>{t.auth_title}</h3>
          <p style={css('font-size:14.5px; line-height:1.55; color:var(--muted2); margin:0 0 24px;')}>{t.auth_sub}</p>

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
