import type { JsonObject, Selection, ToolFrame } from './types';

const ARRAY_ITEM_COUNT_MIN = 1;
const ARRAY_ITEM_COUNT_MAX = 100;
const OUTPUT_FIELD_PATH_LIMIT = 5000;

export function streamType(frame: ToolFrame): string {
  const value = frame.reply?.streamInfo?.streamType;
  return typeof value === 'string' ? value : 'unknown';
}

export function streamContent(frame: ToolFrame): string {
  const value = frame.reply?.streamInfo?.streamContent;
  return typeof value === 'string' ? value : '';
}

/**
 * 解析服务端旧协议中可能出现的 JSON、Python repr 和 `data={...}` 包装。
 * 不执行 eval，所有输入都经过字符扫描后交给 JSON.parse。
 */
export function parsePythonRepr(value: unknown): unknown | null {
  if (typeof value !== 'string') return value ?? null;
  const source = value.trim();
  if (!source) return null;
  const direct = parseJson(source);
  if (direct !== null) return direct;
  const fenced = extractFencedJson(source);
  if (fenced) {
    const parsedFence = parseJson(fenced);
    if (parsedFence !== null) return parsedFence;
  }
  const jsonLines = source.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (jsonLines.length > 1) {
    const parsedLines = jsonLines.map(parseJson);
    if (parsedLines.every((line) => line !== null)) return parsedLines;
  }
  const dataObject = extractDataObject(source);
  if (dataObject) {
    const parsedData = convertPythonRepr(dataObject);
    if (parsedData !== null) return parsedData;
  }
  return convertPythonRepr(source);
}

function parseJson(source: string): unknown | null {
  try {
    return JSON.parse(source) as unknown;
  } catch {
    return null;
  }
}

function extractFencedJson(source: string): string | null {
  const match = source.match(/```(?:json|jsonl|javascript)?\s*([\s\S]*?)```/i);
  return match?.[1]?.trim() || null;
}

function extractDataObject(source: string): string | null {
  const marker = source.indexOf('data=');
  if (marker < 0) return null;
  const start = source.slice(marker + 'data='.length).trimStart();
  if (!start.startsWith('{') && !start.startsWith('[')) return null;
  const end = balancedEnd(start);
  return end < 0 ? null : start.slice(0, end + 1);
}

function balancedEnd(source: string): number {
  const stack: string[] = [];
  let quote = '';
  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];
    if (quote) {
      if (character === '\\') index += 1;
      else if (character === quote) quote = '';
      continue;
    }
    if (character === '"' || character === "'") {
      quote = character;
      continue;
    }
    if (character === '{' || character === '[') stack.push(character);
    if (character === '}' || character === ']') {
      const expected = character === '}' ? '{' : '[';
      if (stack.pop() !== expected) return -1;
      if (stack.length === 0) return index;
    }
  }
  return -1;
}

function convertPythonRepr(source: string): unknown | null {
  let output = '';
  let quote = '';
  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];
    if (!quote) {
      if (character === '"' || character === "'") {
        quote = character;
        output += '"';
      } else if (source.startsWith('True', index) && isTokenBoundary(source, index, 4)) {
        output += 'true';
        index += 3;
      } else if (source.startsWith('False', index) && isTokenBoundary(source, index, 5)) {
        output += 'false';
        index += 4;
      } else if (source.startsWith('None', index) && isTokenBoundary(source, index, 4)) {
        output += 'null';
        index += 3;
      } else {
        output += character;
      }
      continue;
    }
    if (character === '\\' && index + 1 < source.length) {
      const next = source[index + 1];
      const escaped = next === "'" ? "'" : next === '"' ? '\\"' : next;
      output += `\\${escaped}`;
      index += 1;
    } else if (character === quote) {
      quote = '';
      output += '"';
    } else if (character === '"') {
      output += '\\"';
    } else {
      output += character;
    }
  }
  return parseJson(output);
}

function isTokenBoundary(source: string, start: number, length: number): boolean {
  const before = source[start - 1];
  const after = source[start + length];
  return !before || !/[A-Za-z0-9_]/.test(before)
    ? !after || !/[A-Za-z0-9_]/.test(after)
    : false;
}

export function normalizeArrayItemCount(value: unknown): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return ARRAY_ITEM_COUNT_MIN;
  return Math.min(ARRAY_ITEM_COUNT_MAX, Math.max(ARRAY_ITEM_COUNT_MIN, Math.floor(numeric)));
}

export interface SchemaNode {
  type?: string;
  description?: string;
  sampleValue?: unknown;
  properties?: Record<string, SchemaNode>;
  items?: SchemaNode;
  required?: string[];
  [key: string]: unknown;
}

export function buildSchemaPlaceholder(schema: SchemaNode, includeOptional = false): unknown {
  if (schema.type === 'string') return '请输入';
  if (schema.type === 'integer' || schema.type === 'number') return 0;
  if (schema.type === 'boolean') return false;
  if (schema.type === 'array') return [];
  if (schema.type === 'object' || schema.properties) return buildDefaultArguments(schema, includeOptional);
  return null;
}

export function buildDefaultArguments(schema: SchemaNode | null | undefined, includeOptional = false): JsonObject {
  if (!schema?.properties) return {};
  const result: JsonObject = {};
  const required = new Set(schema.required ?? []);
  Object.entries(schema.properties).forEach(([key, child]) => {
    if (child.sampleValue !== undefined) result[key] = clone(child.sampleValue);
    else if (required.has(key) || includeOptional) result[key] = buildSchemaPlaceholder(child, includeOptional);
  });
  return result;
}

