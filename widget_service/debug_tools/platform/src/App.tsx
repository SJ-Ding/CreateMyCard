import { Navigate, NavLink, Route, Routes } from 'react-router-dom';
import { WorkbenchProvider } from './context';
import CallHistory from './components/CallHistory';
import SettingsPanel from './components/SettingsPanel';
import { EndToEndRoute } from './routes/EndToEndRoute';
import { InterfaceRoute } from './routes/InterfaceRoute';
import { RendererRoute } from './routes/RendererRoute';

const navigation = [
  { path: '/end-to-end', label: '端到端调试', detail: 'Main Agent · Agent 调试' },
  { path: '/interface', label: '接口调试', detail: 'WebSocket · API 调试' },
  { path: '/renderer', label: '卡片渲染', detail: 'GenUI · 预览检查' },
  { path: '/settings', label: '连接配置', detail: '地址 · 固定参数' },
];

function Shell() {
  return (
    <div className="workbench-shell">
      <aside className="navigation-rail">
        <div className="brand-lockup">
          <div className="brand-mark" aria-hidden="true">&lt;/&gt;</div>
          <div>
            <strong>AI Widget</strong>
            <span>Debug Workbench</span>
          </div>
        </div>
        <nav aria-label="调试模块">
          {navigation.map((item) => (
            <NavLink
              className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
              to={item.path}
              key={item.path}
            >
              <span className="nav-item-icon" aria-hidden="true">{item.label.slice(0, 1)}</span>
              <span>
                <strong>{item.label}</strong>
                <small>{item.detail}</small>
              </span>
            </NavLink>
          ))}
        </nav>
        <CallHistory />
        <div className="rail-footer">统一测试入口 · 本地调试</div>
      </aside>

      <main className="workbench-main">
        <section className="route-content">
          <Routes>
            <Route path="/end-to-end" element={<EndToEndRoute />} />
            <Route path="/interface" element={<InterfaceRoute />} />
            <Route path="/renderer" element={<RendererRoute />} />
            <Route path="/settings" element={<SettingsPanel />} />
            <Route path="*" element={<Navigate replace to="/end-to-end" />} />
          </Routes>
        </section>
      </main>
    </div>
  );
}

export default function App() {
  return (
    <WorkbenchProvider>
      <Shell />
    </WorkbenchProvider>
  );
}
