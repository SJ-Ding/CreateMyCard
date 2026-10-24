import { afterEach, describe, expect, it, vi } from 'vitest';
import { loadSnapshot, openRendererSnapshot, saveSnapshot, type RendererSnapshot } from '../../../platform/src/rendererSnapshot';

afterEach(() => vi.unstubAllGlobals());

function storage(initial: RendererSnapshot[] = [], failWrite = false) {
  const values = new Map(initial.map(item => [item.id, item]));
  const close = vi.fn();
  const db = {
    close,
    transaction: (_name: string, mode: string) => {
      const transaction = { oncomplete: null as (() => void) | null,
        onerror: null as (() => void) | null, onabort: null as (() => void) | null,
        objectStore: () => ({
          put: (value: RendererSnapshot) => { if (!failWrite) values.set(value.id, structuredClone(value)); },
          get: (id: string) => {
            const request = { result: values.get(id), onsuccess: null as (() => void) | null };
            queueMicrotask(() => request.onsuccess?.()); return request;
          },
          openCursor: () => {
            const entries = [...values.values()]; let index = 0;
            const request = { result: null as unknown, onsuccess: null as (() => void) | null };
            const advance = () => {
              const item = entries[index++];
              request.result = item ? { value: item, delete: () => values.delete(item.id),
                continue: () => queueMicrotask(advance) } : null;
              request.onsuccess?.();
              if (!item) queueMicrotask(() => failWrite ? transaction.onerror?.() : transaction.oncomplete?.());
            };
            queueMicrotask(advance); return request;
          },
        }),
      };
      if (mode === 'readonly') queueMicrotask(() => transaction.oncomplete?.());
      return transaction;
    },
  };
  vi.stubGlobal('indexedDB', { open: () => {
    const request = { result: db, onsuccess: null as (() => void) | null };
    queueMicrotask(() => request.onsuccess?.()); return request;
  } });
  return { values, close };
}

describe('独立卡片快照', () => {
  it('保存完整上下文并读取；不同标识互不覆盖', async () => {
    const { close } = storage();
    const first = { id: 'first', createdAt: Date.now(), source: 'first', artifact: { cardSpec: { title: '原始' } },
      request: { deviceInfo: { romVersion: '7.0' } }, view: { width: 280, height: 150, zoom: 160 } };
    await saveSnapshot(first);
    await saveSnapshot({ id: 'second', createdAt: Date.now(), source: 'second' });
    expect(await loadSnapshot('first')).toEqual(first);
    expect((await loadSnapshot('second')).source).toBe('second');
    expect(close).toHaveBeenCalledTimes(4);
  });
  it('写入时清理七天以前的快照，保留有效快照', async () => {
    const { values } = storage([{ id: 'old', source: 'old', createdAt: Date.now() - 8 * 86400000 }]);
    await saveSnapshot({ id: 'new', source: 'new', createdAt: Date.now() });
    expect(values.has('old')).toBe(false); expect(values.has('new')).toBe(true);
  });
  it.each([false, true])('缺失或过期快照给出可操作提示，过期=%s', async expired => {
    storage(expired ? [{ id: 'old', source: 'old', createdAt: 1 }] : []);
    await expect(loadSnapshot('old')).rejects.toThrow('请从原页面重新打开');
  });
  it('不完整快照不使用默认样例代替', async () => {
    storage([{ id: 'invalid', createdAt: Date.now() } as RendererSnapshot]);
    await expect(loadSnapshot('invalid')).rejects.toThrow('卡片快照格式不兼容');
  });
  it('弹窗阻止时不写入快照', async () => {
    const open = vi.fn().mockReturnValue(null); vi.stubGlobal('open', open);
    await expect(openRendererSnapshot({ source: 'card' })).rejects.toThrow('浏览器阻止');
  });
  it('保存失败关闭空白标签并报告原因', async () => {
    storage([], true);
    const tab = { close: vi.fn(), opener: {}, location: { replace: vi.fn() } };
    vi.stubGlobal('open', vi.fn().mockReturnValue(tab));
    await expect(openRendererSnapshot({ source: 'card' })).rejects.toThrow('保存卡片快照失败');
    expect(tab.close).toHaveBeenCalledOnce(); expect(tab.location.replace).not.toHaveBeenCalled();
  });
  it('新标签 URL 仅传快照标识并断开 opener', async () => {
    const { values } = storage();
    const tab = { close: vi.fn(), opener: {}, location: { replace: vi.fn() } };
    vi.stubGlobal('open', vi.fn().mockReturnValue(tab));
    await openRendererSnapshot({ source: 'private card source' });
    expect(tab.opener).toBeNull();
    expect(tab.location.replace.mock.calls[0][0]).toMatch(/^\/debug\/renderer\?snapshot=[\w-]+$/);
    expect([...values.values()][0].source).toBe('private card source');
  });
});
