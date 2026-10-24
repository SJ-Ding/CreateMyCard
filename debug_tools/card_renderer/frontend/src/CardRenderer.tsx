import React, { useEffect, useMemo, useRef, useState } from 'react';
import { CardPreview } from './render';
import { CardSize, ComponentNode, parseInput, RendererDocument } from './parser';
import { SAMPLE_COMPACT } from './fixtures';
import './styles.css';
import { InspectionCanvas, useRendererDebug } from './RendererDebug';

export type PreviewSettings = {
  source: string; cardSize: CardSize; appVersion?: string; width: number; height: number; zoom: number;
};

export interface CardRendererProps {
  /** JSONL/JSON/compact DSL source supplied by the host; manual edits remain local. */
  initialValue?: string;
  /** Base URL used for relative image resources. Defaults to same-origin /resources/. */
  assetBaseUrl?: string;
  /** Backend endpoint used for every Compact DSL preview. */
  conversionUrl?: string;
  /** Host-resolved card size, usually from the final CardSpec or request query. */
  cardSize?: CardSize;
  /** Original client version used by the Python converter's version gate. */
  appVersion?: string;
  /** Called after a successful parse, allowing the platform to publish the artifact. */
  onArtifact?: (document: RendererDocument) => void;
  /** Receives resolved local actions; the renderer never performs external navigation. */
  onAction?: (action: unknown, component: ComponentNode) => void;
  /** Hides source editing controls while keeping automatic rendering and preview controls. */
  previewOnly?: boolean;
  /** Controlled preview zoom percentage. */
  zoom?: number;
  /** Called when the preview zoom changes. */
  onZoomChange?: (zoom: number) => void;
  className?: string;
  debugTools?: boolean;
  initialView?: Partial<PreviewSettings>;
  onOpenPreview?: (settings: PreviewSettings) => Promise<void>;
  onSourceChange?: (source: string) => void;
}

const DEFAULT_SOURCE = SAMPLE_COMPACT;

function sourceValue(value: string | undefined): string {
  return typeof value === 'string' ? value : DEFAULT_SOURCE;
}

