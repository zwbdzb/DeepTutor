'use client'

import { memo } from 'react'
import dynamic from 'next/dynamic'
import { Loader2 } from 'lucide-react'
import { apiUrl } from '@/lib/api'
import { previewKindFor, resolveSourceUrl, type FilePreviewSource } from './previewerFor'

// Keep lazy chunk loading inside the preview pane, not the page Suspense.
const PreviewLoading = () => (
  <div className="flex h-full items-center justify-center">
    <Loader2 className="h-4 w-4 animate-spin text-[var(--muted-foreground)]" />
  </div>
)
const PdfPreview = dynamic(() => import('./previewers/PdfPreview'), {
  loading: PreviewLoading,
  ssr: false,
})
const ImagePreview = dynamic(() => import('./previewers/ImagePreview'), {
  loading: PreviewLoading,
  ssr: false,
})
const VideoPreview = dynamic(() => import('./previewers/VideoPreview'), {
  loading: PreviewLoading,
  ssr: false,
})
const SvgPreview = dynamic(() => import('./previewers/SvgPreview'), {
  loading: PreviewLoading,
  ssr: false,
})
const MarkdownPreview = dynamic(() => import('./previewers/MarkdownPreview'), {
  loading: PreviewLoading,
  ssr: false,
})
const TextPreview = dynamic(() => import('./previewers/TextPreview'), {
  loading: PreviewLoading,
  ssr: false,
})
const DocxPreview = dynamic(() => import('./previewers/DocxPreview'), {
  loading: PreviewLoading,
  ssr: false,
})
const XlsxPreview = dynamic(() => import('./previewers/XlsxPreview'), {
  loading: PreviewLoading,
  ssr: false,
})
const PptxPreview = dynamic(() => import('./previewers/PptxPreview'), {
  loading: PreviewLoading,
  ssr: false,
})
const OfficePdfPreview = dynamic(() => import('./previewers/OfficePdfPreview'), {
  loading: PreviewLoading,
  ssr: false,
})
const OfficeTextPreview = dynamic(() => import('./previewers/OfficeTextPreview'), {
  loading: PreviewLoading,
  ssr: false,
})
const FallbackPreview = dynamic(() => import('./previewers/FallbackPreview'), {
  loading: PreviewLoading,
  ssr: false,
})

// Memo preserves the drawer's cheap close animation. A new source remounts
// the renderer so a previous file's state cannot leak into the next preview.
export default memo(function FilePreviewContent({ source }: { source: FilePreviewSource }) {
  const url = resolveSourceUrl(source, apiUrl)
  return (
    <PreviewContent
      key={JSON.stringify([url, source.id, source.filename])}
      source={source}
      url={url}
    />
  )
})

function PreviewContent({ source, url }: { source: FilePreviewSource; url: string | null }) {
  const { filename } = source
  const kind = previewKindFor(source)
  const extractedTextUrl = source.extractedTextUrl
    ? resolveSourceUrl({ filename, url: source.extractedTextUrl }, apiUrl)
    : null
  const officeText = (
    <OfficeTextPreview
      filename={filename}
      url={url}
      extractedText={source.extractedText}
      extractedTextUrl={extractedTextUrl}
    />
  )
  if (kind === 'pptx') {
    return (
      <PptxPreview
        url={url}
        filename={filename}
        extractedText={source.extractedText}
        extractedTextUrl={extractedTextUrl}
      />
    )
  }
  if (kind === 'office-text' && !url) return officeText
  if (!url) return <FallbackPreview filename={filename} url={null} reason="legacy" />

  const conversionUrl = source.officePdfUrl
    ? (resolveSourceUrl({ filename, url: source.officePdfUrl }, apiUrl) ?? undefined)
    : undefined
  switch (kind) {
    case 'office-text':
      return (
        <OfficePdfPreview
          url={url}
          filename={filename}
          conversionUrl={conversionUrl}
          fallback={officeText}
        />
      )
    case 'docx':
      return (
        <OfficePdfPreview
          url={url}
          filename={filename}
          conversionUrl={conversionUrl}
          fallback={<DocxPreview url={url} />}
        />
      )
    case 'xlsx':
      return <XlsxPreview url={url} />
    case 'pdf':
      return <PdfPreview url={url} filename={filename} />
    case 'image':
      return <ImagePreview url={url} filename={filename} />
    case 'video':
      return <VideoPreview url={url} filename={filename} />
    case 'svg':
      return <SvgPreview url={url} filename={filename} />
    case 'markdown':
      return (
        <div className="h-full overflow-y-auto">
          <MarkdownPreview url={url} />
        </div>
      )
    case 'code':
    case 'text':
      return (
        <div className="h-full overflow-y-auto">
          <TextPreview url={url} filename={filename} />
        </div>
      )
    default:
      return <FallbackPreview filename={filename} url={url} />
  }
}
