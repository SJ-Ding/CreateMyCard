import { useEffect, useMemo, useRef, useState } from 'react';
import { useRendererDebug } from './RendererDebug';
import { artifactObject, inferGroups, readPointer, record, safeDataPath, schemaValue, validateValue,
  type MockCatalog, type MockGroup, type Schema } from './mock-data';

export const MOCK_APPLY_DELAY = 400;

function fieldName(path: string): string {
  const name = path.split('/').pop() || path;
  return /^\d+$/.test(name) ? `[${name}]` : name;
}

function ValueField({ schema, value, path, onChange }: {
  schema: Schema; value: unknown; path: string; onChange: (value: unknown) => void;
}) {
  if (schema.type === 'object' && value !== null) {
    return <fieldset><legend title={path}>{fieldName(path)}</legend>
      {Object.entries(schema.properties ?? {}).map(([key, child]) => <ValueField key={key} schema={child}
        value={record(value)[key]} path={`${path}/${key}`} onChange={next => onChange({ ...record(value), [key]: next })} />)}
    </fieldset>;
  }
  if (schema.type === 'array' && Array.isArray(value)) {
    return <fieldset><legend title={path}>{fieldName(path)}</legend>{value.map((item, index) => (
      <div key={index}><ValueField schema={schema.items ?? {}} value={item} path={`${path}/${index}`}
        onChange={next => onChange(value.map((old, at) => at === index ? next : old))} />
      <button type="button" onClick={() => onChange(value.filter((_old, at) => at !== index))}>删除第 {index + 1} 项</button></div>
    ))}<button type="button" onClick={() => onChange([...value, schemaValue(schema.items ?? {})])}>添加条目</button></fieldset>;
  }
  return <label className="renderer-mock-field"><span title={path}>{fieldName(path)}</span>
    {value === null ? <span>null（可在 JSON 中修改）</span>
      : schema.enum ? <select aria-label={path} value={JSON.stringify(value)} onChange={event => onChange(JSON.parse(event.target.value))}>
        {schema.enum.map((item, index) => <option key={index} value={JSON.stringify(item)}>{String(item)}</option>)}
      </select> : schema.type === 'boolean' ? <input aria-label={path} type="checkbox" checked={value === true}
        onChange={event => onChange(event.target.checked)} />
        : <input aria-label={path} type={schema.type === 'number' || schema.type === 'integer' ? 'number' : 'text'}
          step={schema.type === 'integer' ? 1 : 'any'} min={schema.minimum} max={schema.maximum}
          value={typeof value === 'number' || typeof value === 'string' ? value : ''}
          onChange={event => onChange(event.target.type === 'number'
            ? event.target.value === '' ? '' : Number(event.target.value) : event.target.value)} />}
    </label>;
}

function MockEditor({ group, catalog, changeGroup }: {
  group: MockGroup; catalog: MockCatalog; changeGroup: (group: MockGroup) => void;
}) {
  const debug = useRendererDebug()!;
  const definition = catalog.capabilities.find(item => item.id === group.capabilityId);
  const original = debug.document ? readPointer(debug.document.data, group.path) : undefined;
  const [draft, setDraft] = useState('');
  const [jsonMode, setJsonMode] = useState(false);
  const [message, setMessage] = useState('');
  const [pathDraft, setPathDraft] = useState(group.path);
  const [pending, setPending] = useState(false);
  const updates = useRef(debug.updates);
  updates.current = debug.updates;
  const update = debug.update;
  useEffect(() => {
    setDraft(JSON.stringify(definition ? schemaValue(definition.outputSchema, original) : original ?? {}, null, 2));
    setMessage(''); setPending(false);
  }, [definition, original, group.path]);
  const parsed = useMemo(() => {
    try { return { value: JSON.parse(draft), error: '' }; }
    catch { return { value: undefined, error: 'JSON 格式不合法' }; }
  }, [draft]);
  const changeDraft = (value: string) => {
    setDraft(value); setPending(true); setMessage('等待自动刷新…');
  };
  useEffect(() => {
    if (!pending || !definition) return;
    const timer = window.setTimeout(() => {
      setPending(false);
      if (parsed.error) { setMessage(parsed.error); return; }
      const errors = validateValue(parsed.value, definition.outputSchema, group.path);
      if (errors.length) { setMessage(errors.join('\n')); return; }
      const conflicting = updates.current.some(item => item.path !== group.path && (
        item.path.startsWith(group.path + '/') || group.path.startsWith(item.path + '/')
      ));
      if (conflicting) { setMessage('写入路径与已应用的模拟数据重叠，请先恢复原始数据'); return; }
      update(current => [...current.filter(item => item.path !== group.path), { path: group.path, value: parsed.value }]);
      setMessage('已自动刷新');
    }, MOCK_APPLY_DELAY);
    return () => window.clearTimeout(timer);
  }, [definition, group.path, parsed, pending, update]);
  return <section className="renderer-mock-group"><h3>{group.path}</h3>
    <label>数据接口<select aria-label={`${group.path} 数据接口`} value={group.capabilityId}
      disabled={group.declared} onChange={event => changeGroup({ ...group, capabilityId: event.target.value })}>
      <option value="">请选择接口</option>{catalog.capabilities.map(item => (
        <option key={item.id} value={item.id}>{item.id}{group.candidates.includes(item.id) ? ' · 匹配候选' : ''}</option>
      ))}</select></label>
    {!group.declared && <div className="renderer-mock-path"><input aria-label={`${group.path} 写入路径`}
      value={pathDraft} onChange={event => setPathDraft(event.target.value)} /><button type="button" onClick={() => {
        if (!safeDataPath(pathDraft)) { setMessage('写入路径必须位于 /data/ 下且不能包含危险字段'); return; }
        changeGroup({ ...group, path: pathDraft });
      }}>确认路径</button></div>}
    {!definition && <p role="status">{group.declared ? '该接口不在当前注册表中，请选择对应版本。' : '无法唯一识别接口，请选择匹配候选或其他注册接口。'}</p>}
    {definition && <>
      <div className="renderer-debug-actions"><button type="button" onClick={() => setJsonMode(!jsonMode)}>
        {jsonMode ? '切换为表单' : '切换为 JSON'}</button><button type="button" onClick={() => {
        changeDraft(JSON.stringify(schemaValue(definition.outputSchema), null, 2));
      }}>填入接口样例</button></div>
      {jsonMode || parsed.error ? <textarea aria-label={`${group.path} 模拟 JSON`} value={draft}
        onChange={event => changeDraft(event.target.value)} />
        : <ValueField schema={definition.outputSchema} value={parsed.value} path={group.path}
          onChange={value => changeDraft(JSON.stringify(value, null, 2))} />}
      <div className="renderer-debug-actions">
        <button type="button" onClick={() => {
          setPending(false);
          update(current => current.filter(item => item.path !== group.path));
          setDraft(JSON.stringify(schemaValue(definition.outputSchema, original), null, 2));
          setMessage('已恢复原始预览数据');
        }}>恢复原始数据</button></div></>}
    {message && <p role="status" className="renderer-mock-message">{message}</p>}
  </section>;
}

