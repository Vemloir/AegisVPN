import { useRef, useState } from 'react'
import { css } from './css.js'

export default function SubscriptionImport({ url, t }) {
  const field = useRef(null)
  const [feedback, setFeedback] = useState(null)

  async function copyLink() {
    try {
      await navigator.clipboard.writeText(url)
      setFeedback('acc_copied')
    } catch {
      field.current?.focus()
      field.current?.select()
      setFeedback('acc_copy_manual')
    }
  }

  return (
    <section>
      <h4>{t.acc_connect}</h4>
      <div style={css('display:flex; gap:8px;')}>
        <input ref={field} aria-label={t.acc_sub_url} readOnly value={url}
          style={css('flex:1; min-width:0; background:var(--codeBg); border:1px solid var(--hair2); border-radius:10px; padding:12px; font-size:12.5px; color:var(--muted); font-family:ui-monospace,monospace;')} />
        <button onClick={copyLink} style={css('flex-shrink:0; background:var(--ink); color:var(--bg); border:none; border-radius:10px; padding:0 16px; font-size:13.5px; font-weight:500; cursor:pointer; font-family:inherit;')}>
          {t.acc_copy}
        </button>
      </div>
      <span role="status">{feedback && t[feedback]}</span>
    </section>
  )
}
