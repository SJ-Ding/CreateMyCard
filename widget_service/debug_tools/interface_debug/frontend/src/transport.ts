import type {
  InterfaceEvent,
  ToolFrame,
  ToolOperation,
  ToolStatus,
} from './types';

export interface ToolSocket {
  send(payload: unknown): void;
  close(): void;
}

export interface ToolSocketHandlers {
  onFrame: (frame: ToolFrame) => void;
  onStatus: (status: ToolStatus) => void;
  onEvent?: (event: InterfaceEvent) => void;
}

function websocketScheme(): string {
  if (typeof window !== 'undefined' && window.location.protocol === 'https:') {
    return 'wss:';
  }
  return 'ws:';
}

/** 将配置中的相对路径、http(s) URL 或 ws(s) URL 统一成 WebSocket 地址。 */
export function buildToolSocketUrl(base: string | undefined, operation: ToolOperation): string {
  const configured = (base || '/debug/tools').trim();
  if (/^https?:\/\//i.test(configured)) {
    const parsed = new URL(configured);
    parsed.protocol = parsed.protocol === 'https:' ? 'wss:' : 'ws:';
    return appendOperation(parsed.toString(), operation);
  }
  if (/^wss?:\/\//i.test(configured)) {
    return appendOperation(configured, operation);
  }
  const origin = typeof window === 'undefined'
    ? 'ws://127.0.0.1:8888'
    : `${websocketScheme()}//${window.location.host}`;
  return appendOperation(`${origin}${configured.startsWith('/') ? configured : `/${configured}`}`, operation);
}

function appendOperation(base: string, operation: ToolOperation): string {
  const normalized = base.replace(/\/+$/, '');
  const encodedOperation = encodeURIComponent(operation);
  if (normalized.endsWith(`/${encodedOperation}`) || normalized.endsWith(`/${operation}`)) {
    return normalized;
  }
  // Allow a BFF template path (`.../{operation}`) without double-appending.
  if (normalized.endsWith('/{operation}')) {
    return `${normalized.slice(0, -('/{operation}'.length))}/${encodedOperation}`;
  }
  return `${normalized}/${encodedOperation}`;
}

function frameType(frame: ToolFrame): string {
  const info = frame.reply?.streamInfo;
  return typeof info?.streamType === 'string' ? info.streamType : 'unknown';
}

function createEvent(
  direction: InterfaceEvent['direction'],
  kind: string,
  operation: ToolOperation,
  payload: unknown,
): InterfaceEvent {
  return { direction, kind, operation, payload, timestamp: new Date().toISOString() };
}

/** 一次工具调用一个连接，保持服务端原始帧顺序和内容。 */
export function connectToolSocket(
  base: string | undefined,
  operation: ToolOperation,
  payload: unknown,
  handlers: ToolSocketHandlers,
): ToolSocket {
  const url = buildToolSocketUrl(base, operation);
  let socket: WebSocket | null = null;
  let closedByCaller = false;
  const emitStatus = (state: ToolStatus['state'], text: string) => {
    handlers.onStatus({ state, text });
    handlers.onEvent?.(createEvent('local', `status:${state}`, operation, text));
  };
  const emit = (direction: InterfaceEvent['direction'], kind: string, value: unknown) => {
    handlers.onEvent?.(createEvent(direction, kind, operation, value));
  };

  emitStatus('connecting', '正在连接…');
  try {
    socket = new WebSocket(url);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    emitStatus('error', `连接失败：${message}`);
    return { send: () => undefined, close: () => undefined };
  }

  socket.onopen = () => {
    emitStatus('connected', '已连接');
    try {
      const serialized = JSON.stringify(payload);
      socket?.send(serialized);
      emit('send', 'request', payload);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      emitStatus('error', `请求序列化失败：${message}`);
    }
  };
  socket.onmessage = (message) => {
    try {
      const value: unknown = JSON.parse(String(message.data));
      if (!value || typeof value !== 'object' || Array.isArray(value)) {
        throw new Error('响应不是 JSON 对象');
      }
      const frame = value as ToolFrame;
      handlers.onFrame(frame);
      emit('receive', frameType(frame), frame);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      emitStatus('error', `响应解析失败：${detail}`);
    }
  };
  socket.onerror = () => {
    if (!closedByCaller) emitStatus('error', 'WebSocket 连接错误');
  };
  socket.onclose = (event) => {
    if (!closedByCaller || event.code !== 1000) {
      emitStatus('closed', `连接已关闭（${event.code}）`);
    }
  };

  return {
    send(nextPayload: unknown) {
      if (!socket || socket.readyState !== WebSocket.OPEN) return;
      socket.send(JSON.stringify(nextPayload));
      emit('send', 'request', nextPayload);
    },
    close() {
      closedByCaller = true;
      if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, 'client complete');
      socket = null;
    },
  };
}
