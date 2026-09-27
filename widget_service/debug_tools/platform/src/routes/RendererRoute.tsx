import { useWorkbench } from '../context';
import { DEFAULT_ASSET_BASE_URL } from '../config';
import { CardRenderer } from '@widget-debug/card-renderer';
import { parsePythonRepr } from '@widget-debug/interface';

function renderableValue(value: unknown): string | undefined {
  if (typeof value === 'string') return value;
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const record = value as Record<string, unknown>;
  const nestedResponse = record.response && typeof record.response === 'object' && !Array.isArray(record.response)
    ? record.response as Record<string, unknown>
    : {};
  const nested = record.data && typeof record.data === 'object' && !Array.isArray(record.data)
    ? record.data as Record<string, unknown>
    : {};
  const responseData = nestedResponse.data && typeof nestedResponse.data === 'object' && !Array.isArray(nestedResponse.data)
    ? nestedResponse.data as Record<string, unknown>
    : {};
  const payload = { ...record, ...nestedResponse, ...nested, ...responseData };
  if (typeof payload.genui === 'string') return payload.genui;
  if (typeof payload.dsl === 'string') return payload.dsl;
  if (typeof payload.compactDsl === 'string') return payload.compactDsl;
  if (payload.cardSpec !== undefined) return JSON.stringify(payload.cardSpec, null, 2);
  return undefined;
}

function finalFrameSource(frame: Record<string, unknown> | undefined): string | undefined {
  if (!frame) return undefined;
  const reply = frame.reply;
  const streamInfo = reply && typeof reply === 'object' && !Array.isArray(reply)
    ? (reply as Record<string, unknown>).streamInfo
    : undefined;
  const streamContent = streamInfo && typeof streamInfo === 'object' && !Array.isArray(streamInfo)
    ? (streamInfo as Record<string, unknown>).streamContent
    : frame.streamContent;
  if (typeof streamContent === 'string' && streamContent.trim()) {
    const parsed = parsePythonRepr(streamContent);
    return renderableValue(parsed) ?? streamContent;
  }
  return renderableValue(frame) ?? renderableValue(frame.data)
    ?? (frame.data == null ? undefined : JSON.stringify(frame.data, null, 2));
}

export function RendererRoute() {
  const { artifact, calls, selectedCallId, pushEvent } = useWorkbench();
  const selectedCall = calls.find((item) => item.id === selectedCallId);
  const responseData = selectedCall?.response?.data;
  const parsedStream = selectedCall?.finalStreamContent
    ? parsePythonRepr(selectedCall.finalStreamContent)
    : undefined;
  const frameSource = finalFrameSource(selectedCall?.finalFrame);
  const responseSource = renderableValue(responseData) ?? renderableValue(parsedStream) ?? frameSource;
  const selectedValue = responseSource
    ?? (responseData == null ? selectedCall?.finalStreamContent : JSON.stringify(responseData, null, 2));
  const artifactValue = artifact?.genui
    ?? (artifact?.cardSpec == null ? undefined : JSON.stringify(artifact.cardSpec, null, 2))
    ?? (artifact?.raw == null ? undefined : JSON.stringify(artifact.raw, null, 2));
  // Selecting a shared call is authoritative; otherwise a previously
  // generated artifact remains available as the renderer's source.
  const initialValue = selectedCall ? selectedValue : artifactValue;
  const artifactKey = selectedCall?.id ?? artifact?.runId ?? artifact?.artifactDigest ?? initialValue ?? 'empty';
  return (
    <CardRenderer
      key={artifactKey}
      initialValue={selectedCall ? (initialValue ?? '') : initialValue}
      assetBaseUrl={DEFAULT_ASSET_BASE_URL}
      onArtifact={(document) => {
        pushEvent({
          channel: 'renderer',
          direction: 'local',
          kind: 'artifact_rendered',
          payload: {
            format: document.mode,
            rows: document.rows,
            componentCount: document.components.size,
            dataPathCount: document.dataPathCount,
          },
        });
      }}
    />
  );
}
