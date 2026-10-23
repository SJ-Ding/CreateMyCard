import type { RendererDocument } from './parser';

export type JsonObject = Record<string, unknown>;
export type Schema = {
  type?: string; properties?: Record<string, Schema>; items?: Schema; required?: string[];
  description?: string; sampleValue?: unknown; enum?: unknown[]; minimum?: number; maximum?: number;
  minLength?: number; maxLength?: number; minItems?: number; maxItems?: number; pattern?: string;
  additionalProperties?: boolean;
};
export type MockCapability = {
  id: string; description: string; defaultWriteResultTo?: string; outputSchema: Schema;
};
export type MockCatalog = {
  registryVersion: string; source: string; availableVersions: string[]; capabilities: MockCapability[];
};
export type MockGroup = { path: string; capabilityId: string; candidates: string[]; declared: boolean };
export type DataUpdate = { path: string; value: unknown };

export function record(value: unknown): JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {};
}

export function pointerParts(path: string): string[] {
  return path.split('/').slice(1).map(part => part.replace(/~1/g, '/').replace(/~0/g, '~'));
}

export function readPointer(data: unknown, path: string): unknown {
  let value = data;
  for (const part of pointerParts(path)) {
    if (value === null || typeof value !== 'object' || !Object.prototype.hasOwnProperty.call(value, part)) return undefined;
    value = (value as JsonObject)[part];
  }
  return value;
}

export function safeDataPath(path: string): boolean {
  return path.startsWith('/data/') && pointerParts(path).every(part => (
    part.length > 0 && !['__proto__', 'prototype', 'constructor'].includes(part)
  ));
}

export function artifactObject(input: unknown): JsonObject {
  if (typeof input === 'string') {
    try { return artifactObject(JSON.parse(input)); } catch {
      const blocks: JsonObject = {};
      for (const match of input.matchAll(/```(?:json\s+)?(cardspec|cardSpec|meta|taskspec|taskSpec)\s*\n([\s\S]*?)```/g)) {
        try { blocks[match[1].toLowerCase() === 'cardspec' ? 'cardSpec' : match[1]] = JSON.parse(match[2]); }
        catch { /* 非 JSON 块不作为接口定义来源，解析器负责报告输入错误。 */ }
      }
      return artifactObject(blocks);
    }
  }
  let root = record(input);
  for (const key of ['artifact', 'blocks', 'raw', 'data']) {
    const nested = record(root[key]);
    if (nested.cardSpec || nested.cardspec || nested.meta) { root = nested; break; }
  }
  const normalized = { ...root };
  for (const [canonical, alias] of [['cardSpec', 'cardspec'], ['taskSpec', 'taskspec'], ['meta', 'meta']]) {
    const candidate = root[canonical] ?? root[alias];
    if (candidate === undefined) continue;
    if (typeof candidate === 'string') {
      try { normalized[canonical] = JSON.parse(candidate); } catch { normalized[canonical] = candidate; }
    } else normalized[canonical] = candidate;
  }
  if (root.dataBindings && !normalized.cardSpec) normalized.cardSpec = root;
  return normalized;
}

export function schemaValue(schema: Schema, current?: unknown): unknown {
  if (current === null) return null;
  if (schema.type === 'object') {
    const value = { ...record(current) };
    for (const [key, child] of Object.entries(schema.properties ?? {})) {
      if (['__proto__', 'constructor', 'prototype'].includes(key)) continue;
      value[key] = schemaValue(child, value[key]);
    }
    return value;
  }
  if (schema.type === 'array') {
    if (Array.isArray(current)) return current.map(item => schemaValue(schema.items ?? {}, item));
    return Array.from({ length: Math.max(1, schema.minItems ?? 0) }, () => schemaValue(schema.items ?? {}));
  }
  if (current !== undefined) return current;
  if (schema.sampleValue !== undefined) return schema.sampleValue;
  if (schema.enum?.length) return schema.enum[0];
  if (schema.type === 'boolean') return false;
  if (schema.type === 'number' || schema.type === 'integer') return schema.minimum ?? 0;
  if (schema.type === 'null') return null;
  return '';
}

