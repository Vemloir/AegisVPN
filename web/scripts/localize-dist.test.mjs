import assert from 'node:assert/strict'
import test from 'node:test'

import { localizeHtml } from './localize-dist.mjs'

const fixture = `<!doctype html>
<html lang="ru"><head>
<title>Old</title>
<meta name="description" content="Old description">
</head><body><div id="root"></div></body></html>`

test('localizer emits complete Russian metadata', () => {
  const html = localizeHtml(fixture, 'ru')
  assert.match(html, /<html lang="ru">/)
  assert.match(html, /<title>AegisVPN — быстрый и приватный VPN<\/title>/)
  assert.match(html, /name="description" content="Быстрый приватный VPN/)
  assert.match(html, /rel="canonical" href="https:\/\/aegisvpn\.org\/ru\/"/)
  assert.match(html, /hreflang="en" href="https:\/\/aegisvpn\.org\/en\/"/)
  assert.match(html, /hreflang="x-default" href="https:\/\/aegisvpn\.org\/ru\/"/)
})

test('localizer emits complete English metadata without leaking Russian title', () => {
  const html = localizeHtml(fixture, 'en')
  assert.match(html, /<html lang="en">/)
  assert.match(html, /<title>AegisVPN — private VPN for a steady connection<\/title>/)
  assert.match(html, /name="description" content="Fast, private VPN/)
  assert.match(html, /rel="canonical" href="https:\/\/aegisvpn\.org\/en\/"/)
  assert.doesNotMatch(html, /быстрый и приватный VPN/)
})