export function buildOutputFieldPaths(
  schema: SchemaNode | null | undefined,
  arrayItemCount: (schemaPath: string) => number = () => ARRAY_ITEM_COUNT_MIN,
): string[] {
  const output: string[] = [];
  collectOutputPaths(schema, '', '', output, arrayItemCount);
  return output;
}

function collectOutputPaths(
  schema: SchemaNode | null | undefined,
  outputPath: string,
  schemaPath: string,
  output: string[],
  arrayItemCount: (schemaPath: string) => number,
): void {
  if (!schema) return;
  if (schema.type === 'array' && schema.items) {
    const count = normalizeArrayItemCount(arrayItemCount(schemaPath));
    for (let index = 0; index < count; index += 1) {
      collectOutputPaths(
        schema.items,
        `${outputPath}/${index}`,
        joinSchemaPath(schemaPath, 'items'),
        output,
        arrayItemCount,
      );
    }
    return;
  }
  const properties = schema.properties;
  if (properties && Object.keys(properties).length > 0) {
    Object.entries(properties).forEach(([key, child]) => {
      collectOutputPaths(
        child,
        `${outputPath}/${escapeJsonPointerSegment(key)}`,
        joinSchemaPath(schemaPath, `properties.${key}`),
        output,
        arrayItemCount,
      );
    });
    return;
  }
  const composite = schema.type === 'object' || schema.type === 'array';
  if (outputPath && !composite) {
    if (output.length >= OUTPUT_FIELD_PATH_LIMIT) {
      throw new RangeError(`输出字段展开超过 ${OUTPUT_FIELD_PATH_LIMIT} 条，请减少数组项数量。`);
    }
    output.push(outputPath);
  }
}

function joinSchemaPath(path: string, segment: string): string {
  return path ? `${path}.${segment}` : segment;
}

function escapeJsonPointerSegment(value: string): string {
  return value.replace(/~/g, '~0').replace(/\//g, '~1');
}

export function getAtPath(source: unknown, path: string): unknown {
  if (!path) return source;
  return path.split('.').reduce<unknown>((value, segment) => {
    if (value && typeof value === 'object') {
      if (Array.isArray(value) && /^\d+$/.test(segment)) return value[Number(segment)];
      return (value as Record<string, unknown>)[segment];
    }
    return undefined;
  }, source);
}

export function buildSelectedSubset(source: unknown, selections: Selection[]): unknown {
  if (selections.length === 0) return {};
  const result: unknown = Array.isArray(source) ? [] : {};
  const uniquePaths = [...new Set(selections.map((item) => item.path))];
  uniquePaths.sort((left, right) => left.split('.').length - right.split('.').length);
  uniquePaths.forEach((path) => {
    const value = getAtPath(source, path);
    if (value === undefined) return;
    setPath(result, path, clone(value));
  });
  return result;
}

function setPath(target: unknown, path: string, value: unknown): void {
  const parts = path.split('.').filter(Boolean);
  if (parts.length === 0 || !target || typeof target !== 'object') return;
  let cursor = target as Record<string, unknown> | unknown[];
  parts.forEach((part, index) => {
    const finalPart = index === parts.length - 1;
    const nextIsArray = !finalPart && /^\d+$/.test(parts[index + 1]);
    if (Array.isArray(cursor)) {
      const position = Number(part);
      while (cursor.length <= position) cursor.push(undefined);
      if (finalPart) cursor[position] = value;
      else {
        const current = cursor[position];
        if (!current || typeof current !== 'object') cursor[position] = nextIsArray ? [] : {};
        cursor = cursor[position] as Record<string, unknown> | unknown[];
      }
    } else if (finalPart) {
      cursor[part] = value;
    } else {
      const current = cursor[part];
      if (!current || typeof current !== 'object') cursor[part] = nextIsArray ? [] : {};
      cursor = cursor[part] as Record<string, unknown> | unknown[];
    }
  });
}

export function clone<T>(value: T): T {
  if (value === undefined) return value;
  try {
    return JSON.parse(JSON.stringify(value)) as T;
  } catch {
    return value;
  }
}

export function findFinalFrame(frames: ToolFrame[]): ToolFrame | undefined {
  return [...frames].reverse().find((frame) => streamType(frame) === 'final');
}

export function extractArtifact(parsed: unknown, operation: Selection['operation'], runId: string) {
  const record = parsed && typeof parsed === 'object' && !Array.isArray(parsed)
    ? parsed as Record<string, unknown>
    : {};
  const nested = record.data && typeof record.data === 'object' && !Array.isArray(record.data)
    ? record.data as Record<string, unknown>
    : {};
  const payload = { ...record, ...nested };
  const genuiValue = payload.genui ?? payload.dsl ?? payload.source ?? payload.compactDsl;
  const artifactUrl = typeof payload.artifactUrl === 'string' ? payload.artifactUrl : undefined;
  const artifactDigest = typeof payload.artifactDigest === 'string' ? payload.artifactDigest : undefined;
  return {
    runId,
    operation,
    // The legacy stream envelope wraps the usable artifact in `data`. Keep
    // that payload as raw input so the renderer can inspect known artifact
    // keys without fetching an arbitrary URL from the browser.
    raw: Object.keys(nested).length > 0 ? nested : parsed,
    genui: typeof genuiValue === 'string' ? genuiValue : undefined,
    cardSpec: payload.cardSpec,
    artifactUrl,
    artifactDigest,
  };
}
