import type { DebugConfig } from './types';

export const DEBUG_CONFIG_VERSION = 1;
export const DEBUG_CONFIG_STORAGE_KEY = `ai-widget-debug-config:v${DEBUG_CONFIG_VERSION}`;
export const DEFAULT_TOOL_WS_BASE_URL = 'ws://127.0.0.1:8855/api/v1/ws/tools';
export const DEFAULT_AGENT_WS_URL = '/debug/e2e/ws';
export const DEFAULT_ASSET_BASE_URL = '/resources/';
export const DEFAULT_MANAGED_SERVICE_CONFIG: Record<string, string | number | boolean> = {};

export const defaultDebugConfig: DebugConfig = {
  agentWsUrl: DEFAULT_AGENT_WS_URL,
  toolWsBaseUrl: DEFAULT_TOOL_WS_BASE_URL,
  microserviceMode: 'deployed',
  managedServiceWsUrl: '',
  bundleName: 'com.omega_w_0823.hmservice',
  protocolVersion: '1.0',
  userId: 'debug-user',
  deviceId: 'debug-device',
  phoneType: 'ALN-AL00',
  appVersion: '11.9.9.342',
  romVersion: 'ALN-AL00 7.0.0.100',
  locale: 'zh-CN',
  countryCode: 'CN',
  deviceFormation: 'phone',
  deviceType: 0,
  sysVer: 'HarmonyOS',
  paginationLimit: 5,
  paginationStart: '',
  managedServiceConfig: { ...DEFAULT_MANAGED_SERVICE_CONFIG },
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function stringValue(value: unknown, fallback: string): string {
  return typeof value === 'string' ? value.trim() : fallback;
}

function numberValue(value: unknown, fallback: number): number {
  if (typeof value !== 'number' && typeof value !== 'string') return fallback;
  if (typeof value === 'string' && !value.trim()) return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

/** 仅归一化当前配置字段；动态会话信息不进入固定配置。 */
export function normalizeDebugConfig(value: unknown): DebugConfig {
  const source = isRecord(value) ? value : {};
  const pickString = (key: keyof DebugConfig): string => (
    stringValue(source[key], String(defaultDebugConfig[key] ?? ''))
  );
  const deviceType = Math.max(0, Math.floor(numberValue(source.deviceType, defaultDebugConfig.deviceType)));
  const paginationLimit = Math.max(1, Math.floor(numberValue(source.paginationLimit, defaultDebugConfig.paginationLimit)));
  const managedServiceConfig = {};
  return {
    agentWsUrl: pickString('agentWsUrl'),
    toolWsBaseUrl: pickString('toolWsBaseUrl'),
    microserviceMode: source.microserviceMode === 'managed' ? 'managed' : 'deployed',
    managedServiceWsUrl: pickString('managedServiceWsUrl'),
    bundleName: pickString('bundleName'),
    protocolVersion: pickString('protocolVersion'),
    userId: pickString('userId'),
    deviceId: pickString('deviceId'),
    phoneType: pickString('phoneType'),
    appVersion: pickString('appVersion'),
    romVersion: pickString('romVersion'),
    locale: pickString('locale'),
    countryCode: pickString('countryCode'),
    deviceFormation: pickString('deviceFormation'),
    deviceType,
    sysVer: pickString('sysVer'),
    paginationLimit,
    paginationStart: pickString('paginationStart'),
    managedServiceConfig,
  };
}

export function cloneDebugConfig(config: DebugConfig): DebugConfig {
  return { ...config, managedServiceConfig: { ...config.managedServiceConfig } };
}

/** URL 中的 userinfo 或常见 query 凭据永远不写入本地持久化配置。 */
export function endpointHasCredentials(value: string): boolean {
  const normalized = value.trim();
  if (!normalized) return false;
  try {
    const parsed = /^(?:https?|wss?):\/\//i.test(normalized)
      ? new URL(normalized)
      : new URL(normalized, 'http://127.0.0.1');
    if (parsed.username || parsed.password) return true;
    // WebSocket/HTTP fragments are never sent to the server and are often
    // used accidentally for bearer material; reject them before persistence.
    if (parsed.hash) return true;
    for (const key of parsed.searchParams.keys()) {
      const normalizedKey = key.toLowerCase().replace(/[^a-z0-9]/g, '');
      if (/(?:token|apikey|secret|password|authorization|auth)/.test(normalizedKey)) {
        return true;
      }
    }
    return false;
  } catch {
    return false;
  }
}

function persistedConfig(config: unknown): DebugConfig {
  const normalized = normalizeDebugConfig(config);
  return {
    ...normalized,
    // Keep the current in-memory value available for an immediate validation
    // message, while ensuring a reload can never recover URL credentials.
    agentWsUrl: endpointHasCredentials(normalized.agentWsUrl)
      ? defaultDebugConfig.agentWsUrl
      : normalized.agentWsUrl,
    toolWsBaseUrl: endpointHasCredentials(normalized.toolWsBaseUrl)
      ? defaultDebugConfig.toolWsBaseUrl
      : normalized.toolWsBaseUrl,
  };
}

export function loadDebugConfig(): DebugConfig {
  if (typeof localStorage === 'undefined') return cloneDebugConfig(defaultDebugConfig);
  try {
    const raw = localStorage.getItem(DEBUG_CONFIG_STORAGE_KEY);
    if (!raw) return cloneDebugConfig(defaultDebugConfig);
    const stored: unknown = JSON.parse(raw);
    if (!isRecord(stored) || stored.version !== DEBUG_CONFIG_VERSION || !isRecord(stored.config)) {
      return cloneDebugConfig(defaultDebugConfig);
    }
    const config = persistedConfig(stored.config);
    // 清理持久化地址中的凭据；写入失败仍保留本次有效配置。
    const serialized = JSON.stringify({ version: DEBUG_CONFIG_VERSION, config });
    if (raw !== serialized) {
      try {
        localStorage.setItem(DEBUG_CONFIG_STORAGE_KEY, serialized);
      } catch { /* 本地存储不可写时继续使用内存配置。 */ }
    }
    return config;
  } catch {
    return cloneDebugConfig(defaultDebugConfig);
  }
}

export function saveDebugConfig(config: DebugConfig): void {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(DEBUG_CONFIG_STORAGE_KEY, JSON.stringify({
      version: DEBUG_CONFIG_VERSION,
      config: persistedConfig(config),
    }));
  } catch {
    // 隐私模式或配额不足时仍保留当前 React 会话中的配置。
  }
}

export function clearSavedDebugConfig(): void {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.removeItem(DEBUG_CONFIG_STORAGE_KEY);
  } catch { /* ignore storage errors */ }
}

