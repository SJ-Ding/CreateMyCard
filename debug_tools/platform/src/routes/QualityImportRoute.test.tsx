import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { QualityImportRoute, readImportFolder } from './QualityImportRoute';

function file(path: string, content: string, type = 'application/json') {
  const value = new File([content], path.split('/').at(-1)!, { type });
  Object.defineProperties(value, {
    webkitRelativePath: { value: `dataset/${path}` },
    text: { value: async () => content },
    arrayBuffer: { value: async () => new TextEncoder().encode(content).buffer },
  });
  return value;
}

describe('QualityImportRoute', () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it('accepts the current manifest and preserves source checksums and batch identity', async () => {
    const samples = await readImportFolder([
      file('quality-import.json', JSON.stringify({ samples: [{ id: 'Astra__Q001', query: '天气', size: '2x2',
        dsl: 'dsl/a.genui.jsonl', image: 'images/a.png',
        sourceSha256: 'a'.repeat(64), imageSha256: 'b'.repeat(64),
      }] })), file('dsl/a.genui.jsonl', '\ufeff[]\r\n'), file('images/a.png', 'test-png', 'image/png'),
    ]);
    expect(samples[0]).toMatchObject({ id: 'Astra__Q001', query: '天气', dsl: '\ufeff[]\r\n',
      sourceSha256: 'a'.repeat(64), imageSha256: 'b'.repeat(64) });
    expect(samples[0].imageBase64).toBe(btoa('test-png'));
  });

  it('rejects missing images, path escapes and unsupported manifests without dropping samples', async () => {
    const row = { id: 'a', query: '天气', size: '2x2', dsl: 'a.jsonl', image: 'a.png' };
    await expect(readImportFolder([file('quality-import.json', JSON.stringify({ samples: [row] })), file('a.jsonl', '[]')])).rejects.toThrow('缺少文件');
    await expect(readImportFolder([file('quality-import.json', JSON.stringify({ samples: [{ ...row, dsl: '../a' }] }))])).rejects.toThrow('相对路径');
    await expect(readImportFolder([file('dataset-manifest.json', '{}')])).rejects.toThrow('quality-import.json');
  });

  it('does nothing on entry; importing one card saves inputs and navigates without starting scoring', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: true, json: async () => ({ runId: 'imported' }) } as Response);
    render(<MemoryRouter initialEntries={['/quality/import']}><Routes>
      <Route path="/quality/import" element={<QualityImportRoute />} />
      <Route path="/quality" element={<p>选择评分样本</p>} />
    </Routes></MemoryRouter>);
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText('Query'), { target: { value: '天气' } });
    fireEvent.change(screen.getByLabelText('DSL 文件'), { target: { files: [file('card.jsonl', '[]')] } });
    fireEvent.change(screen.getByLabelText('截图文件'), { target: { files: [file('card.png', 'pixels', 'image/png')] } });
    // jsdom 的 file change 不更新原生 value；真实浏览器上传另做联调。
    fireEvent.submit(screen.getByRole('button', { name: '导入并选择评分样本' }).closest('form')!);
    await screen.findByText('选择评分样本');
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/debug/batch/quality/import');
    expect(JSON.parse(String(init?.body)).samples[0]).toMatchObject({ id: 'Q001', query: '天气', dsl: '[]' });
  });
});