export function CardRenderer({ initialValue, assetBaseUrl = '/resources/', conversionUrl = '/debug/renderer/convert', cardSize: hostCardSize = 'auto', appVersion: hostAppVersion, onArtifact, onAction, previewOnly = false, zoom: controlledZoom, onZoomChange, className = '', debugTools = false, initialView, onOpenPreview, onSourceChange }: CardRendererProps) {
  const [source, setSource] = useState(() => sourceValue(initialValue));
  const [cardSize, setCardSize] = useState<CardSize>(hostCardSize);
  const [appVersion, setAppVersion] = useState(hostAppVersion ?? '');
  const [internalZoom, setInternalZoom] = useState(initialView?.zoom ?? 220);
  const [autoRender, setAutoRender] = useState(true);
  const [document, setDocument] = useState<RendererDocument | null>(null);
  const [containerWidth, setContainerWidth] = useState(150);
  const [containerHeight, setContainerHeight] = useState(150);
  const [error, setError] = useState('');
  const [openError, setOpenError] = useState('');
  const [converting, setConverting] = useState(false);
  const conversionRequest = useRef<AbortController | null>(null);
  const renderTimer = useRef<number | undefined>(undefined);
  const zoom = controlledZoom ?? internalZoom;
  const stageRef = useRef<HTMLDivElement | null>(null);
  const debug = useRendererDebug();
  const setDebugDocument = debugTools ? debug?.setDocument : undefined;
  const setDebugSource = debugTools ? debug?.setSource : undefined;
  const setPreviewVersion = debugTools ? debug?.setPreviewVersion : undefined;
  const restoredView = useRef(false);
  useEffect(() => { setDebugDocument?.(document); }, [document, setDebugDocument]);
  useEffect(() => { setDebugSource?.(source); }, [source, setDebugSource]);
  useEffect(() => { onSourceChange?.(source); }, [source, onSourceChange]);
  useEffect(() => { setPreviewVersion?.(appVersion); }, [appVersion, setPreviewVersion]);
  useEffect(() => () => setDebugDocument?.(null), [setDebugDocument]);

  const cancelConversion = () => {
    conversionRequest.current?.abort();
    conversionRequest.current = null;
    setConverting(false);
  };

  const changeContainerSize = (dimension: 'width' | 'height', value: string) => {
    window.clearTimeout(renderTimer.current);
    cancelConversion();
    const size = Math.max(1, Number(value));
    if (dimension === 'width') setContainerWidth(size);
    else setContainerHeight(size);
  };

  useEffect(() => () => { conversionRequest.current?.abort(); }, []);

  const changeZoom = (nextZoom: number) => {
    if (controlledZoom === undefined) setInternalZoom(nextZoom);
    onZoomChange?.(nextZoom);
  };

  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const handleWheel = (event: WheelEvent) => {
      if (event.deltaY === 0) return;
      event.preventDefault();
      const nextZoom = Math.max(50, Math.min(360, zoom + (event.deltaY < 0 ? 10 : -10)));
      if (nextZoom === zoom) return;
      if (controlledZoom === undefined) setInternalZoom(nextZoom);
      onZoomChange?.(nextZoom);
    };
    stage.addEventListener('wheel', handleWheel, { passive: false });
    return () => stage.removeEventListener('wheel', handleWheel);
  }, [zoom, controlledZoom, onZoomChange]);

  useEffect(() => {
    if (initialValue === undefined) return;
    cancelConversion();
    setSource(initialValue);
    // A selected shared call may keep the same component identity while its
    // final frame arrives. Clear the previous preview so manual-render mode
    // cannot display an unrelated call's document.
    setDocument(null);
    setError('');
    setOpenError('');
  }, [initialValue]);

  useEffect(() => {
    cancelConversion();
    setCardSize(hostCardSize);
  }, [hostCardSize]);

  useEffect(() => {
    cancelConversion();
    setAppVersion(hostAppVersion ?? '');
    setDocument(null);
  }, [hostAppVersion]);

  const render = async (nextSource = source, replaceSource = false) => {
    window.clearTimeout(renderTimer.current);
    cancelConversion();
    const text = nextSource.trim();
    if (!text) {
      setDocument(null);
      setError('');
      return;
    }
    const controller = new AbortController();
    conversionRequest.current = controller;
    setConverting(true);
    setDocument(null);
    setError('');
    try {
      const parsed = await parseInput(text, { cardSize, appVersion: appVersion || undefined, conversionUrl, signal: controller.signal });
      if (conversionRequest.current !== controller) return;
      if (replaceSource) {
        setSource(parsed.jsonl);
        setCardSize(parsed.surface.width > parsed.surface.height ? '2x4' : '2x2');
      }
      const displayed = replaceSource ? { ...parsed, mode: 'A2UI' as const } : parsed;
      setDocument(displayed);
      setContainerWidth(!restoredView.current && initialView?.width ? initialView.width : parsed.surface.width);
      setContainerHeight(!restoredView.current && initialView?.height ? initialView.height : parsed.surface.height);
      restoredView.current = true;
      setError('');
      onArtifact?.(displayed);
    } catch (reason) {
      if (conversionRequest.current !== controller) return;
      setDocument(null);
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (conversionRequest.current === controller) {
        conversionRequest.current = null;
        setConverting(false);
      }
    }
  };

  useEffect(() => {
    if (!autoRender) return;
    renderTimer.current = window.setTimeout(() => { void render(); }, 250);
    return () => {
      window.clearTimeout(renderTimer.current);
      conversionRequest.current?.abort();
      conversionRequest.current = null;
    };
  }, [source, cardSize, appVersion, autoRender, conversionUrl]);

  const status = useMemo(() => {
    if (converting) return '正在转换为 A2UI…';
    if (error) return error;
    if (!document) return source.trim() ? '未渲染' : '请输入 JSONL 或 DSL';
    const warningText = document.warnings.length > 0
      ? `，${document.warnings.length} 条警告`
      : '';
    return `已渲染 ${document.components.size} 个组件，DataModel ${document.dataPathCount} 个路径${warningText}。`;
  }, [converting, document, error, source]);

  const previewDocument = useMemo(() => document ? {
    ...document,
    surface: { width: containerWidth, height: containerHeight },
  } : null, [containerHeight, containerWidth, document]);

  return <section className={`card-renderer${previewOnly ? ' card-renderer--preview-only' : ''} ${className}`.trim()} aria-label="卡片生成结果渲染器">
    {!previewOnly && <div className="card-renderer__editor">
      <div className="card-renderer__toolbar">
        <button type="button" className="is-primary" onClick={() => { void render(); }}>渲染</button>
        <button type="button" disabled={converting || !source.trim()} onClick={() => { void render(source, true); }}>{converting ? '正在转换…' : 'Python 转换并渲染'}</button>
        <button type="button" onClick={() => { setSource(''); void render(''); }}>清空</button>
        <label className="card-renderer__control">画布<select value={cardSize} onChange={(event) => { cancelConversion(); setCardSize(event.target.value as CardSize); }}><option value="auto">自动</option><option value="2x2">2×2 · 150×150</option><option value="2x4">2×4 · 300×150</option></select></label>
        <label className="card-renderer__check"><input type="checkbox" checked={autoRender} onChange={(event) => { cancelConversion(); setAutoRender(event.target.checked); }} />自动渲染</label>
        <label className="card-renderer__control">客户端版本<input type="text" value={appVersion} placeholder="原请求版本" onChange={(event) => { cancelConversion(); setDocument(null); setAppVersion(event.target.value); }} /></label>
      </div>
      <textarea value={source} onChange={(event) => { cancelConversion(); setSource(event.target.value); }} spellCheck={false} aria-label="DSL 输入" />
      <div className={`card-renderer__status${error ? ' is-error' : ''}`} role={error ? 'alert' : 'status'}>{status}</div>
    </div>}
    <div className="card-renderer__preview">
      <div className="card-renderer__preview-toolbar">
        <span>{previewDocument ? `${previewDocument.mode} · ${containerWidth} × ${containerHeight}` : '预览'}</span>
        {onOpenPreview && <button type="button" disabled={!previewDocument} onClick={() => {
          setOpenError('');
          void onOpenPreview({ source, cardSize, appVersion: appVersion || undefined,
            width: containerWidth, height: containerHeight, zoom }).catch(reason => setOpenError(String(reason)));
        }}>在卡片渲染中打开 ↗</button>}
        <div className="card-renderer__preview-controls">
          {debugTools && debug && <label><input type="checkbox" checked={debug.enabled}
            onChange={event => debug.setEnabled(event.target.checked)} />检查组件</label>}
          <label>容器宽度<input type="number" min="1" step="1" value={containerWidth} disabled={!document} onChange={(event) => changeContainerSize('width', event.target.value)} /></label>
          <label>容器高度<input type="number" min="1" step="1" value={containerHeight} disabled={!document} onChange={(event) => changeContainerSize('height', event.target.value)} /></label>
          <label>缩放<input type="range" min="50" max="360" value={zoom} onChange={(event) => changeZoom(Number(event.target.value))} /><span>{zoom}%</span></label>
        </div>
      </div>
      <div className="card-renderer__stage" ref={stageRef} aria-label="卡片缩放画布"><div className="card-renderer__matte" style={{ transform: `scale(${zoom / 100})` }}>{previewDocument ? <InspectionCanvas><CardPreview document={previewDocument} assetBaseUrl={assetBaseUrl}
        dataUpdates={debugTools ? debug?.updates : undefined} onAction={onAction} /></InspectionCanvas>
        : <div className="card-renderer__empty">暂无卡片</div>}</div></div>
      {openError && <p role="alert">{openError}；请再次点击打开按钮重试。</p>}
    </div>
  </section>;
}

export default CardRenderer;
