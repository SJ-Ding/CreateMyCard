// @vitest-environment jsdom
import React from 'react';
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CardRenderer } from '../../../card_renderer/frontend/src/CardRenderer';
import { RendererDebugPanel, RendererDebugProvider } from '../../../card_renderer/frontend/src/RendererDebug';
import { parseInput } from '../../../card_renderer/frontend/src/parser';
import { MOCK_APPLY_DELAY } from '../../../card_renderer/frontend/src/MockDataPanel';
import { artifactObject, bindingPaths, inferGroups, readPointer, safeDataPath, schemaValue, validateValue,
  type MockCatalog, type Schema } from '../../../card_renderer/frontend/src/mock-data';

const schema: Schema = { type: 'object', properties: {
  value: { type: 'number', description: '数值参数说明', sampleValue: 25, minimum: 0, maximum: 100 },
  label: { type: 'string', sampleValue: '样例' },
  enabled: { type: 'boolean', sampleValue: true },
  items: { type: 'array', items: { type: 'object', properties: { name: { type: 'string', sampleValue: '条目' } } } },
} };
const catalog: MockCatalog = { registryVersion: 'test', source: 'default', availableVersions: ['test'],
  capabilities: [{ id: 'TestData', description: '测试数据', defaultWriteResultTo: '/data/demo', outputSchema: schema }] };
const source = [
  { version: 'v0.9', createSurface: { surfaceId: 'preview', width: 150, height: 150 } },
  { version: 'v0.9', updateComponents: { surfaceId: 'preview', root: 'root', components: [
    { id: 'root', component: 'Column', children: ['text'], styles: { width: 'matchParent', height: 'matchParent' },
      onClick: [{ call: 'openDetail', args: { current: { path: '/data/demo/value' } } }] },
    { id: 'text', component: 'Text', content: { path: '/data/demo/value' },
      styles: { fontColor: "{{${/data/demo/value} > 50 ? '#FFFF0000' : '#FF000000'}}" } },
  ] } },
  { version: 'v0.9', updateDataModel: { surfaceId: 'preview', path: '/', value: { data: { demo: { value: 0 } } } } },
].map(row => JSON.stringify(row)).join('\n');

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('模拟数据定义与推断', () => {
  it('保留 false、0、空字符串和 null，缺失字段从样例补齐', () => {
    expect(schemaValue(schema, { value: 0, label: '', enabled: false, items: [] })).toEqual({
      value: 0, label: '', enabled: false, items: [],
    });
    expect(schemaValue({ type: 'null' }, null)).toBeNull();
    expect(recordSample()).toEqual({ value: 25, label: '样例', enabled: true, items: [{ name: '条目' }] });
  });
  it.each([['/data/demo/value', 0], ['/data/missing', undefined], ['/data/null', null]])(
    '读取路径 %s 保留真实类型', (path, expected) => {
      expect(readPointer({ data: { demo: { value: 0 }, null: null } }, path as string)).toBe(expected);
    });
  it('正确解码 JSON Pointer 转义并拒绝原型路径', () => {
    expect(readPointer({ data: { 'a/b': { '~key': false } } }, '/data/a~1b/~0key')).toBe(false);
    expect(safeDataPath('/data/__proto__/x')).toBe(false);
    expect(safeDataPath('/outside/x')).toBe(false);
    expect(safeDataPath('/data/constructor/x')).toBe(false);
  });
  it.each([
    ['text', { type: 'number' }, '应为 number'],
    [1.5, { type: 'integer' }, '应为 integer'],
    [101, { type: 'number', maximum: 100 }, '最大值'],
    [-1, { type: 'number', minimum: 0 }, '最小值'],
    ['', { type: 'string', minLength: 1 }, '过短'],
    ['abc', { type: 'string', pattern: '^x' }, '格式'],
    [[], { type: 'array', minItems: 1 }, '条目过少'],
    [{}, { type: 'object', required: ['a'] }, '必填'],
    [false, { type: 'string' }, '应为 string'],
  ])('错误值 %j 报告具体字段', (value, definition, message) => {
    expect(validateValue(value, definition as Schema, '/data/demo').join('\n')).toContain(message);
    expect(validateValue(value, definition as Schema, '/data/demo')[0]).toContain('/data/demo');
  });
  it('扫描属性、样式与事件参数中的绑定并按默认路径匹配接口', async () => {
    const document = await parseInput(source);
    expect(bindingPaths(document)).toContain('/data/demo/value');
    expect(inferGroups(document, null, catalog)).toEqual([
      { path: '/data/demo', capabilityId: 'TestData', candidates: ['TestData'], declared: false },
    ]);
  });
  it('完整产物的接口优先于路径推断，未知接口不伪造定义', async () => {
    const document = await parseInput(source);
    const groups = inferGroups(document, { cardSpec: { dataBindings: [
      { capabilityId: 'Unavailable', writeResultTo: '/data/demo' },
    ] } }, catalog);
    expect(groups).toHaveLength(1);
    expect(groups[0].capabilityId).toBe('Unavailable');
    expect(groups[0].declared).toBe(true);
  });
  it('相同字段对应多个接口时保留候选并等待手动选择', async () => {
    const document = await parseInput(source.replaceAll('/data/demo/', '/data/custom/'));
    const groups = inferGroups(document, null, { ...catalog, capabilities: [catalog.capabilities[0],
      { ...catalog.capabilities[0], id: 'OtherData' }] });
    expect(groups[0].capabilityId).toBe('');
    expect(groups[0].candidates).toEqual(['TestData', 'OtherData']);
  });
  it('提取具名 Markdown 产物块', () => {
    expect(artifactObject('```cardspec\n{"dataBindings":[]}\n```\n```meta\n{"capabilityRegistryVersion":"v1"}\n```'))
      .toEqual({ cardSpec: { dataBindings: [] }, meta: { capabilityRegistryVersion: 'v1' } });
  });
  it('规范化批测小写块名与 JSON 字符串，保持完整版本元数据', () => {
    expect(artifactObject({ blocks: { cardspec: '{"dataBindings":[]}',
      taskspec: { appVersion: '11.7.7.330' }, meta: { capabilityRegistryVersion: 'test' } } }))
      .toMatchObject({ cardSpec: { dataBindings: [] }, taskSpec: { appVersion: '11.7.7.330' },
        meta: { capabilityRegistryVersion: 'test' } });
  });
  it('相对列表绑定按模板作用域识别字段', async () => {
    const document = await parseInput(listSource());
    expect(bindingPaths(document)).toContain('/data/test/items/0/name');
  });
});

