import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { pathToFileURL } from 'node:url'

const ORIGIN = 'https://aegisvpn.org'
const META = {
  ru: {
    title: 'AegisVPN — быстрый и приватный VPN',
    description: 'Быстрый приватный VPN на VLESS Reality: одна подписка для всех локаций.',
  },
  en: {
    title: 'AegisVPN — private VPN for a steady connection',
    description: 'Fast, private VPN with VLESS Reality and one subscription for every location.',
  },
}

const escapeAttribute = (value) => value.replaceAll('&', '&amp;').replaceAll('"', '&quot;')

export function localizeHtml(source, lang) {
  const meta = META[lang]
  if (!meta) throw new Error(`Unsupported locale: ${lang}`)

  let html = source
    .replace(/<html\b[^>]*\blang="[^"]*"[^>]*>/i, `<html lang="${lang}">`)
    .replace(/<title>[\s\S]*?<\/title>/i, `<title>${meta.title}</title>`)
    .replace(
      /<meta\s+name="description"\s+content="[^"]*"\s*\/?>/i,
      `<meta name="description" content="${escapeAttribute(meta.description)}" />`,
    )
    .replace(/\s*<link\s+rel="(?:canonical|alternate)"[^>]*>\s*/gi, '\n')

  const links = [
    `<link rel="canonical" href="${ORIGIN}/${lang}/" />`,
    `<link rel="alternate" hreflang="ru" href="${ORIGIN}/ru/" />`,
    `<link rel="alternate" hreflang="en" href="${ORIGIN}/en/" />`,
    `<link rel="alternate" hreflang="x-default" href="${ORIGIN}/ru/" />`,
  ].join('\n    ')
  html = html.replace('</head>', `    ${links}\n  </head>`)
  return html
}

async function main() {
  const sourcePath = new URL('../dist/index.html', import.meta.url)
  const source = await readFile(sourcePath, 'utf8')
  for (const lang of Object.keys(META)) {
    const dir = new URL(`../dist/${lang}/`, import.meta.url)
    await mkdir(dir, { recursive: true })
    await writeFile(new URL('index.html', dir), localizeHtml(source, lang))
  }
  await writeFile(sourcePath, localizeHtml(source, 'ru'))
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main()
}
