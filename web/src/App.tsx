import { useCallback, useEffect, useState } from 'react'
import { ConfigPanel } from './components/ConfigPanel'
import { PdfStage } from './components/PdfStage'
import { cancelJob, checkHealth, createJob, getJob, uploadPdf } from './lib/api'
import { usePdfDocument } from './lib/usePdf'
import { DEFAULT_OPTIONS, type JobOptions, type JobSnapshot } from './types'

export default function App() {
  const [file, setFile] = useState<File | null>(null)
  const [page, setPage] = useState(1)
  const [options, setOptions] = useState<JobOptions>(DEFAULT_OPTIONS)
  const [job, setJob] = useState<JobSnapshot | null>(null)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState('')
  const [backendOk, setBackendOk] = useState(false)

  const { doc, numPages, aspect, loading, error: pdfError } = usePdfDocument(file)

  useEffect(() => {
    let alive = true
    const ping = async () => {
      try {
        const health = await checkHealth()
        if (alive) setBackendOk(Boolean(health.ok && health.script))
      } catch {
        if (alive) setBackendOk(false)
      }
    }
    void ping()
    const timer = setInterval(ping, 12000)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [])

  useEffect(() => {
    setPage(1)
    setJob(null)
    setError('')
  }, [file])

  useEffect(() => {
    if (!job || (job.status !== 'queued' && job.status !== 'running')) return
    let stopped = false
    const tick = async () => {
      try {
        const next = await getJob(job.id)
        if (!stopped) setJob(next)
      } catch {
        /* 单次轮询失败不打断，下一次继续 */
      }
    }
    const timer = setInterval(tick, 900)
    return () => {
      stopped = true
      clearInterval(timer)
    }
  }, [job?.id, job?.status])

  const handlePickFile = useCallback((picked: File) => {
    setFile(picked)
    const nextTitle = picked.name.replace(/\.pdf$/i, '')
    setOptions((o) => {
      const previousAuto = file ? file.name.replace(/\.pdf$/i, '') : ''
      if (!o.title || o.title === previousAuto) return { ...o, title: nextTitle }
      return o
    })
  }, [file])

  const handleChange = useCallback((patch: Partial<JobOptions>) => {
    setOptions((o) => ({ ...o, ...patch }))
  }, [])

  const handleGenerate = useCallback(async () => {
    if (!file) return
    setError('')
    setStarting(true)
    try {
      const uploaded = await uploadPdf(file)
      const { jobId } = await createJob(uploaded.fileId, numPages, {
        ...options,
        title: options.title || file.name.replace(/\.pdf$/i, ''),
      })
      setJob(await getJob(jobId))
      setPage(1)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setStarting(false)
    }
  }, [file, numPages, options])

  const handleCancel = useCallback(async () => {
    if (!job) return
    try {
      await cancelJob(job.id)
      setJob(await getJob(job.id))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [job])

  return (
    <div className="app">
      <div className="ambient" aria-hidden />

      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden>
            <svg viewBox="0 0 24 24" fill="none">
              <path d="M6 3h8l4 4v14H6z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />
              <path d="M14 3v4h4" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />
              <path d="M9 12h6M9 16h4" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
            </svg>
          </span>
          <span className="brand-text">
            <span className="brand-name">Scan2Editable</span>
            <span className="brand-sub">Scanned PDF → Editable Word</span>
          </span>
        </div>
        <div className="topbar-right">
          <span className="pill">公式 → Word 原生公式</span>
          <span className="pill">插图原位还原</span>
        </div>
      </header>

      <main className="layout">
        <PdfStage
          file={file}
          pdf={doc}
          numPages={numPages}
          aspect={aspect}
          page={page}
          loading={loading}
          error={pdfError}
          onPage={setPage}
          onPickFile={handlePickFile}
        />
        <ConfigPanel
          options={options}
          onChange={handleChange}
          hasFile={Boolean(file)}
          starting={starting}
          job={job}
          error={error}
          backendOk={backendOk}
          onGenerate={handleGenerate}
          onCancel={handleCancel}
        />
      </main>
    </div>
  )
}
