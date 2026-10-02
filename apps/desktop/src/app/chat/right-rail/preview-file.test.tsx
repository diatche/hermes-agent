import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as desktopFs from '@/lib/desktop-fs'

import { MarkdownPreview } from './preview-file'

// Behavior tests for the .md file preview renderer: input markdown goes
// through normalizeFilePreviewMath -> Streamdown (+ KaTeX math plugin) and must
// come out as real rendered elements, matching what the chat transcript
// renderer produces. Guards the regression where the preview was a bare
// Streamdown pass with no math plugin and no table/img/a components.
describe('MarkdownPreview', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders block and inline math through KaTeX', () => {
    // KaTeX marks its output; raw "$" delimiters must be gone.
    const { container } = render(
      <MarkdownPreview
        text={'Formula:\n\n$$\nx = \\frac{-b \\pm \\sqrt{b^2-4ac}}{2a}\n$$\n\nInline $a^2 + b^2 = c^2$ too.'}
      />
    )

    expect(container.querySelector('.katex')).not.toBeNull()
    expect(screen.queryByText(/\$\$/)).toBeNull()
  })

  it('renders GFM tables with header and body cells', () => {
    const { container } = render(<MarkdownPreview text={'| h1 | h2 |\n| --- | --- |\n| a | b |'} />)

    const table = container.querySelector('table')
    expect(table).not.toBeNull()
    expect(table?.querySelector('thead th')?.textContent).toBe('h1')
    expect(table?.querySelector('tbody td')?.textContent).toBe('a')
  })

  it('renders images with alt text', () => {
    const { container } = render(<MarkdownPreview text={'![a chart](https://example.com/chart.png)'} />)

    const img = container.querySelector('img')
    expect(img?.getAttribute('alt')).toBe('a chart')
    expect(img?.getAttribute('src')).toBe('https://example.com/chart.png')
  })

  it('renders markdown inside styled disclosure sections', () => {
    const readImage = vi.spyOn(desktopFs, 'readDesktopFileDataUrl').mockResolvedValue('data:image/png;base64,aGVybWVz')

    const { container } = render(
      <MarkdownPreview
        filePath="/work/reviews/REVIEW.md"
        text={'<details><summary>Wider context</summary>\n\n![Context crop](review/context.png)\n\n</details>'}
      />
    )

    const details = container.querySelector('details')
    const summary = details?.querySelector('summary')

    expect(summary?.textContent).toBe('Wider context')
    expect(summary?.getAttribute('class')).toContain('preview-markdown-summary')

    return waitFor(() => {
      const image = details?.querySelector('img')

      expect(readImage).toHaveBeenCalledWith('/work/reviews/review/context.png')
      expect(image?.getAttribute('alt')).toBe('Context crop')
      expect(image?.getAttribute('src')).toBe('data:image/png;base64,aGVybWVz')
    })
  })

  it('renders external links to open in a new tab safely', () => {
    const { container } = render(<MarkdownPreview text={'[docs](https://example.com/docs)'} />)

    const anchor = container.querySelector('a')
    expect(anchor?.getAttribute('href')).toBe('https://example.com/docs')
    expect(anchor?.getAttribute('target')).toBe('_blank')
    expect(anchor?.getAttribute('rel')).toBe('noopener noreferrer')
  })
})
