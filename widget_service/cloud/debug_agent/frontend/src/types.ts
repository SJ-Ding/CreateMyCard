export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

export interface DebugEvent {
  type: string;
  sequence?: number;
  sessionId?: string;
  runId?: string;
  timestamp?: string;
  data?: Record<string, unknown>;
}

export interface ContextValues {
  uid: string;
  odid: string;
  deviceId: string;
  phoneType: string;
  appVersion: string;
  romVersion: string;
  locale: string;
  countryCode: string;
}

export interface SkillProfile {
  id: string;
  name?: string;
  displayName?: string;
  description?: string;
}

export interface QuickPrompt {
  label: string;
  prompt: string;
}

export interface ArtifactRecord {
  runId: string;
  timestamp?: string;
  [key: string]: unknown;
}

export type TimelineStatus = 'running' | 'success' | 'error' | 'result-error';

export interface TimelineMeta {
  callId?: string;
  toolName?: string;
  resourceId?: string;
  functionName?: string;
  step?: string;
  statusLabel?: string;
  flow?: 'request' | 'result' | 'execution-error' | 'business-error' | 'user' | 'assistant';
}

export interface TimelineEntry {
  id: string;
  type: string;
  title: string;
  detail?: string;
  timestamp?: string;
  tone: 'neutral' | 'success' | 'warning' | 'error' | 'info';
  status?: TimelineStatus;
  meta?: TimelineMeta;
}
