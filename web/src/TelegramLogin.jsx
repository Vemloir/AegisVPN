import { useEffect, useRef } from 'react'

/**
 * Telegram's official login widget.
 *
 * It injects an iframe from telegram.org and calls back with
 * { id, first_name, username, photo_url, auth_date, hash }. The hash is an
 * HMAC-SHA256 over the other fields keyed by SHA256(bot_token) — only the
 * backend can verify it, so this component does no validation of its own and
 * simply forwards the payload.
 *
 * Requires the site's domain to be registered for the bot via BotFather
 * (/setdomain), and the page to be served over HTTPS.
 */
export default function TelegramLogin({ botName, onAuth, lang = 'ru', iframeTitle = 'Telegram login' }) {
  const holder = useRef(null)
  const cb = useRef(onAuth)
  cb.current = onAuth

  useEffect(() => {
    const el = holder.current
    if (!el) return

    // The widget only speaks to a global function, named by data-onauth.
    const fnName = `onTelegramAuth_${Math.random().toString(36).slice(2)}`
    window[fnName] = (user) => cb.current?.(user)

    const s = document.createElement('script')
    s.src = 'https://telegram.org/js/telegram-widget.js?22'
    s.async = true
    s.setAttribute('data-telegram-login', botName)
    s.setAttribute('data-size', 'large')
    s.setAttribute('data-radius', '12')
    s.setAttribute('data-userpic', 'false')
    s.setAttribute('data-request-access', 'write')
    s.setAttribute('data-lang', lang)
    s.setAttribute('data-onauth', `${fnName}(user)`)
    el.appendChild(s)

    const labelIframe = () => {
      const iframe = el.querySelector('iframe')
      if (iframe) iframe.title = iframeTitle
    }
    const observer = new MutationObserver(labelIframe)
    observer.observe(el, { childList: true, subtree: true })
    labelIframe()

    return () => {
      observer.disconnect()
      delete window[fnName]
      el.replaceChildren()
    }
  }, [botName, lang, iframeTitle])

  return <div ref={holder} aria-label={iframeTitle} style={{ display: 'flex', justifyContent: 'center' }} />
}
