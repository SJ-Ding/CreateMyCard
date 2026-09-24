import { useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { buildDefaultArguments, buildOutputFieldPaths, buildSelectedSubset, extractArtifact, findFinalFrame, getAtPath, parsePythonRepr, streamContent, streamType } from './parser';
import { connectToolSocket, type ToolSocket } from './transport';
import type {
  CommonEnvelopeState,
  FieldConfig,
  HistoryEntry,
  InterfaceArtifact,
  InterfaceDebuggerProps,
  Selection,
  ToolFrame,
  ToolFrameRecord,
  ToolOperation,
} from './types';
import { TOOL_OPERATIONS } from './types';
import './styles.css';

const OPERATION_LABELS: Record<ToolOperation, string> = {
  getWidgetCapabilityOverview: '能力概览',
  getDataCapabilitySchemas: '数据 Schema',
  generateWidgetCardCompactDsl: '生成 Compact DSL',
};

function createClientId(prefix: string): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

const COMMON_DEFAULTS: CommonEnvelopeState = {
  sessionId: createClientId('session'),
  interactionId: '1',
  userId: 'debug-user',
  bundleName: 'com.omega_w_0823.hmservice',
  version: '1.0',
  utterance: '',
  countryCode: 'CN',
  deviceFormation: 'HDSpeaker',
  deviceType: '0',
  locale: 'zh-CN',
  phoneType: 'CLS-AL00',
  prdVer: '11.7.7.332',
  sysVer: 'HarmonyOS',
  romVersion: 'CLS-AL00 7.0.0.100',
  time: new Date().toISOString().replace(/[-:TZ.]/g, '').slice(0, 17),
};

const COMMON_FIELDS: FieldConfig[] = [
  { id: 'sessionId', label: 'Session ID', type: 'text' },
  { id: 'interactionId', label: 'Interaction ID', type: 'text' },
  { id: 'userId', label: 'User ID', type: 'text' },
  { id: 'bundleName', label: 'Bundle Name', type: 'text' },
  { id: 'version', label: 'Version', type: 'text' },
  { id: 'utterance', label: 'Utterance', type: 'text' },
  { id: 'countryCode', label: 'Country Code', type: 'text' },
  { id: 'deviceFormation', label: 'Device Formation', type: 'text' },
  { id: 'deviceType', label: 'Device Type', type: 'text' },
  { id: 'locale', label: 'Locale', type: 'text' },
  { id: 'phoneType', label: 'Phone Type', type: 'text' },
  { id: 'prdVer', label: 'App Version', type: 'text' },
  { id: 'sysVer', label: 'System Version', type: 'text' },
  { id: 'romVersion', label: 'ROM Version', type: 'text' },
  { id: 'time', label: 'Device Time', type: 'text' },
];

const BUSINESS_FIELDS: Record<ToolOperation, FieldConfig[]> = {
  getWidgetCapabilityOverview: [
    { id: 'odid', label: 'ODID', type: 'text', placeholder: '设备 ODID' },
    { id: 'contentBundleName', label: 'Content Bundle Name', type: 'text', placeholder: '可选' },
  ],
  getDataCapabilitySchemas: [
    { id: 'odid', label: 'ODID', type: 'text', placeholder: '设备 ODID' },
    { id: 'contentBundleName', label: 'Content Bundle Name', type: 'text', placeholder: '可选' },
    { id: 'dataCapabilityIds', label: 'Data Capability IDs', type: 'text', required: true, placeholder: 'ViewWeather,GetCalendarEvents' },
  ],
  generateWidgetCardCompactDsl: [
    { id: 'userQuery', label: 'User Query', type: 'text', required: true, placeholder: '帮我做通勤卡片，包含天气' },
    { id: 'title', label: 'Title', type: 'text', required: true, placeholder: '通勤日常' },
    { id: 'description', label: 'Description', type: 'text', required: true, placeholder: '天气速览' },
    { id: 'size', label: 'Size', type: 'select', default: '2x2', options: ['2x2', '2x4'] },
    { id: 'odid', label: 'ODID', type: 'text', placeholder: '设备 ODID' },
    { id: 'contentBundleName', label: 'Content Bundle Name', type: 'text', placeholder: '可选' },
    { id: 'sourceArtifactUrl', label: 'Source Artifact URL', type: 'text', placeholder: '编辑模式可填写' },
    { id: 'candidateAssetIds', label: 'Candidate Asset IDs', type: 'text', placeholder: 'asset.drop_1,asset.clock_fill' },
    { id: 'candidateDataBindings', label: 'Candidate Data Bindings', type: 'textarea', placeholder: '{"capabilityId":"ViewWeather"}' },
    { id: 'candidateEventCandidates', label: 'Candidate Event Candidates', type: 'textarea', placeholder: '[{"capabilityId":"event.open"}]' },
    { id: 'allowDegradation', label: 'Allow Degradation', type: 'checkbox', default: true },
  ],
};

type BusinessValues = Record<string, string>;

function businessDefaults(operation: ToolOperation): BusinessValues {
  const values: BusinessValues = {};
  BUSINESS_FIELDS[operation].forEach((field) => { values[field.id] = String(field.default ?? ''); });
  return values;
}

function jsonText(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function buildEnvelope(common: CommonEnvelopeState, operation: ToolOperation, business: unknown): Record<string, unknown> {
  const content = business && typeof business === 'object' && !Array.isArray(business)
    ? business
    : {};
  return {
    content,
    deviceInfo: {
      countryCode: common.countryCode,
      deviceFormation: common.deviceFormation,
      deviceType: Number(common.deviceType) || 0,
      locale: common.locale,
      phoneType: common.phoneType,
      prdVer: common.prdVer,
      sysVer: common.sysVer,
      romVersion: common.romVersion,
      time: common.time,
    },
    session: { sessionId: common.sessionId, interactionId: common.interactionId, isNew: false },
    userAuth: { user: { userId: common.userId } },
    utterance: { original: common.utterance, type: 'text' },
    version: common.version,
    bundleName: common.bundleName,
  };
}

function defaultBusiness(operation: ToolOperation, raw: string, formValues: BusinessValues): unknown {
  if (raw.trim()) {
    try {
      return JSON.parse(raw);
    } catch {
      return { userQuery: raw };
    }
  }
  const missing = BUSINESS_FIELDS[operation].find((field) => field.required && !(formValues[field.id] ?? '').trim());
  if (missing) throw new Error(`${missing.label} 为必填项`);
  const result: Record<string, unknown> = {};
  BUSINESS_FIELDS[operation].forEach((field) => {
    const value = (formValues[field.id] ?? '').trim();
    if (!value) return;
    if (field.type === 'textarea') {
      try { result[field.id] = JSON.parse(value) as unknown; } catch { result[field.id] = value; }
    } else if (field.id === 'allowDegradation') {
      result.options = { allowDegradation: value === 'true' };
    } else if (field.id === 'dataCapabilityIds' || field.id === 'candidateAssetIds') {
      result[field.id] = value.split(',').map((item) => item.trim()).filter(Boolean);
    } else if (field.id === 'contentBundleName') {
      result.bundleName = value;
    } else {
      result[field.id] = value;
    }
  });
  if (operation === 'getWidgetCapabilityOverview') return result;
  if (operation === 'getDataCapabilitySchemas') return { dataCapabilityIds: [], ...result };
  return {
    userQuery: '创建一个天气卡片',
    title: '天气',
    description: '本地调试卡片',
    size: '2x2',
    ...result,
  };
}

function displayFrame(frame: ToolFrameRecord): string {
  return `${frame.type.toUpperCase()}\n${jsonText(frame.data)}`;
}

function Tree({ value, path, selected, onToggle, arrayLimits, onArrayLimit }: {
  value: unknown;
  path: string;
  selected: Set<string>;
  onToggle: (path: string, value: unknown) => void;
  arrayLimits: Record<string, number>;
  onArrayLimit: (path: string, value: number) => void;
}) {
  if (value === null || typeof value !== 'object') {
    return <label className="tree-leaf"><input type="checkbox" checked={selected.has(path)} onChange={() => onToggle(path, value)} /> <code>{path || '/'}</code><span>{String(value)}</span></label>;
  }
  const entries = Array.isArray(value)
    ? value.slice(0, arrayLimits[path] ?? 20).map((item, index) => [String(index), item] as const)
    : Object.entries(value as Record<string, unknown>);
  return <div className="tree-node">{Array.isArray(value) && value.length > 20 && <label className="array-limit">展开数组项 <input type="number" min={1} max={100} value={arrayLimits[path] ?? 20} onChange={(event) => onArrayLimit(path, Math.min(100, Math.max(1, Number(event.target.value) || 1)))} /></label>}{entries.map(([key, child]) => {
    const childPath = path ? `${path}.${key}` : key;
    return <details open key={childPath}><summary><span>{key}</span></summary><Tree value={child} path={childPath} selected={selected} onToggle={onToggle} arrayLimits={arrayLimits} onArrayLimit={onArrayLimit} /></details>;
  })}</div>;
}

export function InterfaceDebugger({ transportBase, socketBasePath, onEvent, onArtifact, className = '' }: InterfaceDebuggerProps) {
  const [operation, setOperation] = useState<ToolOperation>(TOOL_OPERATIONS[0]);
  const [common, setCommon] = useState<CommonEnvelopeState>(COMMON_DEFAULTS);
  const [businessText, setBusinessText] = useState('');
  const [businessValues, setBusinessValues] = useState<BusinessValues>(() => businessDefaults(TOOL_OPERATIONS[0]));
  const [frames, setFrames] = useState<ToolFrameRecord[]>([]);
  const [histories, setHistories] = useState<HistoryEntry[]>([]);
  const [activeHistory, setActiveHistory] = useState<number | null>(null);
  const [activeTab, setActiveTab] = useState<'stream' | 'parsed' | 'build'>('stream');
  const [selectedPaths, setSelectedPaths] = useState<Set<string>>(new Set());
  const [builtJson, setBuiltJson] = useState('');
  const [status, setStatus] = useState('就绪');
  const [search, setSearch] = useState('');
  const [arrayLimits, setArrayLimits] = useState<Record<string, number>>({});
  const socketRef = useRef<ToolSocket | null>(null);
  const startedAtRef = useRef(0);
  const nextHistoryIdRef = useRef(1);

  const parsedResult = useMemo(() => {
    const final = findFinalFrame(frames.map((frame) => frame.data));
    return final ? parsePythonRepr(streamContent(final)) : null;
  }, [frames]);

  const filteredResult = useMemo(() => {
    if (!search.trim() || parsedResult == null) return parsedResult;
    const needle = search.toLowerCase();
    const filter = (value: unknown): unknown => {
      if (value == null || typeof value !== 'object') return String(value).toLowerCase().includes(needle) ? value : undefined;
      if (Array.isArray(value)) return value.map(filter).filter((item) => item !== undefined);
      const result: Record<string, unknown> = {};
      Object.entries(value).forEach(([key, child]) => {
        const filtered = filter(child);
        if (key.toLowerCase().includes(needle) || filtered !== undefined) result[key] = filtered ?? child;
      });
      return Object.keys(result).length ? result : undefined;
    };
    return filter(parsedResult);
  }, [parsedResult, search]);

  const updateCommon = (key: keyof CommonEnvelopeState, value: string) => {
    setCommon((current) => ({ ...current, [key]: value }));
  };

  const send = () => {
    let business: unknown;
    try {
      business = defaultBusiness(operation, businessText, businessValues);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : String(error));
      return;
    }
    const envelope = buildEnvelope(common, operation, business);
    const nextFrames: ToolFrameRecord[] = [];
    setFrames(nextFrames);
    setSelectedPaths(new Set());
    setBuiltJson('');
    setArrayLimits({});
    setActiveTab('stream');
    setStatus('连接中…');
    startedAtRef.current = performance.now();
    socketRef.current?.close();
    const socket = connectToolSocket(transportBase ?? socketBasePath, operation, envelope, {
      onFrame: (frame) => {
        const record: ToolFrameRecord = { id: createClientId('frame'), type: streamType(frame) as ToolFrameRecord['type'], timestamp: new Date().toISOString(), data: frame };
        nextFrames.push(record);
        setFrames([...nextFrames]);
        const kind = streamType(frame);
        setStatus(kind === 'final' || kind === 'final_error'
          ? `已完成 · ${Math.max(0, Math.round(performance.now() - startedAtRef.current))} ms`
          : `接收 ${kind}`);
        if (kind === 'final' || kind === 'final_error') {
          const parsed = kind === 'final' ? parsePythonRepr(streamContent(frame)) : null;
          const artifact = extractArtifact(parsed, operation, record.id) as InterfaceArtifact;
          if (kind === 'final') onArtifact?.(artifact);
          const id = nextHistoryIdRef.current++;
          setActiveHistory(id);
          setHistories((items) => [...items, { id, timestamp: record.timestamp, operation, request: envelope, frames: [...nextFrames], parsedResult: parsed, rawStreamContent: streamContent(frame), sourceHistoryId: activeHistory ?? undefined }]);
          window.setTimeout(() => socket.close(), 250);
        }
      },
      onStatus: (next) => setStatus(next.text),
      onEvent,
    });
    socketRef.current = socket;
  };

  const selectHistory = (history: HistoryEntry) => {
    setOperation(history.operation);
    setFrames(history.frames);
    setActiveHistory(history.id);
    setBusinessText(jsonText(history.request.content ?? history.request));
    setBusinessValues(businessDefaults(history.operation));
    setSelectedPaths(new Set());
    setArrayLimits({});
    setActiveTab('parsed');
  };

  const toggleSelection = (path: string, value: unknown) => {
    setSelectedPaths((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path); else next.add(path);
      if (parsedResult && path) {
        const selections: Selection[] = [...next].map((item) => ({ historyId: activeHistory ?? 0, operation, path: item, key: item.split('.').pop() ?? item, value: getAtPath(parsedResult, item) }));
        setBuiltJson(jsonText(buildSelectedSubset(parsedResult, selections)));
      }
      return next;
    });
  };

  const buildQuick = (kind: 'selected' | 'data' | 'assets' | 'events' | 'bindings') => {
    if (!parsedResult || !activeHistory) return;
    const selections: Selection[] = [...selectedPaths].map((path) => ({
      historyId: activeHistory,
      operation,
      path,
      key: path.split('.').pop() ?? path,
      value: getAtPath(parsedResult, path),
    }));
    const source = parsedResult && typeof parsedResult === 'object' && !Array.isArray(parsedResult)
      ? parsedResult as Record<string, unknown>
      : {};
    const selectedItems = (name: string): Record<string, unknown>[] => {
      const collection = source[name];
      if (!Array.isArray(collection)) return [];
      const indexes = new Set<number>();
      selections.forEach((item) => {
        const parts = item.path.split('.');
        if (parts[0] !== name) return;
        if (/^\d+$/.test(parts[1] ?? '')) indexes.add(Number(parts[1]));
      });
      return [...indexes].map((index) => collection[index]).filter((item): item is Record<string, unknown> => !!item && typeof item === 'object' && !Array.isArray(item));
    };
    let result: unknown;
    if (kind === 'selected') result = buildSelectedSubset(parsedResult, selections);
    else if (kind === 'data') result = { dataCapabilityIds: selectedItems('dataCapabilities').map((item) => item.id).filter(Boolean) };
    else if (kind === 'assets') result = { candidateAssetIds: selectedItems('assetCandidates').map((item) => item.id).filter(Boolean) };
    else if (kind === 'events') result = { candidateEventCandidates: selectedItems('eventCapabilities').map((item) => ({ capabilityId: item.id ?? '', action: item.actionTemplate ?? item.action ?? { call: '', args: {} } })) };
    else result = { candidateDataBindings: selectedItems('dataCapabilities').map((item) => ({
      capabilityId: item.id ?? '',
      arguments: buildDefaultArguments(item.inputSchema as Parameters<typeof buildDefaultArguments>[0], true),
      writeResultTo: item.defaultWriteResultTo ?? '',
      candidateOutputFields: buildOutputFieldPaths(item.outputSchema as Parameters<typeof buildOutputFieldPaths>[0]),
    })) };
    setBuiltJson(jsonText(result));
    setActiveTab('build');
  };

  const handleSubmitShortcut = (event: KeyboardEvent<HTMLElement>) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault();
      send();
    }
  };

  return <section className={`interface-debugger ${className}`.trim()} aria-label="接口调试">
    <div className="interface-header">
      <div><span className="section-kicker">WEBSOCKET API</span><h2>接口调试</h2><p>按 Overview → Schemas → Compact DSL 串行检查工具链路。</p></div>
      <div className="interface-status"><span className={`status-dot ${status.includes('错误') ? 'error' : 'online'}`} />{status}</div>
    </div>
    <div className="operation-tabs" role="tablist">{TOOL_OPERATIONS.map((item) => <button type="button" role="tab" aria-selected={operation === item} className={operation === item ? 'active' : ''} onClick={() => { setOperation(item); setBusinessValues(businessDefaults(item)); setBusinessText(''); const latest = [...histories].reverse().find((history) => history.operation === item); if (latest) selectHistory(latest); else { setActiveHistory(null); setFrames([]); setSelectedPaths(new Set()); setBuiltJson(''); } }} key={item}>{OPERATION_LABELS[item]}</button>)}</div>
    <div className="interface-history"><span>历史</span>{histories.length === 0 ? <em>暂无请求</em> : histories.map((item) => <button type="button" className={activeHistory === item.id ? 'active' : ''} onClick={() => selectHistory(item)} key={item.id}>#{item.id} · {OPERATION_LABELS[item.operation]}</button>)}{histories.length > 0 && <button type="button" onClick={() => { setHistories([]); setActiveHistory(null); setFrames([]); setSelectedPaths(new Set()); setBuiltJson(''); setArrayLimits({}); nextHistoryIdRef.current = 1; }}>清空历史</button>}</div>
    <div className="interface-grid">
      <div className="interface-panel request-panel" onKeyDown={handleSubmitShortcut}>
        <div className="panel-heading"><h3>请求</h3><button type="button" onClick={() => setBusinessText('')}>清空业务参数</button></div>
        <details open><summary>公共包络</summary><div className="common-fields">{COMMON_FIELDS.map((field) => <label key={field.id}>{field.label}<input value={common[field.id as keyof CommonEnvelopeState]} onChange={(event) => updateCommon(field.id as keyof CommonEnvelopeState, event.target.value)} /></label>)}</div></details>
        <div className="business-form"><div className="business-form-heading"><strong>接口参数</strong><button type="button" onClick={() => { setBusinessValues(businessDefaults(operation)); setBusinessText(''); }}>恢复示例</button></div>{BUSINESS_FIELDS[operation].map((field) => <label className={`business-field${field.type === 'checkbox' ? ' checkbox-field' : ''}`} key={field.id}>{field.label}{field.required && <span className="required-mark">*</span>}{field.type === 'textarea' ? <textarea value={businessValues[field.id] ?? ''} onChange={(event) => setBusinessValues((current) => ({ ...current, [field.id]: event.target.value }))} placeholder={field.placeholder} spellCheck={false} rows={3} /> : field.type === 'select' ? <select value={businessValues[field.id] ?? String(field.default ?? '')} onChange={(event) => setBusinessValues((current) => ({ ...current, [field.id]: event.target.value }))}>{(field.options ?? []).map((option) => <option value={option} key={option}>{option}</option>)}</select> : field.type === 'checkbox' ? <input type="checkbox" checked={businessValues[field.id] === 'true'} onChange={(event) => setBusinessValues((current) => ({ ...current, [field.id]: String(event.target.checked) }))} /> : <input value={businessValues[field.id] ?? ''} onChange={(event) => setBusinessValues((current) => ({ ...current, [field.id]: event.target.value }))} placeholder={field.placeholder} />}</label>)}</div><label className="business-field advanced-field">高级 JSON（可选）<textarea value={businessText} onChange={(event) => setBusinessText(event.target.value)} placeholder="填写后将覆盖上面的接口参数" spellCheck={false} rows={4} /></label>
        <button type="button" className="primary-action" onClick={send}>发送请求 <span>Ctrl + Enter</span></button>
      </div>
      <div className="interface-panel response-panel">
        <div className="panel-heading"><h3>响应</h3><button type="button" onClick={() => setFrames([])}>清空</button></div>
        <div className="response-tabs">{(['stream', 'parsed', 'build'] as const).map((tab) => <button type="button" className={activeTab === tab ? 'active' : ''} onClick={() => setActiveTab(tab)} key={tab}>{tab === 'stream' ? '流式帧' : tab === 'parsed' ? '解析结果' : '构建 JSON'}</button>)}</div>
        {activeTab === 'stream' && <div className="frame-list">{frames.length === 0 ? <div className="empty-response">发送请求后显示原始 WebSocket 帧。</div> : frames.map((frame) => <pre className={`frame ${frame.type}`} key={frame.id}>{displayFrame(frame)}</pre>)}</div>}
        {activeTab === 'parsed' && <div className="parsed-panel"><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索字段或值…" />{filteredResult == null ? <div className="empty-response">暂无可解析的 final 帧。</div> : <Tree value={filteredResult} path="" selected={selectedPaths} onToggle={toggleSelection} arrayLimits={arrayLimits} onArrayLimit={(path, value) => setArrayLimits((current) => ({ ...current, [path]: value }))} />}</div>}
        {activeTab === 'build' && <div className="build-panel"><p>已选择 {selectedPaths.size} 个字段。</p><div className="quick-build-actions">{(['selected', 'data', 'bindings', 'assets', 'events'] as const).map((kind) => <button type="button" key={kind} disabled={selectedPaths.size === 0} onClick={() => buildQuick(kind)}>{kind === 'selected' ? '选中内容' : kind === 'data' ? 'dataCapabilityIds' : kind === 'bindings' ? 'candidateDataBindings' : kind === 'assets' ? 'candidateAssetIds' : 'candidateEventCandidates'}</button>)}</div><textarea value={builtJson} onChange={(event) => setBuiltJson(event.target.value)} spellCheck={false} placeholder="从解析树选择字段，或手动编辑 JSON。" /><div className="build-actions"><button type="button" onClick={() => navigator.clipboard?.writeText(builtJson)}>复制 JSON</button><button type="button" className="primary-action" onClick={() => { setBusinessText(builtJson); setBusinessValues(businessDefaults(operation)); setActiveTab('stream'); }}>应用到请求</button></div></div>}
      </div>
    </div>
  </section>;
}

export default InterfaceDebugger;
