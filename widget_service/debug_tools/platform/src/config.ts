export type DebugEndpointConfig = {
  e2eSocketPath: string;
  toolsSocketPath: string;
  healthPath: string;
  skillsPath: string;
  assetBaseUrl: string;
};

export const endpointConfig: DebugEndpointConfig = {
  e2eSocketPath: '/debug/e2e/ws',
  toolsSocketPath: '/debug/tools',
  healthPath: '/debug/health',
  skillsPath: '/debug/skills',
  assetBaseUrl: '/resources/',
};

export function websocketUrl(path: string): string {
  if (/^wss?:\/\//.test(path)) {
    return path;
  }
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}${path}`;
}
