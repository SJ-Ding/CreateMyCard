import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { WorkbenchProvider } from '../../../../platform/src/context';
import { RendererRoute } from '../../../../platform/src/routes/RendererRoute';

const CALL_HISTORY_STORAGE_KEY = 'ai-widget-debug-call-history:v1';
const ARTIFACT_URL = 'https://obs.todo.local/widget/artifact-test.md';
const RESULT_ENVELOPE = "type='result' tool='generateWidgetCardCompactDsl' "
  + "operation='generateWidgetCardCompactDsl' requestId='session&call-1' "
  + `data={'status': 'success', 'artifactUrl': '${ARTIFACT_URL}'} `
  + "status='success' errorCode='' error={}";

describe('RendererRoute Compact DSL history', () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    window.sessionStorage.setItem(CALL_HISTORY_STORAGE_KEY, JSON.stringify({
      selectedCallId: 'history-call-1',
      calls: [
        {
          id: 'history-call-1',
          operation: 'generateWidgetCardCompactDsl',
          source: 'interface',
          status: 'success',
          startedAt: '2026-09-28T00:00:00.000Z',
          finishedAt: '2026-09-28T00:00:01.000Z',
          request: {},
          response: {
            operation: 'generateWidgetCardCompactDsl',
            status: 'success',
            data: { artifactUrl: ARTIFACT_URL, artifactDigest: 'sha256:test' },
          },
          finalStreamContent: RESULT_ENVELOPE,
        },
      ],
    }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('loads artifact GenUI instead of placing the result envelope in the editor', async () => {
    const genui = [
      '{"version":"v0.9","createSurface":{"surfaceId":"surface_card"}}',
      '{"version":"v0.9","updateComponents":{"surfaceId":"surface_card","root":"root","components":[{"id":"root","component":"Text","content":"宽卡"}]}}',
    ].join('\n');
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        runId: 'renderer',
        artifactUrl: ARTIFACT_URL,
        artifactDigest: 'sha256:test',
        genui,
        cardSpec: { suggestSize: '2x4' },
        taskSpec: { appVersion: '12.0.0.1' },
      }),
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<WorkbenchProvider><RendererRoute /></WorkbenchProvider>);

    const editor = screen.getByRole('textbox', { name: 'DSL 输入' });
    expect(editor).toHaveValue('');
    expect(editor).not.toHaveValue(RESULT_ENVELOPE);
    await waitFor(() => expect(editor).toHaveValue(genui));
    expect(screen.getByRole('textbox', { name: '客户端版本' })).toHaveValue('12.0.0.1');
    await waitFor(() => expect(screen.getByText(/300 × 150/)).toBeInTheDocument());
    expect(screen.getByText('artifact 已下载')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      `/debug/artifact?url=${encodeURIComponent(ARTIFACT_URL)}&digest=sha256%3Atest`,
    );
  });
  it('手工导入完整产物后同步右侧信息，编辑器保留完整输入', async () => {
    const genui = [
      { version: 'v0.9', createSurface: { surfaceId: 'import' } },
      { version: 'v0.9', updateComponents: { surfaceId: 'import', root: 'root', components: [
        { id: 'root', component: 'Text', content: '新导入卡片' },
      ] } },
    ].map(row => JSON.stringify(row)).join('\n');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ genui, runId: 'renderer' }) }));
    render(<WorkbenchProvider><RendererRoute /></WorkbenchProvider>);
    const editor = screen.getByRole('textbox', { name: 'DSL 输入' });
    await waitFor(() => expect(editor).toHaveValue(genui));
    const imported = JSON.stringify({ genui, cardSpec: { title: '导入标题', suggestSize: '2x2', dataBindings: [] },
      meta: { capabilityRegistryVersion: 'original-version' } });
    fireEvent.change(editor, { target: { value: imported } });
    fireEvent.click(screen.getByRole('tab', { name: '产物信息' }));
    fireEvent.click(screen.getByRole('tab', { name: 'cardSpec' }));
    await waitFor(() => expect(screen.getByText(/导入标题/, { selector: 'pre' })).toBeInTheDocument());
    expect(editor).toHaveValue(imported);
    await screen.findByText('新导入卡片');
    expect(editor).toHaveValue(imported);
  });
});
