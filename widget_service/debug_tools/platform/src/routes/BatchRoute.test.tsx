import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { WorkbenchProvider } from '../context';
import { BatchRoute } from './BatchRoute';

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

describe('BatchRoute', () => {
  afterEach(() => vi.restoreAllMocks());

  it('loads datasets and starts a selected batch run', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === '/debug/batch/datasets') {
        return response({
          items: [
            { id: 'Q001', fileName: 'Q001.json', title: '天气', query: '生成天气卡片', size: '2x2', valid: true, error: '' },
            { id: 'Q002', fileName: 'Q002.json', title: '损坏', query: '', size: '', valid: false, error: 'invalid' },
          ],
        });
      }
      if (url === '/debug/batch/runs' && init?.method === 'POST') {
        return response({
          runId: 'batch_test', status: 'queued', startedAt: '2026-09-29T00:00:00Z',
          endpoint: 'ws://127.0.0.1:8855/api/v1/ws/tools/generateWidgetCardCompactDsl',
          concurrency: 4, maxRetries: 1, total: 1, completed: 0, success: 0,
          degraded: 0, failed: 0, cancelled: 0, averageElapsedMs: 0, traceWarnings: 0,
          samples: [],
        }, 202);
      }
      if (url === '/debug/batch/runs') return response({ items: [] });
      if (url === '/debug/batch/runs/batch_test') {
        return response({
          runId: 'batch_test', status: 'completed', startedAt: '2026-09-29T00:00:00Z',
          endpoint: 'ws://127.0.0.1:8855/api/v1/ws/tools/generateWidgetCardCompactDsl',
          concurrency: 4, maxRetries: 1, total: 1, completed: 1, success: 1,
          degraded: 0, failed: 0, cancelled: 0, averageElapsedMs: 10, traceWarnings: 0,
          samples: [],
        });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    render(<WorkbenchProvider><BatchRoute /></WorkbenchProvider>);

    expect(await screen.findByText('Q001 · 天气')).toBeInTheDocument();
    expect(screen.getByText('Q002 · 损坏')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: '启动批跑' }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/debug/batch/runs',
      expect.objectContaining({ method: 'POST' }),
    ));
    const startCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST');
    const body = JSON.parse(String(startCall?.[1]?.body));
    expect(body.sampleIds).toEqual(['Q001']);
    expect(body.concurrency).toBe(4);
    expect(body.maxRetries).toBe(1);
  });
});
