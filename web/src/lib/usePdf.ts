import { useEffect, useState } from 'react'
import type { PDFDocumentProxy } from 'pdfjs-dist'
import { pdfjs } from './pdf'

interface PdfState {
  doc: PDFDocumentProxy | null
  numPages: number
  aspect: number
  loading: boolean
  error: string
}

export function usePdfDocument(file: File | null): PdfState {
  const [state, setState] = useState<PdfState>({
    doc: null,
    numPages: 0,
    aspect: 1 / Math.SQRT2,
    loading: false,
    error: '',
  })

  useEffect(() => {
    if (!file) {
      setState({ doc: null, numPages: 0, aspect: 1 / Math.SQRT2, loading: false, error: '' })
      return
    }
    let cancelled = false
    let task: ReturnType<typeof pdfjs.getDocument> | null = null
    setState({ doc: null, numPages: 0, aspect: 1 / Math.SQRT2, loading: true, error: '' })

    ;(async () => {
      try {
        const buffer = await file.arrayBuffer()
        if (cancelled) return
        task = pdfjs.getDocument({ data: new Uint8Array(buffer), isEvalSupported: false })
        const doc = await task.promise
        if (cancelled) {
          void doc.destroy()
          return
        }
        const first = await doc.getPage(1)
        const viewport = first.getViewport({ scale: 1 })
        if (cancelled) {
          void doc.destroy()
          return
        }
        setState({
          doc,
          numPages: doc.numPages,
          aspect: viewport.width / viewport.height,
          loading: false,
          error: '',
        })
      } catch (err) {
        if (cancelled) return
        const message = err instanceof Error ? err.message : String(err)
        setState({
          doc: null,
          numPages: 0,
          aspect: 1 / Math.SQRT2,
          loading: false,
          error: message.includes('password') ? '这份 PDF 有密码，暂时无法预览' : 'PDF 读取失败，文件可能已损坏',
        })
      }
    })()

    return () => {
      cancelled = true
      if (task) void task.destroy()
    }
  }, [file])

  return state
}
