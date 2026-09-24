import { FormEvent, KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ArtifactPreview from './components/ArtifactPreview';
import type {
  ArtifactRecord,
  ContextValues,
  DebugEvent,
  QuickPrompt,
  SkillProfile,
  TimelineEntry,
} from './types';

export interface EndToEndEvent {
  direction: 'send' | 'receive' | 'local';
  kind: string;
  payload?: unknown;
}

export interface EndToEndDebugProps {
  socketPath?: string;
  healthPath?: string;
  onEvent?: (event: EndToEndEvent) => void;
  onArtifact?: (artifact: ArtifactRecord) => void;
}

const ARTIFACT_KINDS: Array<[string, string]> = [
  ['genui', 'GenUI DSL'],
  ['cardSpec', 'CardSpec'],
  ['taskSpec', 'TaskSpec'],
  ['effectiveCapabilities', '有效能力'],
  ['removedCapabilities', '移除能力'],
  ['generationPlan', '生成计划'],
  ['meta', 'Meta'],
  ['designToken', 'Design Token'],
];

const DEFAULT_CONTEXT: ContextValues = {
  uid: 'debug-user',
  odid: 'debug-device',
  deviceId: 'debug-device',
  phoneType: 'ALN-AL00',
  appVersion: '11.7.7.332',
  romVersion: 'ALN-AL00 7.0.0.100',
  locale: 'zh-CN',
  countryCode: 'CN',
};

type Message = { role: 'user' | 'assistant'; content: string; runId?: string; timestamp?: string };
type Health = Record<string, unknown> & {
  status?: string;
  model?: string;
  upstreamStatus?: string;
  upstreamReachable?: boolean;
};

const DEFAULT_QUICK_PROMPTS: QuickPrompt[] = [
  { label: '天气卡片', prompt: '帮我做一个今天的天气卡片，显示上海市天气' },
  { label: '待办清单', prompt: '创建一个待办清单卡片' },
  { label: '日程安排', prompt: '创建一个日程安排卡片，突出显示今天的重要事项' },
];

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function textValue(value: unknown, fallback = ''): string {
  if (typeof value === 'string') return value;
  if (value === null || value === undefined) return fallback;
  return String(value);
}

function jsonText(value: unknown): string {
  if (typeof value === 'string') return value;
  try { return JSON.stringify(value ?? {}, null, 2); } catch { return String(value); }
}

function parseArguments(value: unknown): Record<string, unknown> {
  if (typeof value !== 'string') return objectValue(value);
  try { return objectValue(JSON.parse(value.trim())); } catch { return {}; }
}

function prettyArguments(value: unknown): string {
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (!trimmed) return '{}';
    try { return jsonText(JSON.parse(trimmed)); } catch { return value; }
  }
  return jsonText(value);
}

