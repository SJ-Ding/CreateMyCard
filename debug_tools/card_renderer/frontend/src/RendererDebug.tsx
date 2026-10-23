import { createContext, useContext, useEffect, useRef, useState,
  type Dispatch, type ReactNode, type SetStateAction } from 'react';
import type { RendererDocument } from './parser';
import { MockDataPanel } from './MockDataPanel';
import type { DataUpdate } from './mock-data';

type Bounds = { x: number; y: number; width: number; height: number };
type Instance = { id: string; index: number; key: string; bounds: Bounds | null; parent: string | null };
type DebugState = {
  document: RendererDocument | null; setDocument: (value: RendererDocument | null) => void;
  enabled: boolean; setEnabled: (value: boolean) => void;
  instances: Instance[]; setInstances: (value: Instance[]) => void;
  selected: string | null; select: (value: string | null) => void;
  hovered: string | null; hover: (value: string | null) => void;
  updates: DataUpdate[]; update: Dispatch<SetStateAction<DataUpdate[]>>;
  artifact?: unknown; request?: unknown; appVersion?: string;
  source: string; setSource: (source: string) => void;
  setPreviewVersion: (version: string) => void;
};
const Context = createContext<DebugState | null>(null);
export const useRendererDebug = () => useContext(Context);

export function RendererDebugProvider({ children, artifact, request, appVersion }: {
  children: ReactNode; artifact?: unknown; request?: unknown; appVersion?: string;
}) {
  const [document, setDocument] = useState<RendererDocument | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [instances, setInstances] = useState<Instance[]>([]);
  const [selected, select] = useState<string | null>(null);
  const [hovered, hover] = useState<string | null>(null);
  const [updates, update] = useState<DataUpdate[]>([]);
  const [source, setSource] = useState('');
  const [previewVersion, setPreviewVersion] = useState(appVersion);
  useEffect(() => { select(null); hover(null); update([]); }, [document]);
  return <Context.Provider value={{ document, setDocument, enabled, setEnabled, instances, setInstances,
    selected, select, hovered, hover, updates, update, artifact, request, appVersion: previewVersion,
    setPreviewVersion, source, setSource }}>{children}</Context.Provider>;
}

