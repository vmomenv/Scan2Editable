import { useEffect, useRef, type ReactNode } from 'react'
import { PROVIDERS, type JobOptions, type JobSnapshot } from '../types'

interface Props {
  options: JobOptions
  onChange: (patch: Partial<JobOptions>) => void
  hasFile: boolean
  starting: boolean
  job: JobSnapshot | null
  error: string
  backendOk: boolean
  onGenerate: () => void
  onCancel: () => void
}

function Section({ label, children }: { label: string; children: ReactNode }) {
  return (
    <section className="cfg-section">
      <h3 className="cfg-label">{label}</h3>
      <div className="cfg-body">{children}</div>
    </section>
  )
}

function Toggle({
  label,
  hint,
  checked,
  onChange,
}: {
  label: string
  hint?: string
  checked: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <label className="toggle-row">
      <span className="toggle-text">
        <span className="toggle-name">{label}</span>
        {hint && <span className="toggle-hint">{hint}</span>}
      </span>
      <span className={`switch${checked ? ' on' : ''}`} aria-hidden>
        <span className="knob" />
      </span>
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
    </label>
  )
}

function Segmented<T extends string | number>({
  value,
  items,
  onChange,
}: {
  value: T
  items: Array<{ value: T; label: string }>
  onChange: (v: T) => void
}) {
  return (
    <div className="segmented">
      {items.map((item) => (
        <button
          key={String(item.value)}
          type="button"
          className={item.value === value ? 'seg on' : 'seg'}
          onClick={() => onChange(item.value)}
        >
          {item.label}
        </button>
      ))}
    </div>
  )
}

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export function ConfigPanel(props: Props) {
  const { options, onChange, hasFile, starting, job, error, backendOk, onGenerate, onCancel } = props
  const logRef = useRef<HTMLDivElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const running = job?.status === 'running' || job?.status === 'queued'
  const percent = job ? Math.round((job.status === 'success' ? 1 : job.progress) * 100) : 0
  const preset = PROVIDERS.find((p) => p.id === options.provider) || PROVIDERS[0]

  useEffect(() => {
    const el = logRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [job?.logs.length, job?.status])

  useEffect(() => {
    if (job?.status !== 'success') return
    const el = scrollRef.current
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  }, [job?.status])

  useEffect(() => {
    if (!job) return
    const el = scrollRef.current
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  }, [job?.id])

  const buttonLabel = !hasFile
    ? '先选择一个 PDF'
    : starting
      ? '正在上传…'
      : running
        ? `生成中${percent > 0 ? ` · ${percent}%` : ''}`
        : job?.status === 'success'
          ? '重新生成'
          : job?.status === 'error' || job?.status === 'canceled'
            ? '重试'
            : options.exportDocx
              ? '生成可编辑 Word'
              : '生成 Markdown'

  return (
    <aside className="panel">
      <header className="panel-head">
        <div>
          <p className="panel-eyebrow">转换配置</p>
          <h2 className="panel-title">导出设置</h2>
        </div>
        <span className={`health ${backendOk ? 'ok' : 'off'}`}>
          <i />
          {backendOk ? '服务在线' : '服务未连接'}
        </span>
      </header>

      <div className="panel-scroll" ref={scrollRef}>
        <Section label="文档">
          <input
            className="text-input"
            type="text"
            value={options.title}
            placeholder="默认使用 PDF 文件名"
            onChange={(e) => onChange({ title: e.target.value })}
          />
        </Section>

        <Section label="输出">
          <Segmented
            value={options.exportDocx ? 'word' : 'md'}
            items={[
              { value: 'word', label: 'Word + Markdown' },
              { value: 'md', label: '仅 Markdown' },
            ]}
            onChange={(v) => onChange({ exportDocx: v === 'word' })}
          />
          <div className="field-row">
            <label className="field">
              <span className="field-label">页码范围</span>
              <input
                className="text-input"
                type="text"
                value={options.pages}
                placeholder="全部页，如 1,3-5"
                onChange={(e) => onChange({ pages: e.target.value })}
              />
            </label>
          </div>
          <div className="field">
            <span className="field-label">
              渲染清晰度 <em>{options.dpi} DPI</em>
            </span>
            <Segmented
              value={options.dpi}
              items={[
                { value: 200, label: '200 省流' },
                { value: 300, label: '300 推荐' },
                { value: 400, label: '400 精修' },
              ]}
              onChange={(v) => onChange({ dpi: v })}
            />
          </div>
        </Section>

        <Section label="插图定位">
          <div className="field">
            <span className="field-label">
              闭环精修轮次 <em>{options.refine === 0 ? '关闭' : `${options.refine} 轮`}</em>
            </span>
            <input
              className="range"
              type="range"
              min={0}
              max={5}
              step={1}
              value={options.refine}
              onChange={(e) => onChange({ refine: Number(e.target.value) })}
            />
            <div className="range-scale">
              <span>更快</span>
              <span>更准</span>
            </div>
          </div>
          <Toggle
            label="回喂校验插图"
            hint="裁完再让模型确认一次，不过则分块兜底"
            checked={options.verify}
            onChange={(v) => onChange({ verify: v })}
          />
          <Toggle
            label="裁掉多余白边"
            hint="按墨迹投影收紧插图边框"
            checked={options.trim}
            onChange={(v) => onChange({ trim: v })}
          />
        </Section>

        <Section label="模型">
          <select
            className="text-input select"
            value={options.provider}
            onChange={(e) => {
              const next = PROVIDERS.find((p) => p.id === e.target.value)
              onChange({ provider: e.target.value, model: '', baseUrl: next?.baseUrl ?? '' })
            }}
          >
            {PROVIDERS.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>

          <details className="advanced">
            <summary>
              <span>接口与并发</span>
              <svg viewBox="0 0 16 16" fill="none" aria-hidden>
                <path d="M6 4l4 4-4 4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </summary>
            <div className="advanced-body">
              <label className="field">
                <span className="field-label">模型</span>
                <input
                  className="text-input"
                  type="text"
                  value={options.model}
                  placeholder={preset.model || '模型名'}
                  onChange={(e) => onChange({ model: e.target.value })}
                />
              </label>
              <label className="field">
                <span className="field-label">API Base URL</span>
                <input
                  className="text-input"
                  type="text"
                  value={options.baseUrl}
                  placeholder={preset.baseUrl}
                  onChange={(e) => onChange({ baseUrl: e.target.value })}
                />
              </label>
              <label className="field">
                <span className="field-label">API Key</span>
                <input
                  className="text-input"
                  type="password"
                  value={options.apiKey}
                  placeholder="留空则用环境变量 / 本机凭据"
                  autoComplete="off"
                  onChange={(e) => onChange({ apiKey: e.target.value })}
                />
              </label>
              <label className="field field-inline">
                <span className="field-label">插图并发</span>
                <input
                  className="text-input mini"
                  type="number"
                  min={1}
                  max={16}
                  value={options.workers}
                  onChange={(e) => onChange({ workers: Math.min(16, Math.max(1, Number(e.target.value) || 1)) })}
                />
              </label>
              <Toggle label="输出调试框图" hint="把 bbox 画在页面上，便于人工核对" checked={options.debug} onChange={(v) => onChange({ debug: v })} />
              <Toggle label="忽略模型缓存" hint="强制重新请求，调试时用" checked={options.noCache} onChange={(v) => onChange({ noCache: v })} />
              <Toggle label="保留渲染页 PNG" hint="输出 pages/ 目录，占空间" checked={options.keepPages} onChange={(v) => onChange({ keepPages: v })} />
            </div>
          </details>
        </Section>

        {job && (
          <Section label="任务">
            <div className={`job-status ${job.status}`}>
              <span className="job-dot" />
              <span className="job-text">
                {job.status === 'queued' && '排队中…'}
                {job.status === 'running' && `正在处理${job.currentPage ? `第 ${job.currentPage} 页` : ''}${job.totalPages ? ` / 共 ${job.totalPages} 页` : ''}`}
                {job.status === 'success' && '转换完成'}
                {job.status === 'error' && (job.error || '转换失败')}
                {job.status === 'canceled' && '已取消'}
              </span>
            </div>
            <div className="logbox" ref={logRef}>
              {job.logs.length === 0 && <div className="log-line info">等待开始…</div>}
              {job.logs.map((log, i) => (
                <div key={i} className={`log-line ${log.level}`}>
                  {log.text}
                </div>
              ))}
            </div>
          </Section>
        )}

        {job?.status === 'success' && (
          <div className="result-card">
            <div className="result-head">
              <span className="result-check">
                <svg viewBox="0 0 20 20" fill="none" aria-hidden>
                  <path d="M5 10.5l3.2 3.2L15 6.6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </span>
              <div>
                <p className="result-title">文件已生成</p>
                <p className="result-sub">
                  {job.outputs.docx?.name || job.outputs.md?.name}
                  {job.outputs.docx ? ` · ${formatSize(job.outputs.docx.size)}` : ''}
                </p>
              </div>
            </div>
            <div className="result-actions">
              {job.outputs.docx && (
                <a className="download primary" href={job.outputs.docx.url} download>
                  下载 Word
                </a>
              )}
              {job.outputs.md && (
                <a className="download" href={job.outputs.md.url} download>
                  下载 Markdown
                </a>
              )}
            </div>
          </div>
        )}

        {error && <p className="form-error">{error}</p>}
      </div>

      <footer className="panel-action">
        {(running || starting) && (
          <div className={`progress-track${starting ? ' idle' : ''}`}>
            <span className="progress-fill" style={{ width: starting ? '100%' : `${percent}%` }} />
          </div>
        )}
        <button
          className="generate-btn"
          type="button"
          disabled={!hasFile || running || starting || !backendOk}
          onClick={onGenerate}
        >
          <span className="gen-shine" aria-hidden />
          <span className="gen-label">{buttonLabel}</span>
          {!(running || starting) && (
            <svg viewBox="0 0 20 20" fill="none" aria-hidden>
              <path d="M4 10h11M11 6l4 4-4 4" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          )}
        </button>
        {running && (
          <button className="cancel-btn" type="button" onClick={onCancel}>
            取消任务
          </button>
        )}
        {!backendOk && <p className="foot-hint">后端未启动：python3 server/app.py</p>}
      </footer>
    </aside>
  )
}