function formatTime(value?: string): string {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString('zh-CN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

function icon(name: string) {
  const paths: Record<string, string> = {
    spark: 'M12 2l1.7 6.3L20 10l-6.3 1.7L12 18l-1.7-6.3L4 10l6.3-1.7L12 2z',
    user: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zm-7 8a7 7 0 0 1 14 0',
    agent: 'M12 3 14 8l5 2-5 2-2 5-2-5-5-2 5-2 2-5z',
    send: 'M3 11.8 21 3l-8.8 18-1.7-7.5L3 11.8z',
    refresh: 'M20 11a8 8 0 0 0-14.9-4L3 10h6M4 13a8 8 0 0 0 14.9 4L21 14h-6',
    settings: 'M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7zm0-6v2m0 15v2m9.5-9.5h-2M4.5 12h-2m16.2-6.2-1.4 1.4M6.7 17.3l-1.4 1.4m13.4 0-1.4-1.4M6.7 6.7 5.3 5.3',
    copy: 'M8 8h10v12H8zM6 16H4V4h10v2',
    download: 'M12 3v12m0 0 4-4m-4 4-4-4M4 20h16',
  };
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d={paths[name] ?? paths.spark} /></svg>;
}

function timelineGlyph(entry: TimelineEntry) {
  if (entry.type === 'run_started' || entry.meta?.flow === 'user') return icon('user');
  if (entry.type === 'assistant_message' || entry.meta?.flow === 'assistant') return icon('agent');
  if (entry.meta?.flow === 'request') return '→';
  if (entry.meta?.flow === 'execution-error') return '×';
  if (entry.meta?.flow === 'result' || entry.meta?.flow === 'business-error') return '←';
  if (entry.status === 'running') return '…';
  if (entry.status === 'success') return '✓';
  if (entry.status) return '!';
  return '·';
}

function resolveSocketUrl(path: string): string {
  if (/^wss?:\/\//i.test(path)) return path;
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const normalized = path.startsWith('/') ? path : `/${path}`;
  return `${scheme}://${window.location.host}${normalized}`;
}

export default function App({
  socketPath = '/debug/e2e/ws',
  healthPath = '/debug/health',
  onEvent,
  onArtifact,
}: EndToEndDebugProps = {}) {
  const [connected, setConnected] = useState(false);
  const [connecting, setConnecting] = useState(true);
  const [connectionError, setConnectionError] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [profile, setProfile] = useState('');
  const [profiles, setProfiles] = useState<SkillProfile[]>([]);
  const [quickPrompts, setQuickPrompts] = useState<QuickPrompt[]>(DEFAULT_QUICK_PROMPTS);
  const [skillName, setSkillName] = useState('等待 Skill 信息');
  const [context, setContext] = useState<ContextValues>(DEFAULT_CONTEXT);
  const [input, setInput] = useState('');
  const [messages, setMessages] = useState<Message[]>([]);
  const [timeline, setTimeline] = useState<TimelineEntry[]>([]);
  const [artifacts, setArtifacts] = useState<ArtifactRecord[]>([]);
  const [artifactIndex, setArtifactIndex] = useState(0);
  const [artifactKind, setArtifactKind] = useState('genui');
  const [running, setRunning] = useState(false);
  const [runStatus, setRunStatus] = useState('准备就绪');
  const [health, setHealth] = useState<Health | null>(null);
  const [mobileTab, setMobileTab] = useState<'chat' | 'timeline' | 'artifact'>('chat');
  const [contextOpen, setContextOpen] = useState(false);
  const [timelineSearch, setTimelineSearch] = useState('');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const socketRef = useRef<WebSocket | null>(null);
  const runStartedAt = useRef(0);
  const artifactCounter = useRef(0);
  const timelineCounter = useRef(0);
  const onEventRef = useRef(onEvent);
  const onArtifactRef = useRef(onArtifact);

  useEffect(() => {
    onEventRef.current = onEvent;
    onArtifactRef.current = onArtifact;
  }, [onArtifact, onEvent]);

  const addTimeline = useCallback((entry: Omit<TimelineEntry, 'id'>, id?: string) => {
    setTimeline((items) => [...items, { ...entry, id: id ?? `event-${Date.now()}-${timelineCounter.current++}` }]);
  }, []);

  const processEvent = useCallback((event: DebugEvent) => {
    onEventRef.current?.({ direction: 'receive', kind: event.type, payload: event });
    const data = objectValue(event.data);
    const timestamp = event.timestamp ?? new Date().toISOString();
    if (event.sessionId) setSessionId(event.sessionId);
    if (event.type === 'session_started') {
      setProfile(textValue(data.skillProfile));
      setProfiles(Array.isArray(data.skillProfiles) ? data.skillProfiles as SkillProfile[] : []);
      setSkillName(textValue(data.skillName, 'Main Agent Debug'));
      if (Array.isArray(data.quickPrompts)) {
        const configured = data.quickPrompts.filter((item): item is QuickPrompt => (
          !!item && typeof item === 'object'
          && typeof (item as QuickPrompt).label === 'string'
          && typeof (item as QuickPrompt).prompt === 'string'
          && (item as QuickPrompt).label.trim().length > 0
          && (item as QuickPrompt).prompt.trim().length > 0
        ));
        setQuickPrompts(configured.length ? configured : DEFAULT_QUICK_PROMPTS);
      }
      if (data.context) setContext((current) => ({ ...current, ...objectValue(data.context) }));
      addTimeline({ type: event.type, title: '会话已建立', detail: textValue(data.skillProfile), timestamp, tone: 'success' });
      return;
    }
    if (event.type === 'session_reset') {
      setProfile(textValue(data.skillProfile));
      setProfiles(Array.isArray(data.skillProfiles) ? data.skillProfiles as SkillProfile[] : []);
      if (typeof data.skillName === 'string') setSkillName(data.skillName);
      if (Array.isArray(data.quickPrompts)) {
        const configured = data.quickPrompts.filter((item): item is QuickPrompt => (
          !!item && typeof item === 'object'
          && typeof (item as QuickPrompt).label === 'string'
          && typeof (item as QuickPrompt).prompt === 'string'
          && (item as QuickPrompt).label.trim().length > 0
          && (item as QuickPrompt).prompt.trim().length > 0
        ));
        setQuickPrompts(configured.length ? configured : DEFAULT_QUICK_PROMPTS);
      }
      setMessages([]); setTimeline([]); setArtifacts([]); setArtifactIndex(0); setExpanded(new Set());
      artifactCounter.current = 0;
      setRunStatus('会话已重置'); setRunning(false); return;
    }
    if (event.type === 'run_started') {
      runStartedAt.current = Date.now(); setRunning(true); setRunStatus('正在运行');
      setMessages((items) => [...items, { role: 'user', content: textValue(data.query), runId: event.runId, timestamp }]);
      addTimeline({ type: event.type, title: '用户输入', detail: textValue(data.query), timestamp, tone: 'info', meta: { flow: 'user' } }); return;
    }
    if (event.type === 'assistant_message') {
      const content = textValue(data.content);
      setMessages((items) => [...items, { role: 'assistant', content, runId: event.runId, timestamp }]);
      addTimeline({ type: event.type, title: 'Agent 回复', detail: content, timestamp, tone: 'info', meta: { flow: 'assistant' } });
      return;
    }
    if (event.type === 'tool_call') {
      const args = parseArguments(data.arguments);
      const name = textValue(data.name, '未知工具');
      const functionName = textValue(data.functionName || args.functionName);
      const resourceId = textValue(data.resourceId || args.resourceId);
      const callId = textValue(data.callId) || undefined;
      const title = name === 'invoke' && functionName ? `工具请求 · ${name} · ${functionName}` : name === 'load_skill' && resourceId ? `工具请求 · ${name} · ${resourceId}` : `工具请求 · ${name}`;
      addTimeline({ type: event.type, title, detail: `参数:\n${prettyArguments(data.arguments)}`, timestamp, tone: 'warning', status: 'running', meta: { callId, toolName: name, resourceId, functionName, step: textValue(data.step), statusLabel: '运行中', flow: 'request' } }, callId);
      return;
    }
    if (event.type === 'tool_result') {
      const result = objectValue(data.result); const status = textValue(result.status); const executionError = result.ok === false && !status; const failed = status === 'failed';
      const callId = textValue(data.callId); const name = textValue(data.functionName || data.name, '未知工具');
      if (callId) setTimeline((items) => items.map((item) => item.id === callId ? { ...item, status: undefined, meta: { ...item.meta, statusLabel: undefined } } : item));
      addTimeline({ type: event.type, title: `工具结果 · ${name}`, detail: jsonText(data.result), timestamp, tone: executionError || failed ? 'error' : 'success', status: executionError ? 'error' : failed ? 'result-error' : 'success', meta: { callId: callId || undefined, toolName: textValue(data.name), functionName: name, step: textValue(data.step), statusLabel: executionError ? '执行错误' : failed ? '结果错误' : '执行成功', flow: executionError ? 'execution-error' : failed ? 'business-error' : 'result' } }, callId ? `${callId}-result` : undefined); return;
    }
    if (event.type === 'upstream_frame') return;
    if (event.type === 'diagnostic') {
      const diagnosticMessage = textValue(data.message, event.error || jsonText(data));
      addTimeline({ type: event.type, title: textValue(data.kind, '诊断'), detail: diagnosticMessage, timestamp, tone: 'error' });
      if (event.error) setConnectionError(event.error);
      return;
    }
    if (event.type === 'artifact_preview') {
      const index = artifactCounter.current++;
      const artifact: ArtifactRecord = {
        ...data,
        runId: event.runId ?? textValue(data.runId, `run-${index + 1}`),
        timestamp,
      };
      setArtifacts((items) => [...items, artifact]);
      setArtifactIndex(index);
      onArtifactRef.current?.(artifact);
      addTimeline({ type: event.type, title: 'Artifact 已生成', detail: event.runId ?? '', timestamp, tone: 'success' }); return;
    }
    if (event.type === 'run_completed' || event.type === 'run_failed' || event.type === 'run_cancelled') {
      setRunning(false); const failed = event.type === 'run_failed'; const cancelled = event.type === 'run_cancelled'; const status = failed ? '运行失败' : cancelled ? '已取消' : `完成 · ${Math.max(0, Date.now() - runStartedAt.current)} ms`; setRunStatus(status); addTimeline({ type: event.type, title: status, detail: jsonText(data), timestamp, tone: failed ? 'error' : 'success' });
    }
  }, [addTimeline]);

  const connect = useCallback(() => {
    socketRef.current?.close(); setConnecting(true); const socket = new WebSocket(resolveSocketUrl(socketPath)); socketRef.current = socket;
    const isCurrentSocket = () => socketRef.current === socket;
    socket.onopen = () => {
      if (!isCurrentSocket()) return;
      setConnected(true); setConnecting(false); setConnectionError('');
      onEventRef.current?.({ direction: 'local', kind: 'connected', payload: { path: socketPath } });
    };
    socket.onclose = () => {
      if (!isCurrentSocket()) return;
      setConnected(false); setConnecting(false);
      onEventRef.current?.({ direction: 'local', kind: 'closed', payload: { path: socketPath } });
    };
    socket.onerror = () => {
      if (!isCurrentSocket()) return;
      setConnected(false); setConnecting(false); setConnectionError('无法连接到调试服务，请确认后端已启动。');
      onEventRef.current?.({ direction: 'local', kind: 'connection_error', payload: { path: socketPath } });
    };
    socket.onmessage = (message) => {
      if (!isCurrentSocket()) return;
      try {
        const event = JSON.parse(message.data) as DebugEvent;
        if (typeof event.type === 'string') processEvent(event);
      } catch {
        setConnectionError('收到无法解析的服务事件。');
      }
    };
  }, [processEvent, socketPath]);

  const refreshHealth = useCallback(() => { fetch(healthPath).then((response) => response.json()).then((value) => setHealth(objectValue(value) as Health)).catch(() => setHealth({ status: 'unavailable' })); }, [healthPath]);
  useEffect(() => { connect(); refreshHealth(); return () => socketRef.current?.close(); }, [connect, refreshHealth]);
  const send = (payload: Record<string, unknown>) => { if (socketRef.current?.readyState === WebSocket.OPEN) { onEventRef.current?.({ direction: 'send', kind: String(payload.type ?? 'message'), payload }); socketRef.current.send(JSON.stringify(payload)); } };
  const submit = (event: FormEvent) => { event.preventDefault(); const value = input.trim(); if (!value || running) return; send({ type: 'message', content: value }); setInput(''); setMobileTab('chat'); };
  const keyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); submit(event); } };
  const currentArtifact = artifacts[artifactIndex] ?? null;
  const availableKinds = useMemo(() => ARTIFACT_KINDS.filter(([kind]) => currentArtifact?.[kind] !== undefined), [currentArtifact]);
  const filteredTimeline = useMemo(() => { const query = timelineSearch.trim().toLocaleLowerCase(); if (!query) return timeline; return timeline.filter((entry) => [entry.title, entry.detail, entry.type, entry.status, entry.meta?.statusLabel, entry.meta?.toolName, entry.meta?.resourceId, entry.meta?.functionName, entry.meta?.callId].filter(Boolean).join(' ').toLocaleLowerCase().includes(query)); }, [timeline, timelineSearch]);
  useEffect(() => { if (availableKinds.length && !availableKinds.some(([kind]) => kind === artifactKind)) setArtifactKind(availableKinds[0][0]); }, [availableKinds, artifactKind]);

  const upstreamUnavailable = health?.upstreamStatus === 'unavailable' || health?.upstreamReachable === false;
  const healthLabel = health?.status !== 'ok' ? '不可用' : upstreamUnavailable ? '工具服务不可用' : '运行正常';

  return <div className="e2e-debug app-shell">
    <header className="e2e-topbar"><div className="brand"><div className="brand-mark">{icon('spark')}</div><div><strong>Main Agent</strong><span>Debug Workbench</span></div></div><div className="session-meta"><span className={`e2e-status-dot ${connected ? 'online' : 'offline'}`} />{connecting ? '连接中' : connected ? '已连接' : '已断开'}<span className="session-id">会话 {sessionId ? sessionId.slice(0, 8) : '等待会话'}</span></div></header>
    {connectionError && <p className="connection-error" role="alert">{connectionError}</p>}
    <div className="mobile-tabs"><button className={mobileTab === 'chat' ? 'active' : ''} onClick={() => setMobileTab('chat')}>对话</button><button className={mobileTab === 'timeline' ? 'active' : ''} onClick={() => setMobileTab('timeline')}>轨迹 <em>{timeline.length}</em></button><button className={mobileTab === 'artifact' ? 'active' : ''} onClick={() => setMobileTab('artifact')}>产物 <em>{artifacts.length}</em></button></div>
    <main className="workspace">
      <aside className={`context-panel ${contextOpen ? 'open' : ''}`}><div className="panel-heading"><div><span className="eyebrow">SESSION CONTEXT</span><h2>运行配置</h2></div><button className="collapse-button" onClick={() => setContextOpen(false)} aria-label="关闭配置">×</button></div><label className="field-label">Skill Profile<select value={profile} onChange={(event) => setProfile(event.target.value)}><option value="">选择 Profile</option>{profiles.map((item) => <option value={item.id} key={item.id} title={item.description}>{item.displayName || item.name || item.id}</option>)}</select></label><div className="skill-card"><div className="skill-icon">{icon('spark')}</div><div><strong>{skillName}</strong><small>{textValue(health?.model, '等待模型信息')}</small></div><span className="check">✓</span></div><div className="field-group"><span className="group-label">设备上下文</span>{[['uid', '用户 ID'], ['deviceId', '设备 ID'], ['phoneType', '手机型号'], ['appVersion', 'App 版本'], ['romVersion', 'ROM 版本']].map(([key, label]) => <label className="field-label compact" key={key}>{label}<input value={context[key as keyof ContextValues]} onChange={(event) => setContext((current) => ({ ...current, [key]: event.target.value }))} /></label>)}</div><div className="context-actions"><button className="secondary-button" onClick={() => send({ type: 'configure', context })}>应用配置</button><button className="ghost-button" onClick={() => send({ type: 'reset', profile: profile || null })}>应用并切换</button></div><div className="health-card"><div><span className="group-label">服务状态</span><strong className={health?.status === 'ok' && !upstreamUnavailable ? 'text-success' : upstreamUnavailable ? 'text-warning' : ''}>{health ? healthLabel : '检查中…'}</strong>{upstreamUnavailable && <small className="health-detail">请启动 8855 工具服务</small>}</div><button className="icon-button small" onClick={refreshHealth} title="刷新健康状态">{icon('refresh')}</button></div></aside>
      <section className={`chat-panel ${mobileTab === 'chat' ? 'mobile-visible' : ''}`}><div className="chat-heading"><div><span className="eyebrow">CONVERSATION</span><h1>创建一张卡片</h1></div><button className="context-toggle" onClick={() => setContextOpen(true)}>{icon('settings')} 配置</button></div><div className="message-list">{messages.length === 0 ? <div className="empty-chat"><div className="empty-icon">{icon('spark')}</div><h3>从一个想法开始</h3><p>描述你想创建的桌面卡片，Agent 会处理能力裁决、生成与校验。</p><div className="suggestions">{quickPrompts.map((item) => <button key={`${item.label}-${item.prompt}`} onClick={() => setInput(item.prompt)}>{item.label}</button>)}</div></div> : messages.map((message, index) => <div className={`message-row ${message.role}`} key={`${message.runId || 'm'}-${index}`}><div className="message-avatar">{message.role === 'assistant' ? icon('spark') : '用户'}</div><div className="message-bubble"><div className="message-meta">{message.role === 'assistant' ? 'Main Agent' : '用户'}<span>{message.runId ? message.runId.slice(0, 8) : ''}</span><time>{formatTime(message.timestamp)}</time></div><div className="message-content">{message.content}</div></div></div>)}</div><div className="composer-wrap"><div className="run-state"><span className={`state-dot ${running ? 'running' : runStatus === '运行失败' ? 'failed' : 'ready'}`} />{runStatus}{running && <button onClick={() => send({ type: 'cancel' })}>取消运行</button>}</div><form className="composer" onSubmit={submit}><textarea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={keyDown} placeholder="描述你想创建的卡片" rows={1} /><button className="send-button" disabled={!connected || running || !input.trim()} aria-label="发送">{icon('send')}</button></form><small className="composer-hint">Enter 发送 · Shift + Enter 换行</small></div></section>
      <aside className={`inspector-panel ${mobileTab !== 'chat' ? 'mobile-visible' : ''}`}><section className={`timeline-section ${mobileTab === 'timeline' ? 'mobile-visible' : ''}`}><div className="section-heading"><div><span className="eyebrow">ACTIVITY</span><h2>运行轨迹</h2></div><span className="counter">{filteredTimeline.length}/{timeline.length}</span></div><label className="timeline-search"><span className="sr-only">搜索运行轨迹</span><input value={timelineSearch} onChange={(event) => setTimelineSearch(event.target.value)} placeholder="搜索工具、resourceId、状态…" /></label><div className="timeline-list">{filteredTimeline.length === 0 ? <div className="small-empty">{timeline.length ? '没有匹配的运行轨迹' : '等待运行事件'}</div> : filteredTimeline.map((entry) => { const isExpanded = expanded.has(entry.id); const statusLabel = entry.meta?.statusLabel; const functionName = entry.meta?.functionName || ''; return <article className={`timeline-item ${isExpanded ? 'is-expanded' : ''}`} key={entry.id}><button className="timeline-item__header" type="button" aria-expanded={isExpanded} onClick={() => setExpanded((current) => { const next = new Set(current); next.has(entry.id) ? next.delete(entry.id) : next.add(entry.id); return next; })}><span className={`timeline-icon ${entry.tone}`}>{timelineGlyph(entry)}</span><span className="timeline-item__title"><strong>{entry.title}</strong>{functionName && <span className="timeline-inline-value" title={functionName}>{functionName}</span>}{statusLabel && <span className={`timeline-status ${entry.status || ''}`}>{statusLabel}</span>}</span><span className="timeline-item__chevron">{isExpanded ? '⌃' : '⌄'}</span></button><div className="timeline-item__body">{entry.meta && <div className="timeline-meta">{entry.meta.resourceId && <span>resourceId <b>{entry.meta.resourceId}</b></span>}{entry.meta.functionName && <span>functionName <b>{entry.meta.functionName}</b></span>}{entry.meta.callId && <span>callId <b>{entry.meta.callId.slice(0, 8)}</b></span>}{entry.meta.step && <span>step <b>{entry.meta.step}</b></span>}</div>}{entry.detail && <pre>{entry.detail}</pre>}<time>{formatTime(entry.timestamp)}</time></div></article>; })}</div></section><section className={`artifact-section ${mobileTab === 'artifact' ? 'mobile-visible' : ''}`}><div className="section-heading"><div><span className="eyebrow">OUTPUT</span><h2>Artifact 检查器</h2></div><span className="counter">{artifacts.length}</span></div>{currentArtifact ? <><div className="artifact-runs">{artifacts.map((artifact, index) => { const runId = textValue(artifact.runId, `run-${index + 1}`); return <button className={index === artifactIndex ? 'active' : ''} onClick={() => setArtifactIndex(index)} key={`${runId}-${index}`}>Run {index + 1}<small>{runId.slice(0, 8)}</small></button>; })}</div><div className="artifact-toolbar"><span>{ARTIFACT_KINDS.find(([kind]) => kind === artifactKind)?.[1] || artifactKind}</span><button onClick={() => navigator.clipboard?.writeText(jsonText(currentArtifact[artifactKind]))} title="复制">{icon('copy')}</button><button onClick={() => { const url = URL.createObjectURL(new Blob([jsonText(currentArtifact[artifactKind])], { type: 'application/json' })); const anchor = document.createElement('a'); anchor.href = url; anchor.download = `${artifactKind}.json`; anchor.click(); URL.revokeObjectURL(url); }} title="下载">{icon('download')}</button></div><ArtifactPreview artifact={currentArtifact} activeKind={artifactKind} onKindChange={setArtifactKind} /></> : <div className="artifact-empty"><div className="artifact-placeholder">{icon('spark')}</div><strong>暂无成功产物</strong><span>完成一次运行后，生成的 CardSpec 和 GenUI 会显示在这里。</span></div>}</section></aside>
    </main>
  </div>;
}
