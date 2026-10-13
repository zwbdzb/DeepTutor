'use client'

import { useLayoutEffect, useRef, useState } from 'react'
import { Loader2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useBinarySource } from './useBinarySource'
import OfficeTextPreview from './OfficeTextPreview'

/** Render original PPTX slides without an Office installation or conversion. */
export default function PptxPreview({
  url,
  filename,
  extractedText,
  extractedTextUrl,
}: {
  url: string | null
  filename: string
  extractedText?: string
  extractedTextUrl?: string | null
}) {
  const src = useBinarySource(url)
  const fallback = (
    <OfficeTextPreview
      filename={filename}
      url={url}
      extractedText={extractedText}
      extractedTextUrl={extractedTextUrl}
    />
  )
  if (src.kind === 'error') return fallback
  if (src.kind === 'loading') return <Loading />
  return <PptxFrame key={url} buffer={src.buffer} filename={filename} fallback={fallback} />
}

function Loading() {
  const { t } = useTranslation()
  return (
    <div className="absolute inset-0 flex items-center justify-center gap-2 text-[12px] text-[var(--muted-foreground)]">
      <Loader2 size={14} className="animate-spin" />
      <span>{t('Loading preview…')}</span>
    </div>
  )
}

function PptxFrame({
  buffer,
  filename,
  fallback,
}: {
  buffer: ArrayBuffer
  filename: string
  fallback: React.ReactNode
}) {
  const frame = useRef<HTMLIFrameElement>(null)
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading')
  useLayoutEffect(() => {
    const receive = (event: MessageEvent) => {
      if (
        event.source !== frame.current?.contentWindow ||
        event.data?.type !== 'pptx-preview-status'
      )
        return
      if (event.data.status === 'ready' || event.data.status === 'error')
        setStatus(event.data.status)
    }
    window.addEventListener('message', receive)
    return () => window.removeEventListener('message', receive)
  }, [])
  if (status === 'error') return fallback
  return (
    <div className="relative h-full w-full bg-[var(--muted)]/30">
      {status === 'loading' && <Loading />}
      {/* The plugin owns global chart listeners and media URLs. A separate
          realm per preview isolates those resources and releases them on close. */}
      <iframe
        ref={frame}
        title={filename}
        src="/pptx-preview/frame.html"
        sandbox="allow-scripts"
        className={`h-full w-full border-0 ${status === 'loading' ? 'invisible' : ''}`}
        onLoad={() =>
          frame.current?.contentWindow?.postMessage({ type: 'pptx-preview-render', buffer }, '*')
        }
        onError={() => setStatus('error')}
      />
    </div>
  )
}
