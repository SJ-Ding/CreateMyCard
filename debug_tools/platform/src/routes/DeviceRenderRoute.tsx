import { useEffect, useMemo, useRef, useState } from 'react';
import { CardRenderer } from '@widget-debug/card-renderer';
import {
  DeviceApiError, deviceRequest, listDeviceProfiles, terminal,
  type Check, type Crop, type DeviceProfile, type DeviceRun, type Emulator,
  type EnvironmentStatus, type Operation, type Target,
} from '../deviceApi';
import { DEFAULT_ASSET_BASE_URL } from '../config';
import './device-render.css';

const STORAGE = 'ai-widget-device-editor:v1';
const LAST_RUN = 'ai-widget-device-run:v1';
type Editor = { source: string; size: '2x2' | '2x4' };
type Resolution = { width: number; height: number };
type Drag = { kind: 'create' | 'move' | 'resize'; x: number; y: number; crop?: Crop };

function restore(): Editor {
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE) ?? '{}');
    return { source: typeof value.source === 'string' ? value.source : '', size: value.size === '2x4' ? '2x4' : '2x2' };
  } catch { return { source: '', size: '2x2' }; }
}

function CropPreview({ image, crop }: { image: string; crop?: Crop }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const element = canvas.current;
    if (!element || !image || !crop) return;
    const source = new Image();
    source.onload = () => {
      element.width = crop.width; element.height = crop.height;
      element.getContext('2d')?.drawImage(source, crop.x, crop.y, crop.width, crop.height, 0, 0, crop.width, crop.height);
    };
    source.src = image;
  }, [crop, image]);
  return <div className="device-crop-preview">
    {image && crop ? <canvas ref={canvas} aria-label="当前裁切结果" /> : <p>框选设备画面后预览裁切结果</p>}
  </div>;
}

