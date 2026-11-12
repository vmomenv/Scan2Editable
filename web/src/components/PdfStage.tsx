import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react'
import type { PDFDocumentProxy } from 'pdfjs-dist'
import { PageCanvas } from './PageCanvas'

interface Props {
  file: File | null
  pdf: PDFDocumentProxy | null
  numPages: number
  aspect: number
  page: number
  loading: boolean
  error: string
  onPage: (page: number) => void
  onPickFile: (file: File) => void
}

function cardStyle(rel: number): CSSProperties {
  if (rel === 0) {
    return {
      transform: 'translate3d(0, 0, 0) rotateY(0deg) rotateZ(0deg) scale(1)',
      opacity: 1,
      zIndex: 30,
      transitionDelay: '0ms',
    }
  }
  if (rel === 1) {
    return {
      transform: 'translate3d(16px, -14px, -70px) rotateX(2.5deg) scale(0.956)',
      opacity: 0.72,
      zIndex: 20,
    }
  }
  if (rel === 2) {
    return {
      transform: 'translate3d(30px, -26px, -140px) rotateX(4deg) scale(0.914)',
      opacity: 0.42,
      zIndex: 12,
    }
  }
  if (rel === 3) {
    return {
      transform: 'translate3d(42px, -36px, -200px) rotateX(5deg) scale(0.878)',
      opacity: 0.16,
      zIndex: 6,
    }
  }
  if (rel === -1) {
    return {
      transform: 'translate3d(-74%, -5%, 150px) rotateY(46deg) rotateZ(-7deg) scale(0.97)',
      opacity: 0,
      zIndex: 40,
    }
  }
  if (rel < -1) {
    return {
      transform: 'translate3d(-105%, -9%, 210px) rotateY(54deg) rotateZ(-9deg) scale(0.95)',
      opacity: 0,
      zIndex: 40,
    }
  }
  return {
    transform: 'translate3d(56px, -46px, -240px) scale(0.86)',
    opacity: 0,
    zIndex: 1,
  }
}

