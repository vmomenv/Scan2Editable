export interface UploadResult {
  fileId: string
  name: string
  size: number
}

export type LogLevel = 'info' | 'warn' | 'error' | 'ok' | 'step'

export interface JobLog {
  t: number
  level: LogLevel
  text: string
}

export interface JobOutput {
  name: string
  size: number
  url: string
}

export type JobStatus = 'queued' | 'running' | 'success' | 'error' | 'canceled'

export interface JobSnapshot {
  id: string
  name: string
  status: JobStatus
  progress: number
  currentPage: number
  totalPages: number
  createdAt: number
  startedAt: number | null
  finishedAt: number | null
  error: string | null
  logs: JobLog[]
  outputs: {
    md: JobOutput | null
    docx: JobOutput | null
  }
  options: JobOptions
}

export interface JobOptions {
  title: string
  provider: string
  model: string
  baseUrl: string
  apiKey: string
  pages: string
  dpi: number
  refine: number
  workers: number
  trim: boolean
  verify: boolean
  debug: boolean
  keepPages: boolean
  noCache: boolean
  exportDocx: boolean
}

export const DEFAULT_OPTIONS: JobOptions = {
  title: '',
  provider: 'deepseek',
  model: '',
  baseUrl: '',
  apiKey: '',
  pages: '',
  dpi: 300,
  refine: 3,
  workers: 6,
  trim: true,
  verify: true,
  debug: false,
  keepPages: false,
  noCache: false,
  exportDocx: true,
}

export const PROVIDERS: Array<{
  id: string
  label: string
  model: string
  baseUrl: string
}> = [
  { id: 'deepseek', label: 'DeepSeek', model: 'deepseek-flash', baseUrl: 'https://api.deepseek.com/v1' },
  { id: 'openai', label: 'OpenAI', model: 'gpt-4o', baseUrl: 'https://api.openai.com/v1' },
  { id: 'dashscope', label: '通义千问', model: 'qwen-vl-max-latest', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
  { id: 'zhipu', label: '智谱 GLM', model: 'glm-4v-plus', baseUrl: 'https://open.bigmodel.cn/api/paas/v4' },
  { id: 'moonshot', label: 'Moonshot', model: 'moonshot-v1-8k-vision-preview', baseUrl: 'https://api.moonshot.cn/v1' },
  { id: 'gemini', label: 'Gemini', model: 'gemini-2.0-flash', baseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai' },
  { id: 'anthropic', label: 'Anthropic', model: 'claude-sonnet-4-20250514', baseUrl: 'https://api.anthropic.com' },
  { id: 'openai-compatible', label: '自定义 / 本地', model: '', baseUrl: 'http://localhost:8000/v1' },
]
