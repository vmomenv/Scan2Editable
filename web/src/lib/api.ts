import type { JobOptions, JobSnapshot, UploadResult } from '../types'

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  const text = await res.text()
  let data: unknown = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = null
  }
  if (!res.ok) {
    let message = `请求失败（HTTP ${res.status}）`
    if (data && typeof data === 'object' && 'error' in data) {
      const detail = (data as { error?: unknown }).error
      if (detail) message = String(detail)
    }
    throw new Error(message)
  }
  return data as T
}

export function checkHealth() {
  return request<{ ok: boolean; script: boolean }>('/api/health', { cache: 'no-store' })
}

export function uploadPdf(file: File) {
  return request<UploadResult>(`/api/upload?name=${encodeURIComponent(file.name)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/pdf' },
    body: file,
  })
}

export function createJob(fileId: string, totalPages: number, options: JobOptions) {
  return request<{ jobId: string }>('/api/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ fileId, totalPages, options }),
  })
}

export function getJob(jobId: string) {
  return request<JobSnapshot>(`/api/jobs/${jobId}`, { cache: 'no-store' })
}

export function cancelJob(jobId: string) {
  return request<{ ok: boolean }>(`/api/jobs/${jobId}/cancel`, { method: 'POST' })
}
