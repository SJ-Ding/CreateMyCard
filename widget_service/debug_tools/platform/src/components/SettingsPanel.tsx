import { useEffect, useState } from 'react';
import { useWorkbench } from '../context';
import { endpointHasCredentials, websocketUrl } from '../config';
import type { DebugConfig } from '../types';

type TextFieldProps = {
  label: string;
  value: string | number;
  onChange: (value: string) => void;
  help?: string;
  type?: 'text' | 'number';
};

function TextField({ label, value, onChange, help, type = 'text' }: TextFieldProps) {
  return (
    <label className="settings-field">
      <span>{label}</span>
      <input type={type} value={value} onChange={(event) => onChange(event.target.value)} />
      {help && <small>{help}</small>}
    </label>
  );
}

export function SettingsPanel() {
  const { config, updateConfig, resetConfig } = useWorkbench();
  const [draft, setDraft] = useState<DebugConfig>(config);
  const [saved, setSaved] = useState(false);
  const [validationError, setValidationError] = useState('');

  useEffect(() => {
    setDraft(config);
  }, [config]);

  const setValue = <K extends keyof DebugConfig>(key: K, value: DebugConfig[K]) => {
    setDraft((current) => ({ ...current, [key]: value }));
  };

  const save = () => {
    if (!draft.agentWsUrl.trim() || !draft.toolWsBaseUrl.trim()) {
      setValidationError('Agent 和微服务地址不能为空。');
      setSaved(false);
      return;
    }
    if (endpointHasCredentials(draft.agentWsUrl) || endpointHasCredentials(draft.toolWsBaseUrl)) {
      setValidationError('地址不能包含认证信息或 hash；认证信息不会保存。');
      setSaved(false);
      return;
    }
    try {
      // 在保存时就校验协议、相对路径和 HTTPS 混合内容，避免把一个只会在
      // 发起调用时才失败的地址写入共享配置。
      websocketUrl(draft.agentWsUrl);
      websocketUrl(draft.toolWsBaseUrl);
    } catch (error) {
      setValidationError(`地址无效：${error instanceof Error ? error.message : String(error)}`);
      setSaved(false);
      return;
    }
    updateConfig(draft);
    setValidationError('');
    setSaved(true);
    window.setTimeout(() => setSaved(false), 1800);
  };

  const reset = () => {
    resetConfig();
    setValidationError('');
    setSaved(false);
  };

  return (
    <section className="settings-page" aria-label="调试配置">
      <div className="settings-heading">
        <div>
          <span className="section-kicker">CONFIGURATION</span>
          <h1>连接与请求配置</h1>
          <p>所有工作区共用以下 Agent、微服务地址和固定请求参数。配置保存在当前浏览器。</p>
        </div>
        <div className="settings-actions">
          <button type="button" className="settings-reset" onClick={reset}>恢复默认</button>
          <button type="button" className="settings-save" onClick={save}>保存配置</button>
          {saved && <span className="settings-saved" role="status">已保存</span>}
          {validationError && <span className="settings-validation-error" role="alert">{validationError}</span>}
        </div>
      </div>

      <div className="settings-scroll">
        <section className="settings-card">
          <div className="settings-card-heading"><h2>服务地址</h2><span>可填相对路径或完整 URL</span></div>
          <div className="settings-grid">
            <TextField label="Main Agent WebSocket" value={draft.agentWsUrl} onChange={(value) => setValue('agentWsUrl', value)} help="例如 /debug/e2e/ws 或 wss://agent.example/ws" />
            <TextField label="微服务 WebSocket Base" value={draft.toolWsBaseUrl} onChange={(value) => setValue('toolWsBaseUrl', value)} help="调用方会在末尾追加三个 operation" />
          </div>
        </section>

        <section className="settings-card">
          <div className="settings-card-heading"><h2>公共请求参数</h2><span>三接口和 Agent 共用</span></div>
          <div className="settings-grid">
            <TextField label="微服务协议版本" value={draft.protocolVersion} onChange={(value) => setValue('protocolVersion', value)} help="仅用于微服务请求包络；Agent 桥接协议固定为 1.0" />
            <TextField label="Bundle Name" value={draft.bundleName} onChange={(value) => setValue('bundleName', value)} />
            <TextField label="User ID" value={draft.userId} onChange={(value) => setValue('userId', value)} />
            <TextField label="Device ID" value={draft.deviceId} onChange={(value) => setValue('deviceId', value)} />
            <TextField label="Phone Type" value={draft.phoneType} onChange={(value) => setValue('phoneType', value)} />
            <TextField label="App Version" value={draft.appVersion} onChange={(value) => setValue('appVersion', value)} />
            <TextField label="ROM Version" value={draft.romVersion} onChange={(value) => setValue('romVersion', value)} />
            <TextField label="Locale" value={draft.locale} onChange={(value) => setValue('locale', value)} />
            <TextField label="Country Code" value={draft.countryCode} onChange={(value) => setValue('countryCode', value)} />
            <TextField label="Device Formation" value={draft.deviceFormation} onChange={(value) => setValue('deviceFormation', value)} />
            <TextField label="Device Type" type="number" value={draft.deviceType} onChange={(value) => setValue('deviceType', Number(value) || 0)} />
            <TextField label="System Version" value={draft.sysVer} onChange={(value) => setValue('sysVer', value)} />
          </div>
        </section>

        <section className="settings-card">
          <div className="settings-card-heading"><h2>分页参数</h2><span>只影响微服务请求</span></div>
          <div className="settings-grid settings-grid-small">
            <TextField label="最大记录数" type="number" value={draft.paginationLimit} onChange={(value) => setValue('paginationLimit', Math.max(1, Number(value) || 1))} />
            <TextField label="起始游标" value={draft.paginationStart} onChange={(value) => setValue('paginationStart', value)} help="预留分页游标，可保持为空" />
          </div>
        </section>
      </div>
    </section>
  );
}

export default SettingsPanel;
