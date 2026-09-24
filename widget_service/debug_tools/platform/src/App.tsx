import { Navigate, NavLink, Route, Routes, useLocation } from 'react-router-dom';
import { WorkbenchProvider, useWorkbench } from './context';
import { EndToEndRoute } from './routes/EndToEndRoute';
import { InterfaceRoute } from './routes/InterfaceRoute';
import { RendererRoute } from './routes/RendererRoute';

const navigation = [
  { path: '/end-to-end', label: '端到端调试', detail: 'Main Agent · Agent 调试' },
  { path: '/interface', label: '接口调试', detail: 'WebSocket · API 调试' },
  { path: '/renderer', label: '卡片渲染', detail: 'GenUI · 预览检查' },
];

function Shell() {
  const location = useLocation();
  const { events } = useWorkbench();
  const activeLabel = navigation.find((item) => location.pathname.endsWith(item.path))?.label ?? '调试平台';

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
        <div className="rail-footer">统一测试入口 · 本地调试</div>
      </aside>

      <main className="workbench-main">
        <header className="topbar">
          <div>
            <span className="topbar-kicker">DEBUG PLATFORM</span>
            <h1>{activeLabel}</h1>
          </div>
          <div className="topbar-status">
            <span className="status-dot online" aria-hidden="true" />
            <span>本地工作台</span>
            <span className="event-count">事件 {events.length}</span>
          </div>
        </header>
        <section className="route-content">
          <Routes>
            <Route path="/end-to-end" element={<EndToEndRoute />} />
            <Route path="/interface" element={<InterfaceRoute />} />
            <Route path="/renderer" element={<RendererRoute />} />
            <Route path="*" element={<Navigate replace to="/end-to-end" />} />
          </Routes>
        </section>
        <EventTimeline />
      </main>
    </div>
  );
}

function EventTimeline() {
  const { events } = useWorkbench();
  return (
    <section className="event-timeline" aria-label="WebSocket 事件">
      <div className="section-title-row">
        <div>
          <span className="section-kicker">TRACE</span>
          <h2>WebSocket 事件</h2>
        </div>
        <span className="muted-label">最近 {Math.min(events.length, 8)} 条</span>
      </div>
      {events.length === 0 ? (
        <p className="empty-row">等待调试事件…</p>
      ) : (
        <div className="event-list">
          {events.slice(-8).reverse().map((event) => (
            <div className="event-row" key={event.id}>
              <span className={`event-led ${event.direction}`} aria-hidden="true" />
              <time>{new Date(event.timestamp).toLocaleTimeString()}</time>
              <strong>{event.channel}</strong>
              <span>{event.kind}</span>
              <small>{event.durationMs ? `${event.durationMs} ms` : '—'}</small>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

export default function App() {
  return (
    <WorkbenchProvider>
      <Shell />
    </WorkbenchProvider>
  );
}
