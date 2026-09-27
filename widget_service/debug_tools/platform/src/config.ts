import type { DebugConfig } from './types';

export const DEBUG_CONFIG_VERSION = 1;
export const DEBUG_CONFIG_STORAGE_KEY = `ai-widget-debug-config:v${DEBUG_CONFIG_VERSION}`;
const LEGACY_DEBUG_CONFIG_STORAGE_KEY = 'ai-widget-debug-config';
export const CONFIG_STORAGE_KEY = DEBUG_CONFIG_STORAGE_KEY;
export const DEFAULT_TOOL_WS_BASE_URL = 'ws://127.0.0.1:8855/api/v1/ws/tools';
export const DEFAULT_AGENT_WS_URL = '/debug/e2e/ws';
export const DEFAULT_ASSET_BASE_URL = '/resources/';

type EndpointConfig = {
  agentWsUrl: string;
  toolWsBaseUrl: string;
};

/** 供仍未迁移到 Context 的独立模块读取的只读兼容快照。 */
export const endpointConfig: EndpointConfig = {
  agentWsUrl: DEFAULT_AGENT_WS_URL,
  toolWsBaseUrl: DEFAULT_TOOL_WS_BASE_URL,
};

export const defaultDebugConfig: DebugConfig = {
  agentWsUrl: DEFAULT_AGENT_WS_URL,
  toolWsBaseUrl: DEFAULT_TOOL_WS_BASE_URL,
  bundleName: 'com.omega_w_0823.hmservice',
  protocolVersion: '1.0',
  userId: 'debug-user',
  deviceId: 'debug-device',
  phoneType: 'ALN-AL00',
  appVersion: '11.7.7.332',
  romVersion: 'ALN-AL00 7.0.0.100',
  locale: 'zh-CN',
  countryCode: 'CN',
  deviceFormation: 'phone',
  deviceType: 0,
  sysVer: 'HarmonyOS',
  paginationLimit: 5,
  paginationStart: '',
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

/**
 * 将旧版 requestDefaults/endpoints 结构迁移成当前规范字段。
 * 动态 sessionId、interactionId 和 time 即使存在也会被忽略，不进入配置。
 */
export function normalizeDebugConfig(value: unknown): DebugConfig {
  const stored = isRecord(value) ? value : {};
  const source = isRecord(stored.config) ? stored.config : stored;
  const legacy = isRecord(source.requestDefaults) ? source.requestDefaults : {};
  const legacyEndpoints = isRecord(source.endpoints) ? source.endpoints : {};
  const pickString = (key: keyof DebugConfig, legacyKey?: string): string => (
    stringValue(source[key], stringValue(legacy[legacyKey ?? key], String(defaultDebugConfig[key] ?? '')))
  );
  const pickEndpoint = (key: 'agentWsUrl' | 'toolWsBaseUrl', ...legacyKeys: string[]): string => {
    if (typeof source[key] === 'string' && source[key].trim()) return source[key].trim();
    for (const legacyKey of legacyKeys) {
      const valueFromEndpoints = legacyEndpoints[legacyKey];
      if (typeof valueFromEndpoints === 'string' && valueFromEndpoints.trim()) {
        return valueFromEndpoints.trim();
      }
      const valueFromRoot = source[legacyKey];
      if (typeof valueFromRoot === 'string' && valueFromRoot.trim()) return valueFromRoot.trim();
    }
    return defaultDebugConfig[key];
  };
  // Some early builds nested the canonical names under `endpoints`, while
  // others used the older `toolsBaseUrl`/`e2eSocketPath` aliases.  Read both
  // shapes so a refresh never silently switches a user's custom endpoint
  // back to localhost.
  const toolWsBaseUrl = pickEndpoint(
    'toolWsBaseUrl',
    'toolWsBaseUrl',
    'toolsBaseUrl',
    'toolsSocketPath',
  );
  const agentWsUrl = pickEndpoint('agentWsUrl', 'agentWsUrl', 'e2eSocketPath');
  const deviceType = Math.max(0, Math.floor(numberValue(source.deviceType, numberValue(legacy.deviceType, defaultDebugConfig.deviceType))));
  const paginationLimit = Math.max(1, Math.floor(numberValue(source.paginationLimit, defaultDebugConfig.paginationLimit)));
  return {
    agentWsUrl,
    toolWsBaseUrl,
    bundleName: pickString('bundleName'),
    protocolVersion: stringValue(
      source.protocolVersion,
      stringValue(
        typeof source.version === 'string' ? source.version : undefined,
        pickString('protocolVersion', 'version'),
      ),
    ),
    userId: pickString('userId'),
    deviceId: pickString('deviceId'),
    phoneType: pickString('phoneType'),
    appVersion: pickString('appVersion', 'prdVer'),
    romVersion: pickString('romVersion'),
    locale: pickString('locale'),
    countryCode: pickString('countryCode'),
    deviceFormation: pickString('deviceFormation'),
    deviceType,
    sysVer: pickString('sysVer'),
    paginationLimit,
    paginationStart: pickString('paginationStart'),
  };
}

export function cloneDebugConfig(config: DebugConfig): DebugConfig {
  return { ...config };
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
    for (const key of parsed.searchParams.keys()) {
      if (/(?:^|_|-)(?:token|api[_-]?key|secret|password|authorization|auth)(?:$|_|-)/i.test(key)) {
        return true;
      }
    }
    return false;
  } catch {
    return false;
  }
}

function persistedConfig(config: DebugConfig): DebugConfig {
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
    const currentRaw = localStorage.getItem(DEBUG_CONFIG_STORAGE_KEY);
    const legacyRaw = currentRaw ? null : localStorage.getItem(LEGACY_DEBUG_CONFIG_STORAGE_KEY);
    const raw = currentRaw ?? legacyRaw;
    if (!raw) return cloneDebugConfig(defaultDebugConfig);
    const config = persistedConfig(normalizeDebugConfig(JSON.parse(raw)));
    if (!currentRaw && legacyRaw) {
      localStorage.setItem(DEBUG_CONFIG_STORAGE_KEY, JSON.stringify({
        version: DEBUG_CONFIG_VERSION,
        config,
      }));
      localStorage.removeItem(LEGACY_DEBUG_CONFIG_STORAGE_KEY);
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
    localStorage.removeItem(LEGACY_DEBUG_CONFIG_STORAGE_KEY);
  } catch { /* ignore storage errors */ }
}

export function applyDebugConfig(config: DebugConfig): void {
  endpointConfig.agentWsUrl = config.agentWsUrl;
  endpointConfig.toolWsBaseUrl = config.toolWsBaseUrl;
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
    url = new URL(`${scheme}//${window.location.host}/${normalized.replace(/^\/+/, '')}`);
  }
  if (url.username || url.password) throw new Error('地址不能包含认证信息');
  return url;
}

function rejectMixedContent(url: URL): void {
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
  if (typeof window !== 'undefined' && window.location.protocol === 'https:' && url.protocol === 'http:') {
    throw new Error('HTTPS 页面不能连接不安全的 http 地址');
  }
  return url.toString();
}
