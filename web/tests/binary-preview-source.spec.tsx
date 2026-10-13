import { act, renderHook, waitFor } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { useBinarySource } from '@/components/chat/preview/previewers/useBinarySource'

const fixture = vi.hoisted(() => ({ apiFetch: vi.fn() }))
vi.mock('@/lib/api', () => ({ apiFetch: fixture.apiFetch }))

it('rejects oversized originals even when the proxy omits content-length', async () => {
  fixture.apiFetch.mockResolvedValue({
    ok: true,
    headers: new Headers(),
    arrayBuffer: async () => new ArrayBuffer(25 * 1024 * 1024 + 1),
  })
  const { result } = renderHook(() => useBinarySource('/large.xlsx'))
  await waitFor(() => expect(result.current.kind).toBe('error'))
  expect(result.current).toMatchObject({ message: expect.stringContaining('too large') })
})

it('ignores a completed download after its source is removed', async () => {
  let finish!: (buffer: ArrayBuffer) => void
  fixture.apiFetch.mockResolvedValue({
    ok: true,
    headers: new Headers(),
    arrayBuffer: () =>
      new Promise<ArrayBuffer>(resolve => {
        finish = resolve
      }),
  })
  const { result, rerender } = renderHook(({ url }) => useBinarySource(url), {
    initialProps: { url: '/first.xlsx' as string | null },
  })
  await waitFor(() => expect(finish).toBeDefined())
  rerender({ url: null })
  await act(async () => finish(new ArrayBuffer(8)))
  expect(result.current).toEqual({ kind: 'error', message: 'Preview source is not available.' })
})
