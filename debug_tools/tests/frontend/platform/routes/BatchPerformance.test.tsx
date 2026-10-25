import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { pollBatchRun, type BatchRun } from '../../../../platform/src/batchApi';
import { BatchRoute } from '../../../../platform/src/routes/BatchRoute';

vi.mock('@widget-debug/card-renderer', () => ({
  CardRenderer: () => null, resolveCardSize: () => '2x2', resolveAppVersion: () => undefined,
}));

const sample = {
  id: 'Q001', sequence: 1, status: 'success', traceStatus: 'complete', attemptCount: 1,
  revision: 2, title: '天气', elapsedMs: 10,
};
const run = {
  runId: 'run_001', status: 'running', revision: 2, total: 2, success: 1, degraded: 0,
  samples: [sample, { ...sample, id: 'Q002', sequence: 2 }],
} as BatchRun;

function response(body: unknown, status = 200, etag = '"version"'): Response {
  return { ok: status === 200, status, json: async () => body, headers: new Headers({ ETag: etag }) } as Response;
}

function renderRoute() {
  return render(<MemoryRouter initialEntries={['/batch/tasks/task_1']}>
    <Routes><Route path="/batch/tasks/:taskId" element={<BatchRoute />} /></Routes>
  </MemoryRouter>);
}

describe('batch incremental performance', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); });

  it('preserves unchanged samples and handles 304 without parsing a body', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(response({
      revision: 3, sampleChanges: [{ ...run.samples[1], revision: 3, elapsedMs: 20 }],
    }));
    const signal = new AbortController().signal;
    const updated = await pollBatchRun(run, signal);
    expect(updated.samples[0]).toBe(run.samples[0]);
    expect(updated.samples[1].elapsedMs).toBe(20);
    const empty = response(null, 304);
    const json = vi.spyOn(empty, 'json');
    fetch.mockResolvedValueOnce(empty);
    expect(await pollBatchRun(updated, signal)).toBe(updated);
    expect(json).not.toHaveBeenCalled();
    expect(fetch.mock.calls[1][1]?.headers).toMatchObject({ 'If-None-Match': '"version"' });
  });

  it('does not overlap slow polling or reload an unchanged selected sample', async () => {
    vi.useFakeTimers();
    let finish: (value: Response) => void = () => undefined;
    let pollCount = 0;
    let sampleCount = 0;
    let taskCount = 0;
    let pollSignal: AbortSignal | null = null;
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith('/postprocess/plugins')) return response({ items: [] });
      if (url.endsWith('/tasks/task_1')) {
        taskCount += 1;
        return response({ name: '性能回归', runs: [{ runId: 'run_001', status: 'running' }] });
      }
      if (url.endsWith('/samples/Q001')) {
        sampleCount += 1;
        return response({ summary: sample, attempts: [] });
      }
      if ((init?.headers as Record<string, string>)?.['X-Batch-View'] === 'progress') {
        pollCount += 1;
        pollSignal = init?.signal as AbortSignal;
        return new Promise<Response>((resolve) => { finish = resolve; });
      }
      return response(run);
    });
    const view = renderRoute();
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    fireEvent.click(screen.getByText('Q001'));
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(sampleCount).toBe(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(pollCount).toBe(1);
    await act(async () => {
      finish(response({ revision: 3, sampleChanges: [{ ...run.samples[1], revision: 3 }] }));
    });
    expect(sampleCount).toBe(1);
    expect(taskCount).toBe(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(800); });
    expect(pollCount).toBe(2);
    view.unmount();
    expect((pollSignal as AbortSignal | null)?.aborted).toBe(true);
  });

  it('loads and selects samples beyond the first server page', async () => {
    const later = { ...sample, id: 'Q101', sequence: 101 };
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith('/postprocess/plugins')) return response({ items: [] });
      if (url.endsWith('/tasks/task_1')) return response({
        name: '分页回归', runs: [{ runId: 'run_001', status: 'completed' }],
      });
      if (url.includes('/samples?')) {
        const offset = new URL(url, 'http://localhost').searchParams.get('offset');
        return response({ items: offset === '100' ? [later] : [sample], total: 101 });
      }
      if (url.endsWith('/samples/Q101')) return response({ summary: later, attempts: [] });
      return response({ ...run, status: 'completed', storageVersion: 2, total: 101 });
    });
    renderRoute();
    expect(await screen.findByText('Q001')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    fireEvent.click(await screen.findByText('Q101'));
    expect(await screen.findByRole('link', { name: 'Trace' })).toBeInTheDocument();
    expect(screen.queryByText('Q001')).not.toBeInTheDocument();
  });
});
