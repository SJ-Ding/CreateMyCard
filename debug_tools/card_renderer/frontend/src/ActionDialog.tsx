import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { ComponentNode } from './parser';

export function ActionDialog({ action, component, onClose }: {
  action: unknown; component: ComponentNode; onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [copyStatus, setCopyStatus] = useState('');
  const value = action as { functionCall?: { call?: string; args?: unknown }; event?: unknown };
  const name = value?.functionCall?.call ?? '提交事件';
  const parameters = value?.functionCall?.args ?? value?.event ?? action;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const element = dialog.current;
    element?.showModal?.();
    element?.querySelector<HTMLButtonElement>('button')?.focus();
    return () => { element?.close?.(); previous?.focus(); };
  }, []);
  return createPortal(<dialog ref={dialog} open={typeof HTMLDialogElement.prototype.showModal !== 'function'}
    className="renderer-action-dialog" aria-label="点击事件详情" onCancel={(event) => {
      event.preventDefault(); onClose();
    }} onKeyDown={(event) => {
      if (event.key === 'Escape') { event.preventDefault(); onClose(); }
      if (event.key !== 'Tab') return;
      const buttons = Array.from(dialog.current?.querySelectorAll<HTMLButtonElement>('button') ?? []);
      const first = buttons[0]; const last = buttons[buttons.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }}>
    <header><strong>点击事件详情</strong><button type="button" onClick={onClose}>关闭</button></header>
    <dl><dt>函数名</dt><dd>{name}</dd><dt>来源组件</dt><dd>{component.type} · {component.id}</dd></dl>
    <strong>参数内容</strong><pre>{JSON.stringify(parameters, null, 2)}</pre>
    <footer><span>仅展示事件，不执行外部跳转。</span><button type="button" onClick={() => {
      if (!navigator.clipboard) { setCopyStatus('当前浏览器不支持复制，请手动复制'); return; }
      void navigator.clipboard.writeText(JSON.stringify({ action, componentId: component.id }, null, 2))
        .then(() => setCopyStatus('已复制')).catch(() => setCopyStatus('复制失败，请手动复制'));
    }}>复制</button><span role="status">{copyStatus}</span></footer>
  </dialog>, document.body);
}
