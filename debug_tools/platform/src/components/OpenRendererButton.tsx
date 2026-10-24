import { useState } from 'react';
import { openRendererSnapshot, type RendererSnapshot } from '../rendererSnapshot';

export function OpenRendererButton({ snapshot }: { snapshot: Omit<RendererSnapshot, 'id' | 'createdAt'> }) {
  const [error, setError] = useState('');
  return <div className="open-renderer-action"><button type="button" onClick={() => {
    setError(''); void openRendererSnapshot(snapshot).catch(reason => setError(String(reason)));
  }}>在卡片渲染中打开 ↗</button>{error && <p role="alert">{error}；请再次点击重试。</p>}</div>;
}
