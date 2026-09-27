import { useWorkbench } from '../context';
import { endpointConfig } from '../config';
import { InterfaceDebugger } from '@widget-debug/interface';

export function InterfaceRoute() {
  const { config, calls, selectedCallId, recordCall, updateCall, pushEvent, setArtifact } = useWorkbench();
  const selectedCall = calls.find((item) => item.id === selectedCallId) ?? null;
  return (
    <InterfaceDebugger
      transportBase={config.toolWsBaseUrl || endpointConfig.toolWsBaseUrl}
      config={config}
      selectedCall={selectedCall}
      onCallStart={({ operation, request }) => recordCall({
        operation,
        source: 'interface',
        status: 'pending',
        startedAt: new Date().toISOString(),
        request,
      })}
      onCallFinish={(id, result) => {
        if (!id) return;
        const error = result.error === undefined
          ? undefined
          : typeof result.error === 'string'
            ? result.error
            : JSON.stringify(result.error);
        updateCall(id, {
          status: result.status === 'error' || result.status === 'failed' ? 'error' : 'success',
          finishedAt: new Date().toISOString(),
          finalFrame: result.finalFrame,
          finalStreamContent: result.finalStreamContent,
          response: result,
          error,
        });
      }}
      onCallFail={(id, error) => {
        if (id) updateCall(id, { status: 'error', finishedAt: new Date().toISOString(), error });
      }}
      onEvent={(event) => pushEvent({ channel: 'tools', direction: event.direction, kind: event.kind, operation: event.operation, payload: event.payload })}
      onArtifact={(artifact) => setArtifact({ ...artifact, source: 'interface' })}
    />
  );
}
