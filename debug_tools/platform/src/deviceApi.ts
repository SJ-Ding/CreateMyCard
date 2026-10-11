export type Target = { id: string; serial: string; name: string; kind: 'physical' | 'emulator' | 'unknown' };
export type Emulator = { id: string; name: string; path: string; ready: boolean; serial?: string | null };
export type Check = { name: string; ok: boolean; detail: string };
export type EnvironmentStatus = { id: string; label: string; ok: boolean; detail: string };
export type DeviceEnvironment = { checks: Check[]; status: EnvironmentStatus[] };
export type Crop = { x: number; y: number; width: number; height: number };
export type DeviceProfile = {
  id: string; name: string; serial: string; deviceName: string;
  kind: 'physical' | 'emulator' | 'unknown'; emulatorId?: string | null;
  resolution: { width: number; height: number };
  crops: Partial<Record<'2x2' | '2x4', Crop>>;
  renderWaitSeconds: number; version: number; updatedAt: string;
};
export type Operation = { id: string; status: string; message: string };
export type DeviceRun = {
  id: string; targetId: string; device: string; serial: string;
  status: string; stage: string; error: string | null; installed: boolean;
};
export const terminal = new Set(['success', 'failed', 'cancelled', 'interrupted']);

export class DeviceApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

export async function deviceRequest<T>(
  path: string,
  body?: unknown,
  options: { method?: 'POST' | 'PUT'; signal?: AbortSignal } = {},
): Promise<T> {
  const response = await fetch(`/debug/device${path}`, body === undefined
    ? { signal: options.signal }
    : {
      method: options.method ?? 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: options.signal,
    });
  const value = await response.json();
  if (!response.ok) {
    const detail = value.detail;
    throw new DeviceApiError(
      typeof detail === 'string' ? detail : JSON.stringify(detail ?? value), response.status,
    );
  }
  return value as T;
}

export async function listDeviceProfiles(): Promise<DeviceProfile[]> {
  const result = await deviceRequest<{ profiles: DeviceProfile[] }>('/profiles');
  return result.profiles;
}

export async function getDeviceEnvironment(signal?: AbortSignal): Promise<DeviceEnvironment> {
  return deviceRequest<DeviceEnvironment>('/environment', undefined, { signal });
}