export function DeviceRenderRoute() {
  const [editor, setEditor] = useState(restore);
  const [targets, setTargets] = useState<Target[]>([]);
  const [emulators, setEmulators] = useState<Emulator[]>([]);
  const [checks, setChecks] = useState<Check[]>([]);
  const [statusItems, setStatusItems] = useState<EnvironmentStatus[]>([]);
  const [profiles, setProfiles] = useState<DeviceProfile[]>([]);
  const [profileId, setProfileId] = useState('');
  const [profileName, setProfileName] = useState('');
  const [profileVersion, setProfileVersion] = useState<number | null>(null);
  const [waitSeconds, setWaitSeconds] = useState(8);
  const [crops, setCrops] = useState<Partial<Record<'2x2' | '2x4', Crop>>>({});
  const [savedSignature, setSavedSignature] = useState('');
  const [selected, setSelected] = useState('');
  const [offlineDevice, setOfflineDevice] = useState('');
  const [error, setError] = useState('');
  const [operation, setOperation] = useState<Operation | null>(null);
  const [converted, setConverted] = useState('');
  const [convertError, setConvertError] = useState('');
  const [busy, setBusy] = useState(false);
  const [runId, setRunId] = useState(() => localStorage.getItem(LAST_RUN) ?? '');
  const [run, setRun] = useState<DeviceRun | null>(null);
  const [logs, setLogs] = useState('');
  const [paused, setPaused] = useState(false);
  const [visible, setVisible] = useState(!document.hidden);
  const [image, setImage] = useState('');
  const [screenStatus, setScreenStatus] = useState('请选择设备');
  const [frameInfo, setFrameInfo] = useState('');
  const [resolution, setResolution] = useState<Resolution | null>(null);
  const [connection, setConnection] = useState(0);
  const [drag, setDrag] = useState<Drag | null>(null);
  const dragRef = useRef<Drag | null>(null);
  const importRef = useRef<HTMLInputElement>(null);
  const screenRef = useRef<HTMLDivElement>(null);
  useEffect(() => { dragRef.current = drag; }, [drag]);

  const selectedTarget = targets.find((target) => target.id === selected);
  const crop = crops[editor.size];
  const profileSignature = JSON.stringify({
    profileName, selected: selectedTarget?.serial ?? offlineDevice, waitSeconds, crops, resolution,
  });
  const dirty = savedSignature !== '' && profileSignature !== savedSignature;

  async function refresh() {
    const results = await Promise.allSettled([
      deviceRequest<{ targets: Target[]; emulators: Emulator[] }>('/targets'),
      deviceRequest<{ checks: Check[]; status: EnvironmentStatus[] }>('/environment'),
      listDeviceProfiles(),
    ]);
    const devices = results[0];
    if (devices.status === 'fulfilled') {
      setTargets(devices.value.targets); setEmulators(devices.value.emulators);
      setSelected((old) => devices.value.targets.some((target) => target.id === old) ? old : '');
    } else setError(String(devices.reason));
    const environment = results[1];
    if (environment.status === 'fulfilled') {
      setChecks(environment.value.checks);
      const discoveredTargets = devices.status === 'fulfilled' ? devices.value.targets : [];
      const discoveredEmulators = devices.status === 'fulfilled' ? devices.value.emulators : [];
      const physical = discoveredTargets.filter((target) => target.kind === 'physical').length;
      const unknown = discoveredTargets.filter((target) => target.kind === 'unknown').length;
      const onlineEmulators = discoveredTargets.filter((target) => target.kind === 'emulator').length;
      const startable = discoveredEmulators.filter((emulator) => emulator.ready).length;
      setStatusItems(environment.value.status.map((item) => {
        if (item.id === 'physical') return {
          ...item, ok: physical + unknown > 0,
          detail: `在线 ${physical} 台${unknown ? `，另有 ${unknown} 台类型待确认` : ''}`,
        };
        if (item.id === 'emulator') return {
          ...item, ok: onlineEmulators + startable > 0,
          detail: `在线 ${onlineEmulators} 台，可启动 ${startable} 个`,
        };
        return item;
      }));
    } else setError(String(environment.reason));
    if (results[2].status === 'fulfilled') setProfiles(results[2].value);
  }

  useEffect(() => { void refresh(); }, []);
  useEffect(() => {
    if (!visible) return;
    const timer = window.setInterval(() => { void refresh(); }, 20000);
    return () => window.clearInterval(timer);
  }, [visible]);
  useEffect(() => {
    try { localStorage.setItem(STORAGE, JSON.stringify(editor)); }
    catch { setError('浏览器存储空间不足，编辑内容未保存'); }
  }, [editor]);
  useEffect(() => {
    const onVisibility = () => setVisible(!document.hidden);
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, []);

  useEffect(() => {
    const source = editor.source.trim();
    if (!source) { setConverted(''); setConvertError(''); return; }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      deviceRequest<{ text: string }>('/convert', { source: editor.source, format: 'auto', size: editor.size }, {
        signal: controller.signal,
      }).then((result) => { setConverted(result.text); setConvertError(''); }).catch((reason) => {
        if (controller.signal.aborted) return;
        setConverted(''); setConvertError(reason instanceof Error ? reason.message : String(reason));
      });
    }, 400);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [editor.source, editor.size]);

  useEffect(() => {
    if (!operation || operation.status !== 'running') return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const next = await deviceRequest<Operation>(`/operations/${operation!.id}`);
        if (disposed) return;
        setOperation(next);
        if (next.status !== 'running') { await refresh(); return; }
      } catch (reason) { if (!disposed) setError(String(reason)); }
      if (!disposed) timer = setTimeout(poll, 2000);
    }
    timer = setTimeout(poll, 2000);
    return () => { disposed = true; clearTimeout(timer); };
  }, [operation?.id, operation?.status]);

  useEffect(() => {
    if (!runId) return;
    let disposed = false; let offset = 0; let timer: ReturnType<typeof setTimeout>;
    setLogs('');
    async function poll() {
      try {
        const [state, page] = await Promise.all([
          deviceRequest<DeviceRun>(`/runs/${runId}`),
          deviceRequest<{ text: string; offset: number }>(`/runs/${runId}/logs?offset=${offset}`),
        ]);
        if (disposed) return;
        setRun(state); offset = page.offset; setLogs((previous) => (previous + page.text).slice(-200000));
        if (terminal.has(state.status) && page.text.length < 32000) return;
      } catch (reason) {
        if (disposed) return;
        setError(String(reason));
        if (reason instanceof DeviceApiError && reason.status === 404) {
          setRunId(''); setRun(null); localStorage.removeItem(LAST_RUN); return;
        }
      }
      if (!disposed) timer = setTimeout(poll, 1000);
    }
    void poll();
    return () => { disposed = true; clearTimeout(timer); };
  }, [runId]);

  useEffect(() => {
    if (!selected || paused || !visible) {
      setScreenStatus(selected ? '预览已暂停' : offlineDevice ? '配置设备当前离线' : '请选择设备'); return;
    }
    let currentUrl = ''; let disposed = false; let socket: WebSocket | null = null;
    let retryTimer: number | undefined; let attempts = 0;
    const connect = () => {
      if (disposed) return;
      const url = new URL(`/debug/device/targets/${selected}/screen`, window.location.href);
      url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
      socket = new WebSocket(url); socket.binaryType = 'blob';
      setScreenStatus(attempts ? `正在重新连接（${attempts}/5）` : '正在连接设备屏幕');
      socket.onopen = () => setScreenStatus('已连接，等待设备画面');
      socket.onmessage = (event) => {
        if (disposed) return;
        if (typeof event.data === 'string') {
          const info = JSON.parse(event.data);
          if (info.status === 'error') setScreenStatus(`画面已过期：${info.message}`);
          else if (info.status === 'waiting') setScreenStatus(info.message);
          else if (info.status === 'frame') {
            attempts = 0;
            setScreenStatus('实时预览 · 目标 1fps');
            setFrameInfo(`${new Date(info.capturedAt).toLocaleTimeString()} · ${info.fps} fps`);
            setResolution({ width: info.width, height: info.height });
          }
        } else {
          if (dragRef.current) return;
          if (currentUrl) URL.revokeObjectURL(currentUrl);
          currentUrl = URL.createObjectURL(event.data); setImage(currentUrl);
        }
      };
      socket.onerror = () => setScreenStatus('屏幕连接失败，正在等待重连');
      socket.onclose = () => {
        if (disposed) return;
        attempts += 1;
        if (attempts > 5) { setScreenStatus('连接已断开，请手动重新连接'); return; }
        retryTimer = window.setTimeout(connect, Math.min(1000 * (2 ** (attempts - 1)), 15000));
      };
    };
    connect();
    return () => {
      disposed = true; socket?.close();
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
      if (currentUrl) URL.revokeObjectURL(currentUrl);
    };
  }, [selected, paused, visible, connection]);

  async function action(work: () => Promise<void>) {
    setError(''); setBusy(true);
    try { await work(); } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  }

  function point(event: React.PointerEvent): { x: number; y: number } | null {
    const element = screenRef.current?.querySelector('img');
    if (!element || !resolution) return null;
    const rect = element.getBoundingClientRect();
    const x = Math.round((event.clientX - rect.left) * resolution.width / rect.width);
    const y = Math.round((event.clientY - rect.top) * resolution.height / rect.height);
    return { x: Math.max(0, Math.min(resolution.width, x)), y: Math.max(0, Math.min(resolution.height, y)) };
  }

  function beginCrop(event: React.PointerEvent) {
    const current = point(event);
    if (!current || !resolution) return;
    const existing = crops[editor.size];
    const inside = existing && current.x >= existing.x && current.x <= existing.x + existing.width
      && current.y >= existing.y && current.y <= existing.y + existing.height;
    const nearHandle = inside && existing && Math.abs(current.x - existing.x - existing.width) < 30
      && Math.abs(current.y - existing.y - existing.height) < 30;
    setDrag({ kind: nearHandle ? 'resize' : inside ? 'move' : 'create', ...current, crop: existing });
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function updateCrop(event: React.PointerEvent) {
    const current = point(event);
    if (!current || !drag || !resolution) return;
    let next: Crop;
    if (drag.kind === 'move' && drag.crop) {
      next = { ...drag.crop,
        x: Math.max(0, Math.min(resolution.width - drag.crop.width, drag.crop.x + current.x - drag.x)),
        y: Math.max(0, Math.min(resolution.height - drag.crop.height, drag.crop.y + current.y - drag.y)) };
    } else if (drag.kind === 'resize' && drag.crop) {
      next = { ...drag.crop,
        width: Math.max(1, Math.min(resolution.width - drag.crop.x, drag.crop.width + current.x - drag.x)),
        height: Math.max(1, Math.min(resolution.height - drag.crop.y, drag.crop.height + current.y - drag.y)) };
    } else {
      next = { x: Math.min(drag.x, current.x), y: Math.min(drag.y, current.y),
        width: Math.max(1, Math.abs(current.x - drag.x)), height: Math.max(1, Math.abs(current.y - drag.y)) };
    }
    setCrops((value) => ({ ...value, [editor.size]: next }));
  }

  function loadProfile(nextId: string) {
    if (dirty && !window.confirm('当前配置有未保存修改，确定加载其他配置吗？')) return;
    const profile = profiles.find((item) => item.id === nextId);
    setProfileId(nextId);
    if (!profile) return;
    const online = targets.find((target) => target.serial === profile.serial);
    setSelected(online?.id ?? ''); setOfflineDevice(online ? '' : profile.serial);
    setProfileName(profile.name); setProfileVersion(profile.version); setWaitSeconds(profile.renderWaitSeconds);
    setCrops(profile.crops); setResolution(profile.resolution);
    setSavedSignature(JSON.stringify({ profileName: profile.name, selected: profile.serial,
      waitSeconds: profile.renderWaitSeconds, crops: profile.crops, resolution: profile.resolution }));
  }

  async function saveProfile(copy: boolean) {
    if (!selectedTarget || !resolution) throw new Error('请先选择在线设备并取得一帧设备画面');
    if (!profileName.trim()) throw new Error('请输入配置名称');
    const emulator = emulators.find((item) => item.serial === selectedTarget.serial);
    const value = { ...(copy ? {} : { id: profileId || undefined, version: profileVersion ?? undefined }),
      name: profileName.trim(), serial: selectedTarget.serial, deviceName: selectedTarget.name,
      kind: selectedTarget.kind, emulatorId: emulator?.id ?? null, resolution, crops, renderWaitSeconds: waitSeconds };
    const saved = copy || !profileId
      ? await deviceRequest<DeviceProfile>('/profiles', value)
      : await deviceRequest<DeviceProfile>(`/profiles/${profileId}`, value, { method: 'PUT' });
    setProfileId(saved.id); setProfileVersion(saved.version); setOfflineDevice('');
    setProfiles(await listDeviceProfiles());
    setSavedSignature(JSON.stringify({ profileName: saved.name, selected: saved.serial,
      waitSeconds: saved.renderWaitSeconds, crops: saved.crops, resolution: saved.resolution }));
  }

  const overlayStyle = useMemo(() => crop && resolution ? {
    left: `${crop.x * 100 / resolution.width}%`, top: `${crop.y * 100 / resolution.height}%`,
    width: `${crop.width * 100 / resolution.width}%`, height: `${crop.height * 100 / resolution.height}%`,
  } : undefined, [crop, resolution]);
  const running = runId !== '' && (!run || !terminal.has(run.status));

  return <div className="device-workspace">
    <header className="device-header"><div><h2>设备渲染</h2><p>浏览器预览、设备部署与截图区域标定</p></div>
      <button disabled={busy} onClick={() => void action(refresh)}>刷新设备与环境</button></header>
    {error && <div role="alert" className="device-error">{error}</div>}
    <aside className="device-status-widget" aria-label="设备环境状态">
      <div className="device-status-heading"><strong>设备环境</strong><small>每 20 秒自动检查</small></div>
      <div className="backend-status-items">{statusItems.map((item) => <div key={item.id}
        className={`backend-status-item ${item.ok ? 'available' : 'unavailable'}`} title={item.detail}>
        <i aria-hidden="true" /><span>{item.label}</span><b>{item.ok ? '可用' : '不可用'}</b></div>)}</div>
      <details><summary>查看检查明细</summary>{checks.map((check) => <div key={check.name}>
        <strong>{check.ok ? '✓' : '！'} {check.name}</strong> · {check.detail}</div>)}</details>
    </aside>
    <section className="device-profile-bar">
      <label>已保存配置 <select aria-label="已保存设备配置" value={profileId}
        onChange={(event) => loadProfile(event.target.value)}><option value="">新建配置</option>
        {profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>)}</select></label>
      <label>配置名称 <input value={profileName} maxLength={80} onChange={(event) => setProfileName(event.target.value)} /></label>
      <label>渲染等待 <input type="number" min="0" max="120" step="0.5" value={waitSeconds}
        onChange={(event) => setWaitSeconds(Number(event.target.value))} /> 秒</label>
      <button disabled={busy || !selectedTarget || !profileName.trim()} onClick={() => void action(() => saveProfile(false))}>保存</button>
      <button disabled={busy || !selectedTarget || !profileName.trim()} onClick={() => void action(() => saveProfile(true))}>另存为</button>
      {dirty && <small>有未保存修改</small>}
    </section>
    <div className="device-target-bar"><label>在线设备 <select aria-label="在线设备" value={selected}
      onChange={(event) => { setSelected(event.target.value); setOfflineDevice(''); setImage(''); }}>
      <option value="">{offlineDevice ? `配置设备离线 · ${offlineDevice}` : '请选择目标设备'}</option>
      {targets.map((target) => <option key={target.id} value={target.id}>{target.name} · {target.serial} · {
        target.kind === 'emulator' ? '模拟器' : target.kind === 'physical' ? '真机' : '类型待确认'}</option>)}</select></label>
      <div className="device-emulators">{emulators.length === 0 && <span>未发现本地虚拟机</span>}
        {emulators.map((emulator) => <button key={emulator.id} disabled={busy || !emulator.ready || operation?.status === 'running'}
          title={emulator.ready ? emulator.path : '镜像缺失，请检查本机配置'} onClick={() => void action(async () => setOperation(
            await deviceRequest<Operation>(`/emulators/${emulator.id}/start`, {})))}>启动 {emulator.name}</button>)}</div></div>
    {operation && <p role="status">{operation.message}</p>}
    <div className="device-columns">
      <section className="device-editor"><div className="device-toolbar"><label>尺寸 <select aria-label="卡片尺寸" value={editor.size}
        onChange={(event) => setEditor({ ...editor, size: event.target.value as Editor['size'] })}>
        <option value="2x2">2×2</option><option value="2x4">2×4</option></select></label>
        <button onClick={() => importRef.current?.click()}>导入代码</button><input ref={importRef} type="file" hidden
          accept=".json,.jsonl,.txt,.dat" onChange={(event) => { const file = event.target.files?.[0];
            if (file) void action(async () => { if (file.size > 2000000) throw new Error('文件不能超过 2 MB');
              const source = await file.text();
              setEditor((previous) => ({ ...previous, source })); }); event.target.value = ''; }} /></div>
        <textarea aria-label="A2UI 代码" spellCheck={false} value={editor.source}
          placeholder="粘贴标准 A2UI 消息或 Compact DSL，将自动识别并预览"
          onChange={(event) => setEditor({ ...editor, source: event.target.value })} />
        {convertError && <p className="device-inline-error" role="alert">{convertError}</p>}
        <div className="device-toolbar"><span>{converted ? '已自动识别并转换' : '等待有效输入'}</span>
          <button disabled={busy || running || !selected || !converted} onClick={() => void action(async () => {
            const result = await deviceRequest<DeviceRun>('/runs', { source: editor.source, format: 'auto', size: editor.size, targetId: selected });
            setRun(result); setRunId(result.id); localStorage.setItem(LAST_RUN, result.id); })}>编译并运行</button></div>
        {converted && <details><summary>实际写入工程的 A2UI</summary><pre>{converted}</pre></details>}</section>
      <section className="device-browser-preview"><div className="device-panel-title"><strong>浏览器渲染效果</strong>
        <small>与设备部署使用同一转换结果</small></div><CardRenderer key={`${editor.size}:${converted}`}
          initialValue={converted} assetBaseUrl={DEFAULT_ASSET_BASE_URL} cardSize={editor.size} previewOnly zoom={160} /></section>
      <section className="device-preview"><div className="device-toolbar"><strong>{screenStatus}</strong><div>
        <button disabled={!selected} onClick={() => setPaused(!paused)}>{paused ? '恢复' : '暂停'}</button>
        <button disabled={!selected} onClick={() => setConnection((value) => value + 1)}>重新连接</button></div></div>
        <small>{frameInfo || (resolution ? `${resolution.width} × ${resolution.height}` : '暂无画面')}</small>
        <div ref={screenRef} className="device-screen" onPointerDown={beginCrop} onPointerMove={(event) => { if (drag) updateCrop(event); }}
          onPointerUp={() => setDrag(null)} onPointerCancel={() => setDrag(null)}>{image ? <div className="device-screen-image">
            <img src={image} alt="设备当前屏幕" draggable={false} />{overlayStyle && <div className="device-crop-box" style={overlayStyle}>
              <span>{editor.size}</span><i /></div>}</div> : <p>选择在线设备后显示全屏画面</p>}</div>
        {crop && <div className="device-crop-fields">{(['x', 'y', 'width', 'height'] as const).map((key) => <label key={key}>{key}
          <input type="number" min={key === 'width' || key === 'height' ? 1 : 0} value={crop[key]} onChange={(event) => setCrops((value) => ({
            ...value, [editor.size]: { ...crop, [key]: Math.max(key === 'width' || key === 'height' ? 1 : 0, Number(event.target.value)) },
          }))} /></label>)}</div>}<CropPreview image={image} crop={crop} /></section>
    </div>
    {run && <section className="device-task"><div className="device-toolbar"><strong>任务 · {run.device} · {run.stage}</strong>
      {!terminal.has(run.status) && <button onClick={() => void action(async () => setRun(
        await deviceRequest<DeviceRun>(`/runs/${run.id}/cancel`, {})))}>取消任务</button>}</div>
      {run.error && <p role="alert">{run.error}</p>}{run.status === 'success' && <p>应用已启动，请在设备画面中确认实际效果。</p>}
      <details open={run.status === 'failed'}><summary>构建日志</summary><pre aria-label="构建日志">{logs || '等待构建日志…'}</pre></details></section>}
  </div>;
}