/** 外层仅包住整张卡片，组件本身的布局与 DOM 不变。 */
export function InspectionCanvas({ children }: { children: ReactNode }) {
  const debug = useRendererDebug();
  const root = useRef<HTMLDivElement>(null);
  const elements = useRef(new Map<Element, string>());
  const previous = useRef('');
  const [hoverBounds, setHoverBounds] = useState<Bounds | null>(null);
  const setInstances = debug?.setInstances;
  useEffect(() => {
    const container = root.current;
    if (!container || !setInstances) return;
    let frame = 0;
    const measure = () => {
      const surface = container.querySelector<HTMLElement>('.card-renderer__surface');
      if (!surface) return;
      const origin = surface.getBoundingClientRect();
      const scaleX = origin.width / surface.offsetWidth || 1;
      const scaleY = origin.height / surface.offsetHeight || 1;
      const counts = new Map<string, number>();
      const lookup = new Map<Element, string>();
      const items: Instance[] = [];
      for (const element of surface.querySelectorAll<HTMLElement>('[data-node-id]')) {
        const id = element.dataset.nodeId ?? '';
        const index = counts.get(id) ?? 0;
        counts.set(id, index + 1);
        const key = `${id}::${index}`;
        lookup.set(element, key);
        const rect = element.getBoundingClientRect();
        const visible = rect.width > 0 && rect.height > 0 && getComputedStyle(element).visibility !== 'hidden';
        const parent = element.parentElement?.closest('[data-node-id]');
        items.push({ id, index, key, parent: parent ? lookup.get(parent) ?? null : null,
          bounds: visible ? { x: (rect.left - origin.left) / scaleX, y: (rect.top - origin.top) / scaleY,
            width: rect.width / scaleX, height: rect.height / scaleY } : null });
      }
      elements.current = lookup;
      const serialized = JSON.stringify(items);
      if (serialized !== previous.current) { previous.current = serialized; setInstances(items); }
    };
    const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(measure); };
    const observer = new MutationObserver(schedule);
    observer.observe(container, { subtree: true, childList: true, attributes: true, characterData: true });
    const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule);
    resize?.observe(container);
    container.addEventListener('load', schedule, true);
    window.addEventListener('resize', schedule);
    window.addEventListener('scroll', schedule, true);
    document.fonts?.ready.then(schedule);
    schedule();
    return () => {
      cancelAnimationFrame(frame); observer.disconnect(); resize?.disconnect();
      container.removeEventListener('load', schedule, true);
      window.removeEventListener('resize', schedule); window.removeEventListener('scroll', schedule, true);
    };
  }, [debug?.document, debug?.updates, setInstances]);
  if (!debug) return <>{children}</>;
  const active = debug.instances.find(item => item.key === (debug.hovered ?? debug.selected));
  const bounds = active?.bounds ?? (debug.hovered ? hoverBounds : null);
  const identify = (target: EventTarget | null) => {
    if (!(target instanceof Element)) return null;
    const element = target.closest('[data-node-id]');
    if (!element || !root.current?.contains(element)) return null;
    return elements.current.get(element) ?? null;
  };
  return <div ref={root} className={`renderer-inspection-canvas${debug.enabled ? ' is-inspecting' : ''}`}
    onPointerMoveCapture={event => {
      if (!debug.enabled) return;
      const key = identify(event.target); debug.hover(key);
      setHoverBounds(debug.instances.find(item => item.key === key)?.bounds ?? null);
    }} onPointerLeave={() => { debug.hover(null); setHoverBounds(null); }}
    onPointerDownCapture={event => {
      if (!debug.enabled) return;
      event.preventDefault(); event.stopPropagation(); debug.select(identify(event.target));
    }}
    onClickCapture={event => {
      if (!debug.enabled || !root.current?.contains(event.target as Node)) return;
      event.preventDefault(); event.stopPropagation(); debug.select(identify(event.target));
    }} onKeyDownCapture={event => {
      if (debug.enabled && (event.key === 'Enter' || event.key === ' ')) {
        event.preventDefault(); event.stopPropagation(); debug.select(identify(event.target));
      }
    }}>
    {children}
    {debug.enabled && bounds && <div className="renderer-component-outline" aria-hidden="true" style={{
      left: bounds.x, top: bounds.y, width: bounds.width, height: bounds.height,
    }} />}
  </div>;
}

