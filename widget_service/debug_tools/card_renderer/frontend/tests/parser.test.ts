import { describe, expect, it } from 'vitest';
import { SAMPLE_A2UI, SAMPLE_COMPACT, SAMPLE_DESIGN } from '../src/fixtures';
import { parseInput, resolveTemplate, sizeForCard } from '../src/parser';

describe('card renderer parser', () => {
  it.each([
    ['A2UI', SAMPLE_A2UI],
    ['Compact DSL', SAMPLE_COMPACT],
    ['Design Compact DSL', SAMPLE_DESIGN],
  ])('recognizes %s fixtures', (expectedMode, source) => {
    const document = parseInput(source);
    expect(document.mode).toBe(expectedMode);
    expect(document.components.size).toBeGreaterThan(0);
    expect(document.surface.width).toBeGreaterThan(0);
    expect(document.surface.height).toBeGreaterThan(0);
  });

  it('resolves data model template expressions', () => {
    expect(resolveTemplate('天气：{{weather.city}}', { weather: { city: '上海' } })).toBe('天气：上海');
  });

  it('infers stable 2x2 and 2x4 surfaces', () => {
    expect(sizeForCard('2x2')).toEqual({ width: 160, height: 160 });
    expect(sizeForCard('2x4')).toEqual({ width: 320, height: 160 });
  });
});
