import { useEffect, useRef } from 'react'
import type { PDFDocumentProxy } from 'pdfjs-dist'

interface Props {
  pdf: PDFDocumentProxy
  pageNumber: number
  eager: boolean
}

/** 把某一页渲染到 canvas，按约 1100px 的宽度出图，保证高倍屏清晰。 */
export function PageCanvas({ pdf, pageNumber, eager }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    if (!eager) return
    let cancelled = false
    let renderTask: ReturnType<Awaited<ReturnType<PDFDocumentProxy['getPage']>>['render']> | null = null

    ;(async () => {
      const page = await pdf.getPage(pageNumber)
      if (cancelled) return
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      const base = page.getViewport({ scale: 1 })
      const scale = (1100 * dpr) / base.width
      const viewport = page.getViewport({ scale })
      const canvas = canvasRef.current
      if (!canvas) return
      canvas.width = Math.floor(viewport.width)
      canvas.height = Math.floor(viewport.height)
      const ctx = canvas.getContext('2d', { alpha: false })
      if (!ctx) return
      renderTask = page.render({ canvasContext: ctx, viewport })
      await renderTask.promise
    })().catch(() => {
      /* 翻页时取消渲染会抛错，忽略 */
    })

    return () => {
      cancelled = true
      renderTask?.cancel()
    }
  }, [pdf, pageNumber, eager])

  return <canvas ref={canvasRef} className="page-canvas" />
}
