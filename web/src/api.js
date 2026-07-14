// Everything the site knows about plans, locations and the current user comes
// from here. Nothing is hardcoded: the API reads the same tables the bot writes,
// so a price change in the admin panel shows up on the site with no deploy.

const json = async (res) => {
  if (res.status === 401) return null
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

const opts = { credentials: 'same-origin', headers: { 'Content-Type': 'application/json' } }

export const getLocations = () => fetch('/api/locations', opts).then(json)
export const getPlans = () => fetch('/api/plans', opts).then(json)

/** Current session, or null when signed out. */
export const getMe = () => fetch('/api/me', opts).then(json)

/**
 * Hand Telegram's signed callback payload to the backend, which verifies the
 * HMAC against the bot token and sets a session cookie. The signature cannot be
 * checked client-side — the bot token must never reach the browser.
 */
export const loginWithTelegram = (payload) =>
  fetch('/api/auth/telegram', { ...opts, method: 'POST', body: JSON.stringify(payload) }).then(json)

export const logout = () => fetch('/api/auth/logout', { ...opts, method: 'POST' }).then(json)

/** Record acceptance of the current legal documents (same flags the bot sets). */
export const acceptTerms = () => fetch('/api/terms/accept', { ...opts, method: 'POST' }).then(json)

export const getLegal = (doc, lang) =>
  fetch(`/api/legal/${doc}?lang=${lang}`, opts).then(json)

/**
 * Start a payment. Returns { url } to send the browser to: Platega's payment
 * page for СБП, or a pre-filled Telegram invoice for Stars (Stars can only be
 * charged inside Telegram). The subscription is granted by the bot's existing
 * confirmation paths, not by the site.
 *
 * Errors carry a code the UI can act on rather than a bare status:
 * auth_required, terms_required, plan_unavailable, provider_error.
 */
export const checkout = async (planId, method) => {
  const res = await fetch('/api/checkout', {
    ...opts,
    method: 'POST',
    body: JSON.stringify({ plan_id: planId, method }),
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) throw Object.assign(new Error(body.error || 'error'), { code: body.error })
  return body
}
