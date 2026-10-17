import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  DEBUG_CONFIG_STORAGE_KEY, defaultDebugConfig, loadDebugConfig,
  normalizeDebugConfig, saveDebugConfig, selectedToolWsBaseUrl,
} from './config';

describe('microservice connection selection', () => {
  afterEach(() => { localStorage.clear(); vi.restoreAllMocks(); });

  it('ignores old keys, nested fields and aliases', () => {
    localStorage.setItem('ai-widget-debug-config', JSON.stringify({ toolWsBaseUrl: 'ws://old.test' }));
    expect(loadDebugConfig()).toEqual(defaultDebugConfig);
    expect(normalizeDebugConfig({ requestDefaults: { bundleName: 'old' },
      endpoints: { toolWsBaseUrl: 'ws://old.test' }, version: 'old', prdVer: 'old',
    })).toEqual(defaultDebugConfig);
    expect(normalizeDebugConfig({ toolWsBaseUrl: '/debug/tools' }).toolWsBaseUrl).toBe('/debug/tools');
  });

  it('restores the current version and rejects unsupported storage shapes', () => {
    saveDebugConfig({ ...defaultDebugConfig, agentWsUrl: 'wss://agent.test/ws', appVersion: '12.0' });
    expect(loadDebugConfig()).toMatchObject({ agentWsUrl: 'wss://agent.test/ws', appVersion: '12.0' });
    for (const value of [{ config: defaultDebugConfig }, { version: 0, config: defaultDebugConfig }, defaultDebugConfig]) {
      localStorage.setItem(DEBUG_CONFIG_STORAGE_KEY, JSON.stringify(value));
      expect(loadDebugConfig()).toEqual(defaultDebugConfig);
    }
  });

  it('cleans credentials and retains valid memory values when storage is read-only', () => {
    localStorage.setItem(DEBUG_CONFIG_STORAGE_KEY, JSON.stringify({ version: 1, config: {
      ...defaultDebugConfig, agentWsUrl: 'wss://user:secret@agent.test/ws', toolWsBaseUrl: 'wss://tool.test/ws?token=secret',
    } }));
    expect(loadDebugConfig()).toEqual(defaultDebugConfig);
    expect(localStorage.getItem(DEBUG_CONFIG_STORAGE_KEY)).not.toContain('secret');
    localStorage.setItem(DEBUG_CONFIG_STORAGE_KEY, JSON.stringify({ version: 1, config: {
      agentWsUrl: 'wss://custom.test/ws',
    } }));
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('readonly'); });
    expect(loadDebugConfig().agentWsUrl).toBe('wss://custom.test/ws');
  });
  it('defaults to the deployed service', () => {
    const config = normalizeDebugConfig({});
    expect(config.microserviceMode).toBe('deployed');
    expect(selectedToolWsBaseUrl(config)).toBe(defaultDebugConfig.toolWsBaseUrl);
  });

  it('uses the managed endpoint when managed mode is selected', () => {
    const config = normalizeDebugConfig({
      microserviceMode: 'managed',
      managedServiceWsUrl: 'ws://127.0.0.1:9000/api/v1/ws/tools',
    });
    expect(selectedToolWsBaseUrl(config)).toBe('ws://127.0.0.1:9000/api/v1/ws/tools');
  });
});