function recordSample() { return schemaValue(schema); }
function DebugFixture() {
  return <RendererDebugProvider><CardRenderer initialValue={source} debugTools />
    <RendererDebugPanel artifactPanel={<p>原始产物</p>} /></RendererDebugProvider>;
}

function listSource() {
  return [
    { version: 'v0.9', createSurface: { surfaceId: 'preview' } },
    { version: 'v0.9', updateComponents: { surfaceId: 'preview', root: 'root', components: [
      { id: 'root', component: 'Column', children: ['list'] },
      { id: 'list', component: 'List', children: { componentId: 'row', path: '/data/test/items' } },
      { id: 'row', component: 'Column', children: ['label'] },
      { id: 'label', component: 'Text', content: { path: 'name' } },
      { id: 'detached', component: 'Text', content: '未挂载' },
    ] } },
    { version: 'v0.9', updateDataModel: { surfaceId: 'preview', path: '/', value: {
      data: { test: { items: [{ name: '第一项' }, { name: '第二项' }] } },
    } } },
  ].map(row => JSON.stringify(row)).join('\n');
}

describe('渲染检查交互', () => {
  it('其他页面的普通预览不出现组件检查工具', async () => {
    render(<CardRenderer initialValue={source} previewOnly />);
    await screen.findByText('0');
    expect(screen.queryByRole('checkbox', { name: '检查组件' })).not.toBeInTheDocument();
  });
  it('重复模板实例分别进入树，未挂载节点仍展示定义', async () => {
    render(<RendererDebugProvider><CardRenderer initialValue={listSource()} debugTools />
      <RendererDebugPanel artifactPanel={null} /></RendererDebugProvider>);
    await screen.findByText('第一项');
    await waitFor(() => expect(screen.getByRole('button', { name: /Text label \[0\]/ })).toBeInTheDocument());
    expect(screen.getByRole('button', { name: /Text label \[1\]/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Text detached 无可见区域/ }));
    expect(screen.getByLabelText('组件详情')).toHaveTextContent('无可见区域');
  });
  it('默认普通点击显示事件弹窗；Esc 关闭并恢复焦点', async () => {
    const view = render(<DebugFixture />);
    await waitFor(() => expect(screen.getByText('0')).toBeInTheDocument());
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 50)); });
    const renderButton = screen.getByRole('button', { name: '渲染' }); renderButton.focus();
    fireEvent.click(view.container.querySelector('[data-node-id="root"]')!);
    const dialog = await screen.findByRole('dialog', { name: '点击事件详情' });
    expect(dialog).toHaveTextContent('openDetail');
    expect(dialog).toHaveTextContent('"current": 0');
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(renderButton).toHaveFocus();
  });
  it('开启检查后拦截事件并选中组件；树可反向定位', async () => {
    const view = render(<DebugFixture />);
    await waitFor(() => expect(screen.getByText('0')).toBeInTheDocument());
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 50)); });
    fireEvent.click(screen.getAllByRole('checkbox', { name: '检查组件' })[0]);
    fireEvent.click(view.container.querySelector('[data-node-id="text"]')!);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText('组件详情')).toHaveTextContent('text'));
    fireEvent.click(screen.getByRole('button', { name: 'Column root 无可见区域' }));
    expect(screen.getByLabelText('组件详情')).toHaveTextContent('root');
  });
  it('模拟应用更新文字、动态颜色与点击参数，错误输入保留成功预览', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => catalog }));
    const view = render(<DebugFixture />);
    await waitFor(() => expect(screen.getByText('0')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('tab', { name: '模拟数据' }));
    const input = await screen.findByRole('spinbutton', { name: '/data/demo/value' });
    fireEvent.change(input, { target: { value: '75' } });
    expect(screen.queryByRole('button', { name: '应用' })).not.toBeInTheDocument();
    expect(screen.queryByText('数值参数说明')).not.toBeInTheDocument();
    expect(screen.queryByText('测试数据')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('75')).toHaveStyle({ color: 'rgb(255, 0, 0)' }));
    fireEvent.click(view.container.querySelector('[data-node-id="root"]')!);
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('"current": 75');
    fireEvent.click(screen.getByRole('button', { name: '关闭' }));
    fireEvent.change(input, { target: { value: '101' } });
    await screen.findByText(/大于最大值/);
    expect(screen.getByText('75')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '恢复原始数据' }));
    expect(screen.getByText('0')).toBeInTheDocument();
  });
  it('数组增删及切换检查标签保留编辑值并自动刷新', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => catalog }));
    render(<DebugFixture />);
    await screen.findByText('0');
    fireEvent.click(screen.getByRole('tab', { name: '模拟数据' }));
    const input = await screen.findByRole('spinbutton', { name: '/data/demo/value' });
    fireEvent.change(input, { target: { value: '42' } });
    fireEvent.click(screen.getByRole('button', { name: '添加条目' }));
    expect(screen.getByRole('textbox', { name: '/data/demo/items/1/name' })).toHaveValue('条目');
    fireEvent.click(screen.getByRole('button', { name: '删除第 1 项' }));
    expect(screen.queryByRole('textbox', { name: '/data/demo/items/1/name' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: '组件树' }));
    fireEvent.click(screen.getByRole('tab', { name: '模拟数据' }));
    expect(screen.getByRole('spinbutton', { name: '/data/demo/value' })).toHaveValue(42);
    await screen.findByText('42');
  });
  it('连续编辑在停止 400 毫秒后仅应用最后一个值', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => catalog }));
    render(<DebugFixture />);
    await screen.findByText('0');
    fireEvent.click(screen.getByRole('tab', { name: '模拟数据' }));
    const input = await screen.findByRole('spinbutton', { name: '/data/demo/value' });
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    fireEvent.change(input, { target: { value: '10' } });
    await act(async () => { vi.advanceTimersByTime(250); });
    expect(screen.getByText('0')).toBeInTheDocument();
    fireEvent.change(input, { target: { value: '20' } });
    await act(async () => { vi.advanceTimersByTime(MOCK_APPLY_DELAY - 1); });
    expect(screen.getByText('0')).toBeInTheDocument();
    await act(async () => { vi.advanceTimersByTime(1); });
    expect(screen.getByText('20')).toBeInTheDocument();
    expect(screen.queryByText('10')).not.toBeInTheDocument();
  });
  it('恢复原始数据取消等待任务，填入样例会自动刷新', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => catalog }));
    render(<DebugFixture />);
    await screen.findByText('0');
    fireEvent.click(screen.getByRole('tab', { name: '模拟数据' }));
    const input = await screen.findByRole('spinbutton', { name: '/data/demo/value' });
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    fireEvent.change(input, { target: { value: '50' } });
    await act(async () => { vi.advanceTimersByTime(200); });
    fireEvent.click(screen.getByRole('button', { name: '恢复原始数据' }));
    await act(async () => { vi.advanceTimersByTime(1000); });
    expect(screen.getByText('0')).toBeInTheDocument();
    expect(input).toHaveValue(0);
    fireEvent.click(screen.getByRole('button', { name: '填入接口样例' }));
    await act(async () => { vi.advanceTimersByTime(MOCK_APPLY_DELAY); });
    expect(screen.getByText('25')).toBeInTheDocument();
  });
  it('无效 JSON 不覆盖预览，修正后无需按钮自动恢复', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => catalog }));
    render(<DebugFixture />);
    await screen.findByText('0');
    fireEvent.click(screen.getByRole('tab', { name: '模拟数据' }));
    await screen.findByRole('spinbutton', { name: '/data/demo/value' });
    fireEvent.click(screen.getByRole('button', { name: '切换为 JSON' }));
    const input = screen.getByRole('textbox', { name: '/data/demo 模拟 JSON' });
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    fireEvent.change(input, { target: { value: '{' } });
    await act(async () => { vi.advanceTimersByTime(MOCK_APPLY_DELAY); });
    expect(screen.getByText('JSON 格式不合法')).toBeInTheDocument();
    expect(screen.getByText('0')).toBeInTheDocument();
    fireEvent.change(input, { target: { value: '{"value":35}' } });
    await act(async () => { vi.advanceTimersByTime(MOCK_APPLY_DELAY); });
    expect(screen.getByText('35')).toBeInTheDocument();
  });
  it('多组数据同时到期时合并更新，不覆盖彼此', async () => {
    const multiple = source.replace('"children":["text"]', '"children":["text","other"]')
      .replace('{"id":"text",', '{"id":"other","component":"Text","content":{"path":"/data/other/value"}},{"id":"text",');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ...catalog,
      capabilities: [...catalog.capabilities, { ...catalog.capabilities[0], id: 'OtherData', defaultWriteResultTo: '/data/other' }],
    }) }));
    render(<RendererDebugProvider><CardRenderer initialValue={multiple} debugTools />
      <RendererDebugPanel artifactPanel={null} /></RendererDebugProvider>);
    await screen.findByText('0');
    fireEvent.click(screen.getByRole('tab', { name: '模拟数据' }));
    const first = await screen.findByRole('spinbutton', { name: '/data/demo/value' });
    const second = await screen.findByRole('spinbutton', { name: '/data/other/value' });
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    fireEvent.change(first, { target: { value: '10' } });
    fireEvent.change(second, { target: { value: '20' } });
    await act(async () => { vi.advanceTimersByTime(MOCK_APPLY_DELAY); });
    expect(screen.getByText('10')).toBeInTheDocument();
    expect(screen.getByText('20')).toBeInTheDocument();
  });
});