export function MockDataPanel({ active }: { active: boolean }) {
  const debug = useRendererDebug()!;
  const [catalog, setCatalog] = useState<MockCatalog | null>(null);
  const [versions, setVersions] = useState<string[]>([]);
  const [manualVersion, setManualVersion] = useState('');
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const [groups, setGroups] = useState<MockGroup[]>([]);
  const [started, setStarted] = useState(false);
  useEffect(() => { if (active) setStarted(true); }, [active]);
  const artifact = useMemo(() => ({ ...artifactObject(debug.artifact), ...artifactObject(debug.source) }),
    [debug.artifact, debug.source]);
  const registryVersion = record(artifact.meta).capabilityRegistryVersion;
  const device = record(record(debug.request).deviceInfo);
  const romVersion = typeof device.romVersion === 'string' ? device.romVersion : undefined;
  useEffect(() => {
    if (!started) return;
    const controller = new AbortController();
    const params = new URLSearchParams();
    if (manualVersion || typeof registryVersion === 'string') params.set('registryVersion', manualVersion || String(registryVersion));
    else {
      if (debug.appVersion) params.set('appVersion', debug.appVersion);
      if (romVersion) params.set('romVersion', romVersion);
    }
    setCatalog(null); setError('正在读取模拟数据定义…');
    fetch(`/debug/renderer/mock-catalog?${params}`, { signal: controller.signal }).then(async response => {
      const body = await response.json();
      if (controller.signal.aborted) return;
      const available = body.availableVersions ?? body.detail?.availableVersions ?? [];
      setVersions(available);
      if (!response.ok) throw new Error(body.detail?.message ?? body.detail ?? '读取失败');
      setCatalog(body); setError('');
    }).catch(reason => { if (!controller.signal.aborted) setError(String(reason)); });
    return () => controller.abort();
  }, [started, registryVersion, manualVersion, debug.appVersion, romVersion, retry]);
  useEffect(() => {
    setGroups(debug.document && catalog ? inferGroups(debug.document, artifact, catalog) : []);
  }, [debug.document, catalog, artifact]);
  return <div><p>仅修改本地预览，不调用真实数据接口。</p>
    <label>定义版本<select aria-label="模拟数据定义版本" value={manualVersion}
      onChange={event => { setManualVersion(event.target.value); debug.update([]); }}>
      <option value="">自动选择</option>{versions.map(version => <option key={version}>{version}</option>)}
    </select></label>
    {catalog && <p>{catalog.registryVersion} · 来源：{manualVersion ? '手动选择' : catalog.source === 'default'
      ? '服务默认配置' : catalog.source === 'request' ? '原请求版本' : '原产物版本'}</p>}
    {error && <p role="status">{error}<button type="button" onClick={() => setRetry(value => value + 1)}>重试</button></p>}
    {catalog && groups.map((group, index) => <MockEditor key={`${index}:${group.path}:${group.capabilityId}`} group={group}
      catalog={catalog} changeGroup={next => {
        const conflicting = groups.some((item, at) => at !== index && (item.path === next.path
          || item.path.startsWith(next.path + '/') || next.path.startsWith(item.path + '/')));
        if (conflicting) { setError('写入路径与其他数据组重叠，请选择独立路径'); return; }
        setError('');
        debug.update(debug.updates.filter(item => item.path !== group.path));
        setGroups(current => current.map((item, at) => at === index ? next : item));
      }} />)}
    {catalog && groups.length === 0 && <p>未识别到动态数据绑定。</p>}
  </div>;
}
