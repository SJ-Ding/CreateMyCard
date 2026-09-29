import React, { CSSProperties, useMemo, useState } from 'react';
import {
  colorValue,
  ComponentNode,
  edgeNumbers,
  getPath,
  normalizeA2uiColor,
  numberOr,
  RendererDocument,
  resolveProps,
  resolveValue,
  stringify,
} from './parser';

export interface CardPreviewProps {
  document: RendererDocument;
  assetBaseUrl?: string;
  onAction?: (action: unknown, component: ComponentNode) => void;
  className?: string;
}

export interface RenderNodeProps {
  id: string;
  document: RendererDocument;
  assetBaseUrl?: string;
  depth?: number;
  onAction?: (action: unknown, component: ComponentNode) => void;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

export function sizeValue(value: unknown): string | undefined {
  if (value == null) return undefined;
  if (typeof value === 'number') return `${value}px`;
  if (typeof value !== 'string') return undefined;
  if (value === 'matchParent' || value === 'fill_container') return '100%';
  if (/^\d+(\.\d+)?$/.test(value)) return `${value}px`;
  return value;
}

export function edgeValue(value: unknown): string | undefined {
  if (value == null) return undefined;
  if (typeof value === 'number' || typeof value === 'string') return sizeValue(value);
  if (!isRecord(value)) return undefined;
  return [value.top ?? 0, value.right ?? 0, value.bottom ?? 0, value.left ?? 0]
    .map((item) => sizeValue(item) ?? '0px')
    .join(' ');
}

export function gradientValue(value: unknown): string | undefined {
  if (!isRecord(value) || !Array.isArray(value.colors)) return undefined;
  const directionMap: Record<string, string> = {
    RightBottom: 'to right bottom',
    LeftBottom: 'to left bottom',
    RightTop: 'to right top',
    LeftTop: 'to left top',
    Bottom: 'to bottom',
    Top: 'to top',
    Right: 'to right',
    Left: 'to left',
  };
  const direction = directionMap[String(value.direction)] ?? 'to right bottom';
  const stops = value.colors.map((item) => {
    if (!Array.isArray(item) || item.length === 0) return '';
    const color = colorValue(item[0]) ?? String(item[0]);
    const offset = typeof item[1] === 'number' ? ` ${item[1] * 100}%` : '';
    return `${color}${offset}`;
  }).filter(Boolean).join(', ');
  return stops ? `linear-gradient(${direction}, ${stops})` : undefined;
}

function backgroundImageValue(value: unknown): string | undefined {
  if (typeof value !== 'string' || !value) return undefined;
  return value.startsWith('url(') ? value : `url("${value}")`;
}

function justifyValue(value: unknown): CSSProperties['justifyContent'] {
  const values: Record<string, CSSProperties['justifyContent']> = {
    start: 'flex-start',
    center: 'center',
    end: 'flex-end',
    spaceBetween: 'space-between',
    spaceAround: 'space-around',
    spaceEvenly: 'space-evenly',
  };
  return typeof value === 'string' ? values[value] ?? value as CSSProperties['justifyContent'] : undefined;
}

function alignValue(value: unknown): CSSProperties['alignItems'] {
  const values: Record<string, CSSProperties['alignItems']> = {
    start: 'flex-start',
    top: 'flex-start',
    center: 'center',
    end: 'flex-end',
    bottom: 'flex-end',
  };
  return typeof value === 'string' ? values[value] ?? value as CSSProperties['alignItems'] : undefined;
}

function gridAlignValue(value: unknown): CSSProperties['placeItems'] {
  const values: Record<string, CSSProperties['placeItems']> = {
    start: 'start',
    top: 'start',
    center: 'center',
    end: 'end',
    bottom: 'end',
  };
  return typeof value === 'string' ? values[value] ?? value as CSSProperties['placeItems'] : undefined;
}

function textAlignValue(value: unknown): CSSProperties['textAlign'] {
  if (value === 'start') return 'left';
  if (value === 'end') return 'right';
  return typeof value === 'string' ? value as CSSProperties['textAlign'] : undefined;
}

function commonStyle(props: Record<string, unknown>): CSSProperties {
  const layoutWeight = numberOr(props.layoutWeight, null);
  const constraint = isRecord(props.constraintSize) ? props.constraintSize : {};
  const style: CSSProperties = {
    width: sizeValue(props.width),
    height: sizeValue(props.height),
    minWidth: sizeValue(props.minWidth ?? constraint.minWidth),
    maxWidth: sizeValue(props.maxWidth ?? constraint.maxWidth),
    minHeight: sizeValue(props.minHeight ?? constraint.minHeight),
    maxHeight: sizeValue(props.maxHeight ?? constraint.maxHeight),
    padding: edgeValue(props.padding),
    margin: edgeValue(props.margin),
    borderRadius: sizeValue(props.borderRadius),
    borderWidth: sizeValue(props.borderWidth),
    borderColor: colorValue(props.borderColor),
    opacity: typeof props.opacity === 'number' ? props.opacity : undefined,
    flexShrink: typeof props.flexShrink === 'number' ? props.flexShrink : undefined,
    flexGrow: typeof layoutWeight === 'number' ? layoutWeight : undefined,
    flexBasis: typeof layoutWeight === 'number' ? '0%' : undefined,
    alignSelf: typeof props.alignSelf === 'string' ? alignValue(props.alignSelf) as CSSProperties['alignSelf'] : undefined,
    overflow: props.clip === true ? 'hidden' : undefined,
    backgroundColor: colorValue(props.backgroundColor),
    backgroundImage: gradientValue(props.linearGradient) ?? backgroundImageValue(props.backgroundImage),
    boxSizing: 'border-box',
  };
  if (typeof layoutWeight === 'number') {
    style.minWidth = '0';
    style.minHeight = '0';
  }
  if (style.borderWidth && style.borderColor) style.borderStyle = 'solid';
  return style;
}

function layoutStyle(props: Record<string, unknown>, type: string): CSSProperties {
  const gap = sizeValue(props.space ?? props.itemMargin);
  const common: CSSProperties = {
    gap,
    justifyContent: justifyValue(props.justifyContent),
    alignItems: alignValue(props.alignItems),
  };
  if (type === 'Row') return { display: 'flex', flexDirection: 'row', alignItems: 'center', ...common };
  if (type === 'Column' || type === 'List' || type === 'Card') {
    return { display: 'flex', flexDirection: 'column', alignItems: 'stretch', ...common };
  }
  if (type === 'Grid') {
    return {
      display: 'grid',
      gridTemplateColumns: typeof props.columnsTemplate === 'string' ? props.columnsTemplate : undefined,
      gridTemplateRows: typeof props.rowsTemplate === 'string' ? props.rowsTemplate : undefined,
      columnGap: sizeValue(props.columnGap ?? props.columnsGap),
      rowGap: sizeValue(props.rowsGap),
      ...common,
    };
  }
  if (type === 'Stack') {
    return {
      display: 'grid',
      gridTemplateColumns: '1fr',
      gridTemplateRows: '1fr',
      placeItems: gridAlignValue(props.alignContent ?? props.alignItems),
      placeContent: gridAlignValue(props.alignContent) as CSSProperties['placeContent'],
      ...common,
    };
  }
  return {};
}

function componentStyle(props: Record<string, unknown>, type: string): CSSProperties {
  if (type === 'Text') {
    const numericFontSize = numberOr(props.fontSize, null);
    const textStyle: CSSProperties = {
      fontSize: sizeValue(props.fontSize),
      fontWeight: typeof props.fontWeight === 'number' || typeof props.fontWeight === 'string' ? props.fontWeight as CSSProperties['fontWeight'] : undefined,
      color: colorValue(props.fontColor),
      textAlign: textAlignValue(props.textAlign),
      lineHeight: typeof props.lineHeight === 'number' || typeof props.lineHeight === 'string'
        ? props.lineHeight : (typeof numericFontSize === 'number' && numericFontSize >= 20 ? 1.15 : 1.25),
    };
    if (props.maxLines === 1) {
      textStyle.display = 'block';
      textStyle.whiteSpace = 'nowrap';
      textStyle.overflow = 'hidden';
      textStyle.textOverflow = props.textOverflow === 'ellipsis' ? 'ellipsis' : 'clip';
    } else if (typeof props.maxLines === 'number' && props.maxLines > 0) {
      textStyle.display = '-webkit-box';
      textStyle.WebkitBoxOrient = 'vertical';
      textStyle.WebkitLineClamp = String(props.maxLines);
      textStyle.overflow = 'hidden';
    }
    if (props.textOverflow === 'ellipsis' && !props.maxLines) {
      textStyle.overflow = 'hidden';
      textStyle.textOverflow = 'ellipsis';
      textStyle.whiteSpace = 'nowrap';
    }
    return textStyle;
  }
  if (type === 'Image') {
    return { objectFit: props.objectFit === 'cover' ? 'cover' : 'contain', backgroundColor: colorValue(props.backgroundColor) };
  }
  if (type === 'Button') {
    return {
      padding: edgeValue(props.padding) ?? '0 12px',
      minHeight: 0,
      fontSize: sizeValue(props.fontSize),
      fontWeight: typeof props.fontWeight === 'number' || typeof props.fontWeight === 'string' ? props.fontWeight as CSSProperties['fontWeight'] : undefined,
      color: colorValue(props.fontColor ?? 'font_on_primary'),
      backgroundColor: colorValue(props.backgroundColor ?? 'background_emphasize'),
    };
  }
  if (type === 'Divider') {
    const vertical = props.vertical === true;
    return {
      width: vertical ? sizeValue(props.strokeWidth ?? props.width ?? 1) : sizeValue(props.width ?? '100%'),
      height: vertical ? sizeValue(props.height ?? '100%') : sizeValue(props.strokeWidth ?? props.height ?? 1),
      backgroundColor: colorValue(props.color ?? 'comp_divider'),
    };
  }
  return {};
}

function nodeStyle(props: Record<string, unknown>, type: string): CSSProperties {
  return { ...commonStyle(props), ...layoutStyle(props, type), ...componentStyle(props, type) };
}

function fallbackSvg(src: unknown): string {
  const key = String(src ?? '').toLowerCase();
  let body = '<rect x="10" y="10" width="44" height="44" rx="10" fill="none" stroke="#111" stroke-width="5"/><path d="M18 43l10-12 7 8 6-7 7 11" fill="none" stroke="#111" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>';
  if (key.includes('sun')) body = '<g fill="none" stroke="#111" stroke-width="5" stroke-linecap="round"><circle cx="32" cy="32" r="14"/><path d="M32 5v7M32 52v7M5 32h7M52 32h7M13 13l5 5M46 46l5 5M51 13l-5 5M18 46l-5 5"/></g>';
  else if (key.includes('drop') || key.includes('rain')) body = '<path d="M32 7C22 19 13 30 13 42c0 10 8 17 19 17s19-7 19-17C51 30 42 19 32 7z" fill="none" stroke="#111" stroke-width="5" stroke-linejoin="round"/>';
  else if (key.includes('snow') || key.includes('cold')) body = '<g fill="none" stroke="#111" stroke-width="5" stroke-linecap="round"><path d="M32 7v50M11 20l42 24M53 20 11 44"/></g>';
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">${body}</svg>`;
  return `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(svg)}`;
}

function resolveImageSources(src: unknown, assetBaseUrl: string): string[] {
  if (typeof src !== 'string' || !src) return [fallbackSvg(src)];
  if (/^(https?:|data:|blob:|file:)/i.test(src)) return [src];
  const normalized = src.replace(/\\/g, '/').replace(/^\.\//, '');
  const base = assetBaseUrl.endsWith('/') ? assetBaseUrl : `${assetBaseUrl}/`;
  const baseLooksLikeResources = /\/resources\/?$/i.test(base);
  const relative = baseLooksLikeResources && normalized.startsWith('resources/')
    ? normalized.slice('resources/'.length) : normalized;
  const candidates = [relative, `copy-v2/${relative}`, `code/${relative}`, `sources/${relative}`];
  return [...new Set(candidates.map((value) => {
    try {
      return new URL(value, base).href;
    } catch {
      return `${base}${value}`;
    }
  }))];
}

function FallbackImage({ src, alt, style, assetBaseUrl }: {
  src: unknown;
  alt: string;
  style?: CSSProperties;
  assetBaseUrl: string;
}) {
  const sources = useMemo(() => resolveImageSources(src, assetBaseUrl), [src, assetBaseUrl]);
  const [index, setIndex] = useState(0);
  const current = sources[index] ?? fallbackSvg(src);
  return <img
    className="card-renderer__image"
    src={current}
    alt={alt}
    style={style}
    onError={() => setIndex((previous) => previous + 1 < sources.length ? previous + 1 : sources.length)}
  />;
}

function ProgressNode({ props }: { props: Record<string, unknown> }) {
  const value = Number(resolveValue(props.value, {})) || 0;
  const total = Number(resolveValue(props.total, {})) || 100;
  const ratio = Math.max(0, Math.min(100, total ? (value / total) * 100 : value));
  const circle = props.type === 'ring' || props.type === 'circle' || props.type === 'circular';
  const style = {
    '--value': `${ratio}%`,
    '--progress-color': colorValue(props.color ?? '#0A59F7'),
    '--progress-track': colorValue(props.backgroundColor ?? 'rgba(0, 0, 0, 0.12)'),
    '--stroke-width': `${numberOr(props.strokeWidth, 5)}px`,
  } as CSSProperties;
  return <div className={`card-renderer__progress${circle ? ' is-circle' : ''}`} style={style}>
    {circle ? <span className="card-renderer__progress-hole" /> : <span style={{ width: `${ratio}%` }} />}
  </div>;
}

function CheckboxNode({ props }: { props: Record<string, unknown> }) {
  const selected = props.value === true || props.select === true;
  const style: CSSProperties = {
    background: selected ? colorValue(props.selectedColor ?? 'comp_background_emphasize') : colorValue(props.unSelectedColor ?? 'transparent'),
    borderRadius: sizeValue(props.borderRadius ?? 999),
  };
  return <div className="card-renderer__checkbox" style={style} aria-checked={selected} role="checkbox">{selected ? '✓' : ''}</div>;
}

export function RenderNode({ id, document, assetBaseUrl = '/resources/', depth = 0, onAction }: RenderNodeProps) {
  const component = document.components.get(id);
  if (!component) return <div className="card-renderer__missing" data-node-id={id} />;
  if (depth > 64) return <div className="card-renderer__missing">嵌套层级过深</div>;
  const props = resolveProps(component.props, document.data);
  const type = component.type;
  const style = nodeStyle(props, type);
  const action = props.onClick ?? props.action;
  const actionProps = action !== undefined ? {
    title: (() => { try { return JSON.stringify(action); } catch { return 'action'; } })(),
    onClick: () => onAction?.(action, component),
  } : {};
  const children = component.children.map((childId, index) => {
    const child = <RenderNode key={`${childId}-${index}`} id={childId} document={document} assetBaseUrl={assetBaseUrl} depth={depth + 1} onAction={onAction} />;
    return type === 'Stack' ? <span className="card-renderer__stack-child" key={`${childId}-${index}`}>{child}</span> : child;
  });
  let element: React.ReactNode;
  if (type === 'Text') {
    element = <p className="card-renderer__text" style={style}>{stringify(resolveValue(props.content, document.data))}</p>;
  } else if (type === 'Image') {
    const imageStyle = { ...style, width: style.width ?? '100%', height: style.height ?? '100%' };
    element = <FallbackImage src={props.src ?? props.url ?? props.source} alt={stringify(props.alt ?? '')} style={imageStyle} assetBaseUrl={assetBaseUrl} />;
  } else if (type === 'Button') {
    element = <button type="button" className="card-renderer__button" disabled={props.enabled === false} style={style} {...actionProps}>{stringify(props.label ?? props.actionText ?? '')}</button>;
  } else if (type === 'Divider') {
    element = <div className="card-renderer__divider" style={style} {...actionProps} />;
  } else if (type === 'Progress') {
    element = <div style={style} {...actionProps}><ProgressNode props={props} /></div>;
  } else if (type === 'Checkbox') {
    element = <div style={style} {...actionProps}><CheckboxNode props={props} /></div>;
  } else {
    element = <div className={`card-renderer__node card-renderer__node--${type.toLowerCase()}`} style={style} {...actionProps}>{children}</div>;
  }
  return React.isValidElement(element)
    ? React.cloneElement(element, { 'data-node-id': component.id } as Record<string, unknown>)
    : element;
}

export function CardPreview({ document, assetBaseUrl = '/resources/', onAction, className = '' }: CardPreviewProps) {
  return <div
    className={`card-renderer__surface ${className}`.trim()}
    style={{ width: document.surface.width, height: document.surface.height }}
    data-renderer-mode={document.mode}
    data-renderer-size={`${document.surface.width}x${document.surface.height}`}
  >
    <RenderNode id={document.rootId} document={document} assetBaseUrl={assetBaseUrl} onAction={onAction} />
  </div>;
}

export { colorValue, normalizeA2uiColor, getPath };