export function selectedToolWsBaseUrl(config: DebugConfig): string {
  return config.microserviceMode === 'managed'
    ? config.managedServiceWsUrl
    : config.toolWsBaseUrl;
}

function ensureScheme(value: string, kind: 'http' | 'ws'): URL {
  const normalized = value.trim();
  if (!normalized) throw new Error('地址不能为空');
  if (normalized.startsWith('//')) throw new Error('地址协议无效');
  if (/^[A-Za-z][A-Za-z0-9+.-]*:/.test(normalized)
    && !/^(?:https?|wss?):\/\//i.test(normalized)) {
    throw new Error('地址协议无效');
  }
  const isHttp = /^https?:\/\//i.test(normalized);
  const isWebSocket = /^wss?:\/\//i.test(normalized);
  let url: URL;
  if (kind === 'ws' && isHttp) {
    url = new URL(normalized.replace(/^http:/i, 'ws:').replace(/^https:/i, 'wss:'));
  } else if (kind === 'http' && isWebSocket) {
    url = new URL(normalized.replace(/^ws:/i, 'http:').replace(/^wss:/i, 'https:'));
  } else if (isHttp || isWebSocket) {
    url = new URL(normalized);
  } else if (typeof window === 'undefined') {
    url = new URL(`${kind === 'ws' ? 'ws' : 'http'}://127.0.0.1:8888/${normalized.replace(/^\/+/, '')}`);
  } else {
    const scheme = kind === 'ws'
      ? window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      : window.location.protocol === 'https:' ? 'https:' : 'http:';
    const host = window.location.host || '127.0.0.1:8888';
    url = new URL(`${scheme}//${host}/${normalized.replace(/^\/+/, '')}`);
  }
  if (url.username || url.password) throw new Error('地址不能包含认证信息');
  return url;
}

function rejectMixedContent(url: URL): void {
  if (url.hash) throw new Error('地址不能包含 hash 片段');
  if (typeof window !== 'undefined' && window.location.protocol === 'https:' && url.protocol === 'ws:') {
    throw new Error('HTTPS 页面不能连接不安全的 ws 地址');
  }
}

export function websocketUrl(path: string): string {
  const url = ensureScheme(path, 'ws');
  if (!['ws:', 'wss:'].includes(url.protocol) || !url.hostname) throw new Error('地址必须使用 ws/wss 协议');
  rejectMixedContent(url);
  return url.toString();
}

export function httpUrl(path: string): string {
  const url = ensureScheme(path, 'http');
  if (!['http:', 'https:'].includes(url.protocol) || !url.hostname) throw new Error('地址必须使用 http/https 协议');
  if (url.hash) throw new Error('地址不能包含 hash 片段');
  if (typeof window !== 'undefined' && window.location.protocol === 'https:' && url.protocol === 'http:') {
    throw new Error('HTTPS 页面不能连接不安全的 http 地址');
  }
  return url.toString();
}
