import { useWorkbench } from '../context';
import { endpointConfig } from '../config';
import { EndToEndDebug } from '@widget-debug/end-to-end';

export function EndToEndRoute() {
  const { pushEvent, setArtifact } = useWorkbench();
  return (
    <EndToEndDebug
      socketPath={endpointConfig.e2eSocketPath}
      healthPath={endpointConfig.healthPath}
      onEvent={(event) => pushEvent({ channel: 'e2e', direction: event.direction, kind: event.kind, payload: event.payload })}
      onArtifact={(artifact) => setArtifact({ ...artifact, source: 'e2e' })}
    />
  );
}
