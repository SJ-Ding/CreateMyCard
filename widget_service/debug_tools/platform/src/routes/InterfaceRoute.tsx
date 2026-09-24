import { useWorkbench } from '../context';
import { endpointConfig } from '../config';
import { InterfaceDebugger } from '@widget-debug/interface';

export function InterfaceRoute() {
  const { pushEvent, setArtifact } = useWorkbench();
  return (
    <InterfaceDebugger
      socketBasePath={endpointConfig.toolsSocketPath}
      onEvent={(event) => pushEvent({ channel: 'tools', direction: event.direction, kind: event.kind, payload: event.payload })}
      onArtifact={(artifact) => setArtifact({ ...artifact, source: 'interface' })}
    />
  );
}
