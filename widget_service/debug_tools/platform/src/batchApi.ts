export type BatchDatasetItem = {
  id: string;
  fileName: string;
  title: string;
  query: string;
  size: string;
  valid: boolean;
  error: string;
};

export type BatchSampleSummary = {
  id: string;
  fileName: string;
  title: string;
  query: string;
  size: string;
  sequence: number;
  uid?: string;
  status: string;
  attemptCount: number;
  elapsedMs: number;
  errorCode: string;
  error: string;
  artifactAvailable: boolean;
  dslAvailable: boolean;
  traceStatus: string;
  traceRecordCount: number;
};

export type BatchRun = {
  runId: string;
  status: string;
  startedAt: string;
  finishedAt?: string;
  endpoint: string;
  concurrency: number;
  maxRetries: number;
  total: number;
  completed: number;
  success: number;
  degraded: number;
  failed: number;
  cancelled: number;
  averageElapsedMs: number;
  traceWarnings: number;
  samples: BatchSampleSummary[];
};

export type BatchAttempt = {
  name: string;
  request?: Record<string, unknown>;
  response?: Record<string, unknown>;
  result?: Record<string, unknown>;
  blocks?: Record<string, unknown>;
  trace?: Record<string, unknown>;
  genui?: string;
};

export type BatchSampleDetail = {
  summary: BatchSampleSummary & { finalAttempt?: number };
  attempts: BatchAttempt[];
};

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body && typeof body.detail === 'string' ? body.detail : `请求失败（${response.status}）`;
    throw new Error(detail);
  }
  return body as T;
}

export async function listBatchDatasets(): Promise<BatchDatasetItem[]> {
  const body = await requestJson<{ items: BatchDatasetItem[] }>('/debug/batch/datasets');
  return body.items;
}

export async function listBatchRuns(): Promise<BatchRun[]> {
  const body = await requestJson<{ items: BatchRun[] }>('/debug/batch/runs');
  return body.items;
}

export function getBatchRun(runId: string): Promise<BatchRun> {
  return requestJson(`/debug/batch/runs/${encodeURIComponent(runId)}`);
}

export function getBatchSample(runId: string, sampleId: string): Promise<BatchSampleDetail> {
  return requestJson(
    `/debug/batch/runs/${encodeURIComponent(runId)}/samples/${encodeURIComponent(sampleId)}`,
  );
}

export function startBatchRun(input: {
  sampleIds: string[];
  toolWsBaseUrl: string;
  concurrency: number;
  maxRetries: number;
}): Promise<BatchRun> {
  return requestJson('/debug/batch/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
}

export function cancelBatchRun(runId: string): Promise<BatchRun> {
  return requestJson(`/debug/batch/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST' });
}
