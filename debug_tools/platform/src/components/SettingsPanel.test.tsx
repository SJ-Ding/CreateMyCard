import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { WorkbenchProvider } from '../context';
import { SettingsPanel } from './SettingsPanel';

describe('SettingsPanel', () => {
  afterEach(() => {
    cleanup();
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it('defaults to deployed service and allows selecting managed service', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ checks: [], status: [] }),
    } as Response);
    render(<WorkbenchProvider><SettingsPanel /></WorkbenchProvider>);
    expect(screen.queryByText('分页参数')).not.toBeInTheDocument();
    const deployed = screen.getByRole('radio', { name: /使用已部署微服务/ });
    const managed = screen.getByRole('radio', { name: /使用本地受管微服务/ });
    expect(deployed).toBeChecked();
    expect(screen.queryByText('Main Agent WebSocket')).not.toBeInTheDocument();
    expect(screen.getByText('微服务 WebSocket Base')).toBeInTheDocument();
    expect(screen.queryByText('可选微服务参数')).not.toBeInTheDocument();
    await userEvent.click(managed);
    expect(managed).toBeChecked();
    expect(screen.queryByText('微服务 WebSocket Base')).not.toBeInTheDocument();
    expect(screen.getByText('可选微服务参数')).toBeInTheDocument();
  });

  it('shows local Node and DevEco tool availability', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        checks: [
          { name: 'Node', ok: true, detail: 'D:/DevEco/tools/node/node.exe（来源：deveco）' },
          { name: 'HDC', ok: true, detail: 'D:/DevEco/sdk/hdc.exe' },
          { name: 'Hvigor', ok: false, detail: 'D:/DevEco/tools/hvigor/hvigorw.js' },
          { name: '工程', ok: false, detail: '未配置' },
        ],
        status: [],
      }),
    } as Response);

    render(<WorkbenchProvider><SettingsPanel /></WorkbenchProvider>);

    expect(screen.getByRole('complementary', { name: '本机工具链状态' })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('每 20 秒自动检查')).toBeInTheDocument());
    expect(screen.getByText('Node').closest('.backend-status-item')).toHaveClass('available');
    expect(screen.getByText('HDC').closest('.backend-status-item')).toHaveClass('available');
    expect(screen.getByText('Hvigor').closest('.backend-status-item')).toHaveClass('unavailable');
    expect(screen.queryByText('工程')).not.toBeInTheDocument();
  });
});
