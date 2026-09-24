import { useWorkbench } from '../context';
import { endpointConfig } from '../config';
import { CardRenderer } from '@widget-debug/card-renderer';

export function RendererRoute() {
  const { artifact, pushEvent } = useWorkbench();
  const initialValue = artifact?.genui
    ?? (artifact?.raw == null ? undefined : JSON.stringify(artifact.raw, null, 2));
  const artifactKey = artifact?.runId ?? artifact?.artifactDigest ?? initialValue ?? 'empty';
  return (
    <CardRenderer
      key={artifactKey}
      initialValue={initialValue}
      assetBaseUrl={endpointConfig.assetBaseUrl}
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
