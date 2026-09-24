export type DebugModule = 'end-to-end' | 'interface' | 'renderer';

export type DebugEvent = {
  id: string;
  channel: 'e2e' | 'tools' | 'renderer' | 'system';
  direction: 'send' | 'receive' | 'local';
  kind: string;
  timestamp: string;
  durationMs?: number;
  payload?: unknown;
};

export type CardArtifact = {
  source: 'e2e' | 'interface' | 'import';
  runId?: string;
  artifactUrl?: string;
  artifactDigest?: string;
  genui?: string;
  cardSpec?: unknown;
  raw?: unknown;
};

export type WorkbenchContextValue = {
  events: DebugEvent[];
  pushEvent: (event: Omit<DebugEvent, 'id' | 'timestamp'> & { timestamp?: string }) => void;
  artifact: CardArtifact | null;
  setArtifact: (artifact: CardArtifact | null) => void;
};
