import { copyFileSync, mkdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

/** Bundle the pinned browser renderer for offline and packaged previews. */
export function copyPptxAssets() {
  const target = path.join(webRoot, 'public', 'pptx-preview')
  mkdirSync(target, { recursive: true })
  copyFileSync(
    path.join(webRoot, 'node_modules', 'pptx-preview', 'dist', 'pptx-preview.umd.js'),
    path.join(target, 'pptx-preview.umd.js')
  )
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) copyPptxAssets()