function ComponentTree() {
  const debug = useRendererDebug()!;
  const [collapsed, collapse] = useState<Set<string>>(new Set());
  const treeRoot = useRef<HTMLDivElement>(null);
  const document = debug.document;
  const focused = debug.instances.find(item => item.key === (debug.hovered ?? debug.selected));
  const selectedId = focused?.id ?? debug.selected?.replace(/::(?:definition|\d+)$/, '');
  useEffect(() => {
    collapse(new Set());
    treeRoot.current?.querySelector('[aria-selected="true"]')?.scrollIntoView?.({ block: 'nearest' });
  }, [debug.selected, debug.document]);
  const renderedIds = new Set<string>();
  const markReachable = (id: string): void => {
    if (renderedIds.has(id)) return;
    renderedIds.add(id);
    const node = document?.graph.getNode(id);
    if (!node) return;
    node.children.forEach(markReachable);
    if (node.dynamicChildrenTemplate) markReachable(node.dynamicChildrenTemplate.componentId);
    for (const branch of ['childrenIf', 'childrenElse']) {
      const values = node.props[branch];
      if (Array.isArray(values)) for (const child of values) if (typeof child === 'string') markReachable(child);
    }
  };
  if (document) markReachable(document.rootId);
  const renderNode = (id: string, ancestry: Set<string>, instance?: Instance): ReactNode => {
    const node = document?.components.get(id);
    if (!node || ancestry.has(id)) return null;
    renderedIds.add(id);
    const key = instance?.key ?? `${id}::definition`;
    const next = new Set(ancestry).add(id);
    const graphNode = document?.graph.getNode(id);
    const children = [...node.children];
    if (graphNode?.dynamicChildrenTemplate) children.push(graphNode.dynamicChildrenTemplate.componentId);
    for (const branch of ['childrenIf', 'childrenElse']) {
      const ids = node.props[branch];
      if (Array.isArray(ids)) for (const child of ids) if (typeof child === 'string') children.push(child);
    }
    const actualChildren = instance ? debug.instances.filter(item => item.parent === instance.key) : [];
    const declaredOnly = children.filter(child => !actualChildren.some(item => item.id === child));
    const isSelected = debug.selected === key;
    const hasChildren = children.length > 0 || actualChildren.length > 0;
    const isRepeated = instance && debug.instances.filter(item => item.id === id).length > 1;
    return <li key={key} role="treeitem" aria-selected={isSelected}
      aria-expanded={hasChildren ? !collapsed.has(key) : undefined}>
      <div className={`renderer-tree-row${isSelected ? ' is-selected' : ''}${debug.hovered === key ? ' is-hovered' : ''}`}>
        {hasChildren ? <button type="button" className="renderer-tree-toggle"
          aria-label={`展开或折叠 ${id}`} onClick={() => collapse(value => {
          const copy = new Set(value); if (copy.has(key)) copy.delete(key); else copy.add(key); return copy;
        })}><svg aria-hidden="true" viewBox="0 0 16 16" className={collapsed.has(key) ? '' : 'is-expanded'}>
          <path d="m6 4 4 4-4 4" /></svg></button> : <span className="renderer-tree-spacer" />}
        <button type="button" className="renderer-tree-select" onClick={() => { debug.setEnabled(true); debug.select(key); }}>
          <span className={`renderer-tree-type${hasChildren ? ' is-container' : ''}`}>{node.type}</span>{' '}
          <code>{id}</code>{isRepeated && <span className="renderer-tree-instance"> [{instance.index}]</span>}
          {!instance?.bounds && <small className="renderer-tree-hidden"> 无可见区域</small>}</button>
      </div>
      {!collapsed.has(key) && <ul role="group">
        {actualChildren.map(child => renderNode(child.id, next, child))}
        {declaredOnly.map(child => renderNode(child, next))}
      </ul>}
    </li>;
  };
  if (!document) return <p>渲染卡片后查看组件树。</p>;
  const roots = debug.instances.filter(item => item.parent === null);
  const rendered = roots.length ? roots.map(item => renderNode(item.id, new Set(), item))
    : renderNode(document.rootId, new Set());
  const detached = [...document.components.keys()].filter(id => !renderedIds.has(id));
  const node = selectedId ? document.components.get(selectedId) : null;
  return <div ref={treeRoot}>
    <div className="renderer-tree-heading"><strong>组件结构</strong><span>{document.components.size} 个组件</span></div>
    <p className="renderer-tree-hint">画布悬停定位，点击锁定；也可在树中选择。</p>
    <ul className="renderer-component-tree" role="tree" aria-label="组件结构树">{rendered}
      {detached.map(id => renderNode(id, new Set()))}</ul>
    {node && <section className="renderer-component-details" aria-label="组件详情"><h3>{node.type} · {node.id}</h3>
      {focused?.bounds ? <dl className="renderer-component-bounds">{Object.entries(focused.bounds).map(([key, value]) => (
        <div key={key}><dt>{key}</dt><dd>{Math.round(value * 10) / 10}</dd></div>
      ))}</dl> : <p>无可见区域</p>}
      <p className="renderer-tree-hint">相对卡片左上角，缩放前的画布坐标。</p>
      <pre>{JSON.stringify(node.props, null, 2)}</pre></section>}
  </div>;
}

export function RendererDebugPanel({ artifactPanel }: { artifactPanel: ReactNode }) {
  const [tab, setTab] = useState('components');
  // 各面板保留挂载，切换标签不会丢失模拟草稿或中断等待中的自动刷新。
  return <><div className="renderer-debug-tabs" role="tablist" aria-label="渲染检查">
    {[['components', '组件树'], ['mock', '模拟数据'], ['artifact', '产物信息']].map(([id, label]) => (
      <button type="button" role="tab" aria-selected={tab === id} key={id} onClick={() => setTab(id)}>{label}</button>
    ))}</div><div className="renderer-debug-panel">
    <div hidden={tab !== 'components'}><ComponentTree /></div>
    <div hidden={tab !== 'mock'}><MockDataPanel active={tab === 'mock'} /></div>
    <div hidden={tab !== 'artifact'}>{artifactPanel}</div>
  </div></>;
}
