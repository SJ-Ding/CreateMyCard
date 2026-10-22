// @vitest-environment jsdom

import React from 'react';
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { resolvePathBindingsInValue } from '@genui-sdk/interactions';
import { parseInput, resolveProps, resolveTemplate } from '../../../card_renderer/frontend/src/parser';
import { CardPreview, colorValue } from '../../../card_renderer/frontend/src/render';

const color = "{{${/data/countdown/countdownDays} % 1000 == 0 ? '#FFCD7F21' : "
  + "(${/data/countdown/countdownDays} % 100 == 0 ? '#FFC0C0C0' : '#FFFFFFFF')}}";
const cases = [
  { days: 1000, hex: '#FFCD7F21', css: 'rgb(205, 127, 33)' },
  { days: 100, hex: '#FFC0C0C0', css: 'rgb(192, 192, 192)' },
  { days: 99, hex: '#FFFFFFFF', css: 'rgb(255, 255, 255)' },
];

function source(days: number, padded = false) {
  const expression = padded ? ` \n${color}\n ` : color;
  return [
    { version: 'v0.9', createSurface: { surfaceId: 'preview', width: 300, height: 150 } },
    { version: 'v0.9', updateComponents: { surfaceId: 'preview', root: 'root', components: [
      { id: 'root', component: 'Column', children: ['text', 'button', 'gradient'],
        styles: { width: 'matchParent', height: 'matchParent', backgroundColor: expression } },
      { id: 'text', component: 'Text', content: '倒计时颜色',
        styles: { fontColor: expression, fontSize: 20 } },
      { id: 'button', component: 'Button', label: '动态按钮',
        styles: { backgroundColor: expression, fontColor: '#FF000000' } },
      { id: 'gradient', component: 'Column', children: [], styles: {
        width: 100, height: 30,
        linearGradient: { angle: 90, colors: [[expression, 0], ['#FF000000', 1]] },
      } },
    ] } },
    { version: 'v0.9', updateDataModel: {
      surfaceId: 'preview', path: '/', value: { data: { countdown: { countdownDays: days } } },
    } },
  ].map(row => JSON.stringify(row)).join('\n');
}

afterEach(cleanup);

describe('动态颜色与背景', () => {
  it.each(cases)('模板辅助入口计算 $days 天的嵌套三元表达式', ({ days, hex }) => {
    const data = { data: { countdown: { countdownDays: days } } };
    expect(resolveTemplate(color, data)).toBe(hex);
    expect(resolveProps({ styles: { fontColor: color, backgroundColor: color } }, data))
      .toEqual({ styles: { fontColor: hex, backgroundColor: hex } });
  });

  it.each([false, true])('颜色、按钮背景与渐变随数据重新渲染，外层空白=%s', async padded => {
    const view = render(<div />);
    for (const { days, hex, css } of cases) {
      view.rerender(<CardPreview document={await parseInput(source(days, padded))} />);
      expect(screen.getByText('倒计时颜色')).toHaveStyle({ color: css });
      expect(view.container.querySelector('[data-node-id="root"]')).toHaveStyle({ backgroundColor: css });
      expect(screen.getByRole('button', { name: '动态按钮' })).toHaveStyle({ backgroundColor: css });
      const gradient = view.container.querySelector<HTMLElement>('[data-node-id="gradient"]');
      expect(gradient?.style.backgroundImage).toContain('linear-gradient(90deg');
      expect(gradient?.style.backgroundImage).toContain(`${colorValue(hex)} 0%`);
    }
  });

  it('运行时保留表达式结果类型并短路未选中的分支', () => {
    const get = () => 1000;
    expect(resolvePathBindingsInValue(` \n${color}\n `, get)).toBe('#FFCD7F21');
    expect(resolvePathBindingsInValue('{{ true ? 7 : 1 / 0 }}', get)).toBe(7);
    expect(resolvePathBindingsInValue('{{ false ? 1 / 0 : false }}', get)).toBe(false);
  });

  it('模板辅助入口保留历史文字、拼接及相对路径', () => {
    const data = { weather: { city: '上海' }, data: { value: 3 } };
    expect(resolveTemplate('天气：{{weather.city}}', data)).toBe('天气：上海');
    expect(resolveTemplate("{{ '第' + ${/data/value} + '天' }}", data)).toBe('第3天');
    expect(resolveTemplate('{{ true ? 7 : 1 / 0 }}', data)).toBe('7');
    expect(resolveTemplate('{{ invalid(1) }}', data)).toBe('{{ invalid(1) }}');
    expect(resolvePathBindingsInValue(' 普通文字 ', () => 0)).toBe(' 普通文字 ');
  });

  it('无效表达式报告错误且不执行任意脚本', () => {
    expect(() => resolvePathBindingsInValue('{{ globalThis.alert(1) }}', () => 0)).toThrow();
    expect(() => resolvePathBindingsInValue('{{ true ? 1 }}', () => 0)).toThrow();
  });
});
