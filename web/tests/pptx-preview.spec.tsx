import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { Suspense } from 'react'
import PptxPreview from '@/components/chat/preview/previewers/PptxPreview'
import KbFilePreview from '@/components/knowledge/KbFilePreview'

const fixture = vi.hoisted(() => ({ apiFetch: vi.fn() }))
vi.mock('@/lib/api', () => ({ apiFetch: fixture.apiFetch, apiUrl: (path: string) => path }))
vi.mock('next/dynamic', async () => {
  const { lazy } = await import('react')
  return { default: (loader: Parameters<typeof lazy>[0]) => lazy(loader) }
})
vi.mock('@/components/chat/preview/previewers/OfficePdfPreview', () => ({
  default: () => <div data-testid="pdf-conversion">PDF conversion</div>,
}))
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }))
vi.mock('@/components/chat/preview/previewers/OfficeTextPreview', () => ({
  default: ({ extractedTextUrl }: { extractedTextUrl?: string }) => (
    <div data-testid="text-fallback" data-url={extractedTextUrl}>
      Parsed text
    </div>
  ),
}))

beforeEach(() => {
  fixture.apiFetch.mockReset().mockResolvedValue({
    ok: true,
    headers: new Headers(),
    arrayBuffer: async () => new ArrayBuffer(8),
  })
})

function report(frame: HTMLIFrameElement, status: string) {
  act(() =>
    window.dispatchEvent(
      new MessageEvent('message', {
        source: frame.contentWindow,
        data: { type: 'pptx-preview-status', status },
      })
    )
  )
}

it.each([
  '/api/knowledge-bases/local/files/slides.pptx',
  '/api/knowledge-bases/remote/remote-file-preview/doc-1',
])('renders %s in an isolated browser frame without PDF conversion', async url => {
  const view = render(<PptxPreview url={url} filename="slides.pptx" />)
  const frame = await screen.findByTitle<HTMLIFrameElement>('slides.pptx')
  expect(frame).toHaveAttribute('sandbox', 'allow-scripts')
  const send = vi.spyOn(frame.contentWindow!, 'postMessage')
  fireEvent.load(frame)
  expect(send).toHaveBeenCalledWith(
    { type: 'pptx-preview-render', buffer: expect.any(ArrayBuffer) },
    '*'
  )
  report(frame, 'ready')
  expect(screen.queryByText('Loading preview…')).not.toBeInTheDocument()
  expect(fixture.apiFetch).toHaveBeenCalledTimes(1)
  expect(fixture.apiFetch).toHaveBeenCalledWith(
    url,
    expect.objectContaining({ signal: expect.any(AbortSignal) })
  )
  view.unmount()
  expect(frame.isConnected).toBe(false)
})

it('falls back to parsed text on rendering errors, including asynchronous chart errors', async () => {
  render(
    <PptxPreview
      url="/slides.pptx"
      filename="slides.pptx"
      extractedTextUrl="/remote-file-preview-text/doc-1"
    />
  )
  const frame = await screen.findByTitle<HTMLIFrameElement>('slides.pptx')
  report(frame, 'ready')
  report(frame, 'error')
  expect(await screen.findByTestId('text-fallback')).toHaveAttribute(
    'data-url',
    '/remote-file-preview-text/doc-1'
  )
  expect(frame.isConnected).toBe(false)
})

it.each([
  '/api/knowledge-bases/local/files/slides.pptx',
  '/api/knowledge-bases/remote/remote-file-preview/doc-1',
])('the knowledge pane sends %s directly to browser slides', async url => {
  render(
    <Suspense fallback={<div>Importing preview</div>}>
      <KbFilePreview
        source={{ filename: 'slides.pptx', url, officePdfUrl: '/must-not-convert?format=pdf' }}
      />
    </Suspense>
  )
  await screen.findByTitle('slides.pptx')
  expect(screen.queryByTestId('pdf-conversion')).not.toBeInTheDocument()
  expect(fixture.apiFetch).toHaveBeenCalledTimes(1)
  expect(fixture.apiFetch).toHaveBeenCalledWith(
    url,
    expect.objectContaining({ signal: expect.any(AbortSignal) })
  )
})

it('falls back when downloading the original fails', async () => {
  fixture.apiFetch.mockResolvedValue({ ok: false, status: 404 })
  render(<PptxPreview url="/missing.pptx" filename="missing.pptx" />)
  expect(await screen.findByTestId('text-fallback')).toBeInTheDocument()
  expect(screen.queryByTitle('missing.pptx')).not.toBeInTheDocument()
})

it("ignores another viewer's messages and discards an unfinished frame on file switch", async () => {
  const view = render(<PptxPreview url="/first.pptx" filename="first.pptx" />)
  const oldFrame = await screen.findByTitle<HTMLIFrameElement>('first.pptx')
  const oldWindow = oldFrame.contentWindow
  view.rerender(<PptxPreview url="/second.pptx" filename="second.pptx" />)
  const newFrame = await screen.findByTitle<HTMLIFrameElement>('second.pptx')
  await waitFor(() => expect(oldFrame.isConnected).toBe(false))
  act(() =>
    window.dispatchEvent(
      new MessageEvent('message', {
        source: oldWindow,
        data: { type: 'pptx-preview-status', status: 'error' },
      })
    )
  )
  expect(screen.queryByTestId('text-fallback')).not.toBeInTheDocument()
  report(newFrame, 'ready')
  expect(screen.queryByText('Loading preview…')).not.toBeInTheDocument()
})