export function PdfStage(props: Props) {
  const { file, pdf, numPages, aspect, page, loading, error, onPage, onPickFile } = props
  const stageRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const stateRef = useRef({ page, numPages, onPage })
  stateRef.current = { page, numPages, onPage }

  const [deckSize, setDeckSize] = useState({ width: 560, height: 760 })
  const [dragging, setDragging] = useState(false)

  useEffect(() => {
    const el = stageRef.current
    if (!el) return
    const compute = () => {
      const rect = el.getBoundingClientRect()
      const padX = rect.width < 560 ? 58 : 150
      const padY = rect.height < 560 ? 104 : 150
      const availW = Math.max(200, rect.width - padX)
      const availH = Math.max(280, rect.height - padY)
      let height = availH
      let width = height * aspect
      if (width > availW) {
        width = availW
        height = width / aspect
      }
      setDeckSize({ width, height })
    }
    compute()
    const ro = new ResizeObserver(compute)
    ro.observe(el)
    return () => ro.disconnect()
  }, [aspect])

  // 滚轮：向下 = 下一张（向左丢出），向上 = 上一张（飞回来）
  useEffect(() => {
    const el = stageRef.current
    if (!el) return
    let last = 0
    let acc = 0
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      if (!stateRef.current.numPages) return
      const now = performance.now()
      if (now - last < 380) {
        acc = 0
        return
      }
      acc += e.deltaY
      if (Math.abs(acc) < 10) return
      const dir = acc > 0 ? 1 : -1
      acc = 0
      last = now
      const current = stateRef.current.page
      const next = Math.min(stateRef.current.numPages, Math.max(1, current + dir))
      if (next !== current) stateRef.current.onPage(next)
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  // 键盘
  useEffect(() => {
    if (!numPages) return
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return
      if (e.key === ' ' && target?.tagName === 'BUTTON') return
      let dir = 0
      if (e.key === 'ArrowDown' || e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ' ') dir = 1
      if (e.key === 'ArrowUp' || e.key === 'ArrowLeft' || e.key === 'PageUp') dir = -1
      if (!dir) return
      e.preventDefault()
      onPage(Math.min(numPages, Math.max(1, page + dir)))
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [numPages, page, onPage])

  const openPicker = useCallback(() => inputRef.current?.click(), [])

  const handleFiles = useCallback(
    (files: FileList | null) => {
      const picked = files?.[0]
      if (!picked) return
      if (!/\.pdf$/i.test(picked.name) && picked.type !== 'application/pdf') return
      onPickFile(picked)
    },
    [onPickFile],
  )

  const ratio = numPages > 1 ? ((page - 1) / (numPages - 1)) * 100 : 100

  return (
    <section
      ref={stageRef}
      className={`stage${dragging ? ' is-dragging' : ''}${file ? ' has-file' : ''}`}
      onDragOver={(e) => {
        e.preventDefault()
        setDragging(true)
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragging(false)
      }}
      onDrop={(e) => {
        e.preventDefault()
        setDragging(false)
        handleFiles(e.dataTransfer.files)
      }}
    >
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf,.pdf"
        hidden
        onChange={(e) => {
          handleFiles(e.target.files)
          e.target.value = ''
        }}
      />

      {!file ? (
        <button className="dropzone" onClick={openPicker} type="button">
          <span className="drop-glow" aria-hidden />
          <span className="drop-icon" aria-hidden>
            <svg viewBox="0 0 64 64" fill="none">
              <path d="M14 8h24l12 12v36H14z" stroke="currentColor" strokeWidth="2.2" strokeLinejoin="round" />
              <path d="M38 8v12h12" stroke="currentColor" strokeWidth="2.2" strokeLinejoin="round" />
              <path d="M23 32h18M23 40h12" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
              <path d="M46 38l2.2 5.8L54 46l-5.8 2.2L46 54l-2.2-5.8L38 46l5.8-2.2z" fill="currentColor" opacity=".9" />
            </svg>
          </span>
          <span className="drop-title">把扫描版 PDF 放到这里</span>
          <span className="drop-sub">左侧逐页预览 · 滚轮向左丢出，向上翻回</span>
          <span className="drop-btn">选择 PDF 文件</span>
          <span className="drop-hint">本地处理 · 文件不会离开这台机器</span>
        </button>
      ) : (
        <>
          <div className="stage-top">
            <div className="file-chip">
              <span className="file-dot" />
              <span className="file-name">{file.name}</span>
              <span className="file-meta">
                {loading ? '读取中…' : numPages ? `${numPages} 页` : '—'}
              </span>
            </div>
            <button className="ghost-btn" type="button" onClick={openPicker}>
              换一份
            </button>
          </div>

          <div className="stage-body">
            <div className="deck" style={{ width: deckSize.width, height: deckSize.height }}>
              {pdf &&
                Array.from({ length: numPages }, (_, i) => {
                  const n = i + 1
                  const rel = n - page
                  return (
                    <div
                      key={n}
                      className={`sheet${rel === 0 ? ' sheet-active' : ''}`}
                      style={cardStyle(rel)}
                      aria-hidden={rel !== 0}
                    >
                      {Math.abs(rel) <= 2 ? (
                        <PageCanvas pdf={pdf} pageNumber={n} eager={Math.abs(rel) <= 2} />
                      ) : (
                        <div className="sheet-placeholder" />
                      )}
                      <div className="sheet-gloss" aria-hidden />
                    </div>
                  )
                })}
            </div>

            {error && <div className="stage-error">{error}</div>}
            {loading && <div className="stage-loading">正在解析 PDF…</div>}
          </div>

          <div className="stage-controls">
            <button
              className="nav-btn"
              type="button"
              onClick={() => onPage(Math.max(1, page - 1))}
              disabled={page <= 1}
              aria-label="上一页"
            >
              <svg viewBox="0 0 20 20" fill="none" aria-hidden>
                <path d="M12 5l-5 5 5 5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
            <div className="page-meter">
              <div className="page-count">
                <strong>{String(page).padStart(2, '0')}</strong>
                <span>/ {String(Math.max(numPages, 1)).padStart(2, '0')}</span>
              </div>
              <div className="page-rail">
                <span className="page-rail-fill" style={{ width: `${ratio}%` }} />
              </div>
            </div>
            <button
              className="nav-btn"
              type="button"
              onClick={() => onPage(Math.min(numPages, page + 1))}
              disabled={page >= numPages}
              aria-label="下一页"
            >
              <svg viewBox="0 0 20 20" fill="none" aria-hidden>
                <path d="M8 5l5 5-5 5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
            <span className="wheel-hint">滚轮 / ↑↓ 翻页</span>
          </div>
        </>
      )}

      {dragging && (
        <div className="drag-veil">
          <span>松手即可载入 PDF</span>
        </div>
      )}
    </section>
  )
}
