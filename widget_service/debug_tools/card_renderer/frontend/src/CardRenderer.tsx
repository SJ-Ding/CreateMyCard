import React, { ChangeEvent, useEffect, useMemo, useState } from 'react';
import { CardPreview } from './render';
import { CardSize, ComponentNode, parseInput, RendererDocument } from './parser';
import { SAMPLE_A2UI, SAMPLE_COMPACT, SAMPLE_DESIGN } from './fixtures';
import './styles.css';

export interface CardRendererProps {
  /** JSONL/JSON/compact DSL source supplied by the host; manual edits remain local. */
  initialValue?: string;
  /** Base URL used for relative image resources. Defaults to same-origin /resources/. */
  assetBaseUrl?: string;
  /** Called after a successful parse, allowing the platform to publish the artifact. */
  onArtifact?: (document: RendererDocument) => void;
  /** Receives resolved local actions; the renderer never performs external navigation. */
  onAction?: (action: unknown, component: ComponentNode) => void;
  className?: string;
}

const DEFAULT_SOURCE = SAMPLE_COMPACT;

function sourceValue(value: string | undefined): string {
  return typeof value === 'string' ? value : DEFAULT_SOURCE;
}

export function CardRenderer({ initialValue, assetBaseUrl = '/resources/', onArtifact, onAction, className = '' }: CardRendererProps) {
  const [source, setSource] = useState(() => sourceValue(initialValue));
  const [cardSize, setCardSize] = useState<CardSize>('auto');
  const [zoom, setZoom] = useState(220);
  const [autoRender, setAutoRender] = useState(true);
  const [document, setDocument] = useState<RendererDocument | null>(null);
  const [error, setError] = useState('');
  const [interaction, setInteraction] = useState('');

  useEffect(() => {
    if (initialValue === undefined) return;
    setSource(initialValue);
    // A selected shared call may keep the same component identity while its
    // final frame arrives. Clear the previous preview so manual-render mode
    // cannot display an unrelated call's document.
    setDocument(null);
    setError('');
    setInteraction('');
  }, [initialValue]);

  const render = (nextSource = source) => {
    const text = nextSource.trim();
    if (!text) {
      setDocument(null);
      setError('');
      return;
    }
    try {
      const parsed = parseInput(text, { cardSize });
      setDocument(parsed);
      setError('');
      onArtifact?.(parsed);
    } catch (reason) {
      setDocument(null);
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  useEffect(() => {
    if (!autoRender) return;
    const timer = window.setTimeout(() => render(), 250);
    return () => window.clearTimeout(timer);
  }, [source, cardSize, autoRender]);

  const status = useMemo(() => {
    if (error) return error;
    if (!document) return source.trim() ? '未渲染' : '请输入 JSONL 或 DSL';
    const warningText = document.warnings.length > 0
      ? `，${document.warnings.length} 条警告`
      : '';
    return `已渲染 ${document.components.size} 个组件，DataModel ${document.dataPathCount} 个路径${warningText}。`;
  }, [document, error, source]);

  const loadFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const value = await file.text();
      setSource(value);
      if (!autoRender) render(value);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      event.target.value = '';
    }
  };

  const setSample = (value: string) => {
    setSource(value);
    setInteraction('');
    if (!autoRender) render(value);
  };

  return <section className={`card-renderer ${className}`.trim()} aria-label="卡片生成结果渲染器">
    <div className="card-renderer__editor">
      <div className="card-renderer__toolbar">
        <button type="button" className="is-primary" onClick={() => render()}>渲染</button>
        <label className="card-renderer__file-button">打开 JSONL<input type="file" accept=".jsonl,.json,.md,.txt,application/json,text/plain" onChange={loadFile} /></label>
        <button type="button" onClick={() => { setSource(''); render(''); }}>清空</button>
        <button type="button" onClick={() => setSample(SAMPLE_A2UI)}>A2UI 示例</button>
        <button type="button" onClick={() => setSample(SAMPLE_COMPACT)}>极简示例</button>
        <button type="button" onClick={() => setSample(SAMPLE_DESIGN)}>Design 示例</button>
        <label className="card-renderer__control">画布<select value={cardSize} onChange={(event) => setCardSize(event.target.value as CardSize)}><option value="auto">自动</option><option value="2x2">2×2 · 160×160</option><option value="2x4">2×4 · 320×160</option></select></label>
        <label className="card-renderer__check"><input type="checkbox" checked={autoRender} onChange={(event) => setAutoRender(event.target.checked)} />自动渲染</label>
      </div>
      <textarea value={source} onChange={(event) => setSource(event.target.value)} spellCheck={false} aria-label="DSL 输入" />
      <div className={`card-renderer__status${error ? ' is-error' : ''}`} role={error ? 'alert' : 'status'}>{status}</div>
    </div>
    <div className="card-renderer__preview">
      <div className="card-renderer__preview-toolbar"><span>{document ? `${document.mode} · ${document.surface.width} × ${document.surface.height}` : '预览'}</span><label>缩放<input type="range" min="50" max="360" value={zoom} onChange={(event) => setZoom(Number(event.target.value))} /><span>{zoom}%</span></label></div>
      <div className="card-renderer__stage"><div className="card-renderer__matte" style={{ transform: `scale(${zoom / 100})` }}>{document ? <CardPreview document={document} assetBaseUrl={assetBaseUrl} onAction={(action, component) => {
        setInteraction(JSON.stringify({ action, componentId: component.id }, null, 2));
        onAction?.(action, component);
      }} /> : <div className="card-renderer__empty">暂无卡片</div>}</div></div>
      {interaction && <div className="card-renderer__interaction" role="status"><div><strong>点击事件</strong><button type="button" onClick={() => setInteraction('')}>清除</button></div><pre>{interaction}</pre><p>仅展示解析后的事件参数，不执行外部跳转。</p></div>}
    </div>
  </section>;
}

export default CardRenderer;
