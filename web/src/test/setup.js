import '@testing-library/jest-dom/vitest'
import { afterEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'

afterEach(() => {
  cleanup()
  document.body.style.overflow = ''
  document.documentElement.lang = ''
  document.title = ''
  window.history.replaceState(null, '', '/ru/')
})

Object.defineProperty(window, 'matchMedia', {
  configurable: true,
  value: vi.fn().mockImplementation((query) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })),
})

class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver = ResizeObserver

class IntersectionObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.IntersectionObserver = IntersectionObserver

HTMLCanvasElement.prototype.getContext = vi.fn(() => ({
  font: '',
  measureText: () => ({
    fontBoundingBoxAscent: 10,
    fontBoundingBoxDescent: 2,
    actualBoundingBoxAscent: 8,
  }),
}))
