import { describe, expect, it } from 'vitest';
import { buildToolSocketUrl } from '../src/transport';

describe('buildToolSocketUrl', () => {
  it('appends an operation to a BFF path', () => {
    expect(buildToolSocketUrl('/debug/tools', 'getDataCapabilitySchemas'))
      .toBe('ws://127.0.0.1:8888/debug/tools/getDataCapabilitySchemas');
  });

  it('supports a full ws URL and an operation template', () => {
    expect(buildToolSocketUrl('wss://localhost:8888/debug/tools/{operation}', 'generateWidgetCardCompactDsl'))
      .toBe('wss://localhost:8888/debug/tools/generateWidgetCardCompactDsl');
  });

  it('does not append an operation twice', () => {
    expect(buildToolSocketUrl('ws://localhost:8855/api/v1/ws/tools/getWidgetCapabilityOverview', 'getWidgetCapabilityOverview'))
      .toBe('ws://localhost:8855/api/v1/ws/tools/getWidgetCapabilityOverview');
  });
});