export function validateValue(value: unknown, schema: Schema, path: string): string[] {
  const errors: string[] = [];
  const type = schema.type;
  const validType = type === undefined || (type === 'null' ? value === null
    : type === 'array' ? Array.isArray(value)
      : type === 'object' ? value !== null && typeof value === 'object' && !Array.isArray(value)
        : type === 'integer' ? typeof value === 'number' && Number.isInteger(value)
          : typeof value === type && (type !== 'number' || Number.isFinite(value)));
  if (!validType) return [`${path}: 应为 ${type}`];
  if (schema.enum && !schema.enum.some(item => JSON.stringify(item) === JSON.stringify(value))) {
    errors.push(`${path}: 值不在允许范围内`);
  }
  if (typeof value === 'number') {
    if (schema.minimum !== undefined && value < schema.minimum) errors.push(`${path}: 小于最小值 ${schema.minimum}`);
    if (schema.maximum !== undefined && value > schema.maximum) errors.push(`${path}: 大于最大值 ${schema.maximum}`);
  }
  if (typeof value === 'string') {
    if (schema.minLength !== undefined && value.length < schema.minLength) errors.push(`${path}: 字符串过短`);
    if (schema.maxLength !== undefined && value.length > schema.maxLength) errors.push(`${path}: 字符串过长`);
    if (schema.pattern && !new RegExp(schema.pattern).test(value)) errors.push(`${path}: 格式不符合定义`);
  }
  if (Array.isArray(value)) {
    if (schema.minItems !== undefined && value.length < schema.minItems) errors.push(`${path}: 数组条目过少`);
    if (schema.maxItems !== undefined && value.length > schema.maxItems) errors.push(`${path}: 数组条目过多`);
    value.forEach((item, index) => errors.push(...validateValue(item, schema.items ?? {}, `${path}/${index}`)));
  } else if (type === 'object') {
    const object = record(value);
    for (const key of schema.required ?? []) {
      if (!Object.prototype.hasOwnProperty.call(object, key)) errors.push(`${path}/${key}: 必填字段缺失`);
    }
    for (const [key, child] of Object.entries(object)) {
      const definition = schema.properties?.[key];
      if (definition) errors.push(...validateValue(child, definition, `${path}/${key}`));
      else if (schema.additionalProperties === false) errors.push(`${path}/${key}: 未声明字段`);
    }
  }
  return errors;
}

export function bindingPaths(document: RendererDocument): string[] {
  const paths = new Set<string>();
  const scan = (value: unknown, scope = ''): void => {
    if (typeof value === 'string') {
      for (const match of value.matchAll(/\/data\/[\w~./-]+/g)) paths.add(match[0]);
      if (scope) for (const match of value.matchAll(/\$\{([^}/][^}]*)\}/g)) paths.add(`${scope}/${match[1]}`);
    } else if (Array.isArray(value)) value.forEach(item => scan(item, scope));
    else if (value && typeof value === 'object') {
      const pointer = record(value).path;
      if (scope && typeof pointer === 'string' && !pointer.startsWith('/')) paths.add(`${scope}/${pointer}`);
      Object.values(value).forEach(item => scan(item, scope));
    }
  };
  const seen = new Set<string>();
  const scanNode = (id: string, scope: string): void => {
    if (seen.has(`${id}:${scope}`)) return;
    seen.add(`${id}:${scope}`);
    const node = document.graph.getNode(id);
    if (!node) return;
    scan(node.props, scope);
    for (const child of node.children) scanNode(child, scope);
  };
  for (const node of document.graph.getAllNodes().values()) {
    scan(node.props);
    if (node.dynamicChildrenTemplate) {
      scan(node.dynamicChildrenTemplate.path);
      scanNode(node.dynamicChildrenTemplate.componentId, `${node.dynamicChildrenTemplate.path}/0`);
    }
  }
  return [...paths].filter(safeDataPath);
}

function schemaHasPath(schema: Schema, parts: string[]): boolean {
  let child: Schema | undefined = schema;
  for (const part of parts) {
    if (child?.type === 'array' && /^\d+$/.test(part)) child = child.items;
    else child = child?.properties?.[part];
    if (!child) return false;
  }
  return true;
}

export function inferGroups(document: RendererDocument, artifact: unknown, catalog: MockCatalog): MockGroup[] {
  const root = artifactObject(artifact);
  const spec = record(root.cardSpec ?? root.cardspec ?? root.card_spec);
  const bindings = Array.isArray(spec.dataBindings) ? spec.dataBindings : [];
  const groups: MockGroup[] = [];
  for (const item of bindings) {
    const binding = record(item);
    if (typeof binding.writeResultTo !== 'string' || !safeDataPath(binding.writeResultTo)) continue;
    const capabilityId = typeof binding.capabilityId === 'string' ? binding.capabilityId : '';
    groups.push({ path: binding.writeResultTo, capabilityId, candidates: [capabilityId], declared: true });
  }
  const references = bindingPaths(document);
  const roots = new Set(references.map(path => '/' + path.split('/').slice(1, 3).join('/')));
  for (const path of roots) {
    if (groups.some(group => path === group.path || path.startsWith(group.path + '/'))) continue;
    const used = references.filter(ref => ref.startsWith(path + '/'));
    const defaults = catalog.capabilities.filter(capability => capability.defaultWriteResultTo === path);
    const candidates = defaults.length ? defaults : catalog.capabilities.filter(capability => (
      used.length > 0 && used.every(ref => schemaHasPath(capability.outputSchema,
        pointerParts(ref).slice(pointerParts(path).length)))
    ));
    groups.push({ path, capabilityId: candidates.length === 1 ? candidates[0].id : '',
      candidates: candidates.map(capability => capability.id), declared: false });
  }
  return groups;
}
