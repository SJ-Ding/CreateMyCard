import type { PreviewSettings } from '@widget-debug/card-renderer';

export type RendererSnapshot = {
  id: string; createdAt: number; source: string; artifact?: unknown; request?: unknown;
  view?: Partial<PreviewSettings>;
};
const DATABASE = 'ai-widget-renderer-snapshots';
const STORE = 'snapshots';
const MAX_AGE = 7 * 24 * 60 * 60 * 1000;

function database(): Promise<IDBDatabase> {
  if (typeof indexedDB === 'undefined') return Promise.reject(new Error('浏览器不支持卡片快照存储'));
  return new Promise((resolve, reject) => {
    let blocked = false;
    const request = indexedDB.open(DATABASE, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE, { keyPath: 'id' });
    request.onsuccess = () => {
      if (blocked) request.result.close(); else resolve(request.result);
    };
    request.onerror = () => reject(new Error('浏览器无法打开卡片快照存储'));
    request.onblocked = () => {
      blocked = true; reject(new Error('快照存储被其他标签页阻塞，请关闭旧标签页后重试'));
    };
  });
}

export async function saveSnapshot(snapshot: RendererSnapshot): Promise<void> {
  const db = await database();
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = db.transaction(STORE, 'readwrite');
      const store = transaction.objectStore(STORE);
      store.put(snapshot);
      const cursor = store.openCursor();
      cursor.onsuccess = () => {
        const current = cursor.result;
        if (!current) return;
        if ((current.value as RendererSnapshot).createdAt < Date.now() - MAX_AGE) current.delete();
        current.continue();
      };
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(new Error('保存卡片快照失败，请检查浏览器存储空间'));
      transaction.onabort = () => reject(new Error('卡片快照保存已中止'));
    });
  } finally { db.close(); }
}

export async function loadSnapshot(id: string): Promise<RendererSnapshot> {
  const db = await database();
  try {
    return await new Promise<RendererSnapshot>((resolve, reject) => {
      const request = db.transaction(STORE, 'readonly').objectStore(STORE).get(id);
      request.onsuccess = () => {
        const snapshot = request.result as RendererSnapshot | undefined;
        if (!snapshot || snapshot.createdAt < Date.now() - MAX_AGE) {
          reject(new Error('卡片快照不存在或已过期，请从原页面重新打开')); return;
        }
        if (snapshot.id !== id || typeof snapshot.source !== 'string' || !Number.isFinite(snapshot.createdAt)) {
          reject(new Error('卡片快照格式不兼容，请从原页面重新打开')); return;
        }
        resolve(snapshot);
      };
      request.onerror = () => reject(new Error('读取卡片快照失败'));
    });
  } finally { db.close(); }
}

export async function openRendererSnapshot(input: Omit<RendererSnapshot, 'id' | 'createdAt'>): Promise<void> {
  // 保留直接用户手势，异步写入完成前先打开空白标签，避免被弹窗策略拦截。
  const tab = window.open('about:blank', '_blank');
  if (!tab) throw new Error('浏览器阻止了新标签页，请允许弹出窗口后重试');
  tab.opener = null;
  try {
    const snapshot = { ...input, id: crypto.randomUUID(), createdAt: Date.now() };
    await saveSnapshot(snapshot);
    tab.location.replace(`/debug/renderer?snapshot=${encodeURIComponent(snapshot.id)}`);
  } catch (reason) {
    tab.close(); throw reason;
  }
}
