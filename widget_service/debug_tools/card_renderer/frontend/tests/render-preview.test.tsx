// @vitest-environment jsdom

import React from 'react';
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CardRenderer } from '../src/CardRenderer';
import { CardPreview } from '../src/render';
import { parseInput } from '../src/parser';

afterEach(cleanup);

describe('Render 内核预览', () => {
  it('使用 renderTree 绘制并安全回传点击动作', async () => {
    const source = [
      '["root","Column",{"onClick":[{"call":"clickToDeeplink","args":{"uri":"demo://card"}}]},["text"]]',
      '["text","Text",{"content":"打开详情"}]',
    ].join('\n');
    const document = parseInput(source);
    const onAction = vi.fn();

    const { container } = render(<CardPreview document={document} onAction={onAction} />);

    expect(container.querySelector('[data-renderer-engine="render"]')).toBeTruthy();
    fireEvent.click(container.querySelector('[data-node-id="root"]') as HTMLElement);
    await waitFor(() => expect(onAction).toHaveBeenCalledOnce());
    expect(onAction.mock.calls[0][0]).toEqual({
      functionCall: { call: 'clickToDeeplink', args: { uri: 'demo://card' } },
    });
    expect(onAction.mock.calls[0][1].id).toBe('root');
  });

  it('按 assetBaseUrl 重写本地素材路径', () => {
    const document = parseInput(
      '["root","Image",{"src":"resources/base/media/sun_max.svg","width":20,"height":20}]',
    );
    const { container } = render(
      <CardPreview document={document} assetBaseUrl="/custom-assets/" />,
    );

    expect(container.querySelector('img')).toHaveAttribute(
      'src',
      '/custom-assets/base/media/sun_max.svg',
    );
  });

  it('TextInput 写回 DataModel 后局部刷新绑定文本', async () => {
    const source = [
      '{"version":"v0.9","createSurface":{"surfaceId":"form"}}',
      '{"version":"v0.9","updateComponents":{"surfaceId":"form","root":"root","components":[' +
        '{"id":"root","component":"Column","children":["input","echo"]},' +
        '{"id":"input","component":"Extended.TextInput","text":{"path":"/name"}},' +
        '{"id":"echo","component":"Text","content":{"path":"/name"}}]}}',
      '{"version":"v0.9","updateDataModel":{"surfaceId":"form","path":"/name","value":"初始值"}}',
    ].join('\n');
    const document = parseInput(source);

    render(<CardPreview document={document} />);
    const input = screen.getByRole('textbox');
    expect(input).toHaveValue('初始值');
    fireEvent.change(input, { target: { value: '已编辑' } });

    await waitFor(() => expect(screen.getByText('已编辑')).toBeInTheDocument());
    expect(input).toHaveValue('已编辑');
  });

  it('保留 CardRenderer 编辑器与自动渲染状态', async () => {
    render(<CardRenderer initialValue='["root","Text",{"content":"迁移完成"}]' />);

    expect(screen.getByRole('textbox', { name: 'DSL 输入' })).toHaveValue(
      '["root","Text",{"content":"迁移完成"}]',
    );
    await waitFor(() => expect(screen.getByText(/已渲染 1 个组件/)).toBeInTheDocument());
    expect(screen.getByText('迁移完成')).toBeInTheDocument();
  });
});
