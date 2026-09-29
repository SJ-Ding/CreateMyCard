import { useEffect, useMemo, useState } from 'react';
import { CardRenderer } from '@widget-debug/card-renderer';
import {
  BatchAttempt,
  BatchDatasetItem,
  BatchRun,
  BatchSampleDetail,
  cancelBatchRun,
  getBatchRun,
  getBatchSample,
  listBatchDatasets,
  listBatchRuns,
  startBatchRun,
} from '../batchApi';
import { DEFAULT_ASSET_BASE_URL } from '../config';
import { useWorkbench } from '../context';
import { normalizeToolWebSocketUrl } from '../toolBridge';

const ACTIVE_STATUSES = new Set(['queued', 'running']);

function jsonText(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? '';
  } catch {
    return String(value);
  }
}

function statusLabel(status: string): string {
  return ({
    queued: '等待中',
    running: '运行中',
    success: '成功',
    degraded: '降级',
    failed: '失败',
    unsupported: '不支持',
    cancelled: '已取消',
    completed: '已完成',
  } as Record<string, string>)[status] ?? status;
}

function finalAttempt(detail: BatchSampleDetail | null): BatchAttempt | null {
  if (!detail?.attempts.length) return null;
  const index = detail.summary.finalAttempt;
  if (typeof index === 'number') {
    return detail.attempts.find((item) => item.name === `attempt_${String(index).padStart(3, '0')}`)
      ?? detail.attempts.at(-1)
      ?? null;
  }
  return detail.attempts.at(-1) ?? null;
}

export function BatchRoute() {
  const { config } = useWorkbench();
  const [datasets, setDatasets] = useState<BatchDatasetItem[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState('');
  const [concurrency, setConcurrency] = useState(4);
  const [maxRetries, setMaxRetries] = useState(1);
  const [runs, setRuns] = useState<BatchRun[]>([]);
  const [run, setRun] = useState<BatchRun | null>(null);
  const [selectedSampleId, setSelectedSampleId] = useState('');
  const [detail, setDetail] = useState<BatchSampleDetail | null>(null);
  const [detailTab, setDetailTab] = useState('preview');
  const [statusFilter, setStatusFilter] = useState('all');
  const [traceFilter, setTraceFilter] = useState('all');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  const refreshRuns = async () => {
    const items = await listBatchRuns();
    setRuns(items);
    return items;
  };

  useEffect(() => {
    let active = true;
    Promise.all([listBatchDatasets(), listBatchRuns()])
      .then(([datasetItems, runItems]) => {
        if (!active) return;
        setDatasets(datasetItems);
        setSelected(new Set(datasetItems.filter((item) => item.valid).map((item) => item.id)));
        setRuns(runItems);
        if (runItems.length) setRun(runItems[0]);
      })
      .catch((reason) => active && setError(reason instanceof Error ? reason.message : String(reason)))
      .finally(() => active && setLoading(false));
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!run || !ACTIVE_STATUSES.has(run.status)) return;
    const timer = window.setInterval(() => {
      getBatchRun(run.runId)
        .then((next) => {
          setRun(next);
          setRuns((current) => [next, ...current.filter((item) => item.runId !== next.runId)]);
        })
        .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)));
    }, 750);
    return () => window.clearInterval(timer);
  }, [run?.runId, run?.status]);

  useEffect(() => {
    if (!run || !selectedSampleId) {
      setDetail(null);
      return;
    }
    const sample = run.samples.find((item) => item.id === selectedSampleId);
    if (!sample || ACTIVE_STATUSES.has(sample.status) || sample.status === 'queued') return;
    getBatchSample(run.runId, selectedSampleId)
      .then(setDetail)
      .catch((reason) => setError(reason instanceof Error ? reason.message : String(reason)));
  }, [run, selectedSampleId]);

  const visibleDatasets = useMemo(() => {
    const term = search.trim().toLowerCase();
    if (!term) return datasets;
    return datasets.filter((item) => (
      `${item.id} ${item.title} ${item.query} ${item.size}`.toLowerCase().includes(term)
    ));
  }, [datasets, search]);

  const visibleSamples = useMemo(() => (run?.samples ?? []).filter((sample) => {
    if (statusFilter !== 'all' && sample.status !== statusFilter) return false;
    if (traceFilter === 'warning' && !['missing', 'parse_error'].includes(sample.traceStatus)) {
      return false;
    }
    if (traceFilter === 'available' && sample.traceStatus !== 'available') return false;
    return true;
  }), [run, statusFilter, traceFilter]);

  const activeAttempt = finalAttempt(detail);
  const progress = run?.total ? Math.round((run.completed / run.total) * 100) : 0;

  const start = async () => {
    setError('');
    try {
      const endpoint = normalizeToolWebSocketUrl(
        config.toolWsBaseUrl,
        'generateWidgetCardCompactDsl',
      );
      const created = await startBatchRun({
        sampleIds: Array.from(selected),
        toolWsBaseUrl: endpoint,
        concurrency,
        maxRetries,
      });
      setRun(created);
      setRuns((current) => [created, ...current]);
      setSelectedSampleId('');
      setDetail(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const cancel = async () => {
    if (!run) return;
    try {
      setRun(await cancelBatchRun(run.runId));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const toggleDataset = (id: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  return (
    <div className="batch-page">
      <header className="batch-heading">
        <div>
          <span className="section-kicker">BATCH DSL</span>
          <h1>批量生成测试</h1>
          <p>从 request_dataset 选择样本，并发调用 Compact DSL 接口并关联本地 Trace。</p>
        </div>
        <div className="batch-heading-actions">
          <label>并发数<input type="number" min="1" max="16" value={concurrency} onChange={(event) => setConcurrency(Number(event.target.value))} /></label>
          <label>失败重试<input type="number" min="0" max="5" value={maxRetries} onChange={(event) => setMaxRetries(Number(event.target.value))} /></label>
          <button type="button" className="batch-start" disabled={!selected.size || Boolean(run && ACTIVE_STATUSES.has(run.status))} onClick={start}>启动批跑</button>
          <button type="button" className="batch-cancel" disabled={!run || !ACTIVE_STATUSES.has(run.status)} onClick={cancel}>取消</button>
        </div>
      </header>
      {error && <div className="batch-error" role="alert">{error}</div>}
      <div className="batch-layout">
        <aside className="batch-datasets">
          <div className="batch-panel-heading"><strong>数据样本</strong><span>{selected.size}/{datasets.filter((item) => item.valid).length}</span></div>
          <input className="batch-search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索编号、标题或需求" />
          <div className="batch-selection-actions">
            <button type="button" onClick={() => setSelected(new Set(datasets.filter((item) => item.valid).map((item) => item.id)))}>全选</button>
            <button type="button" onClick={() => setSelected((current) => {
              const next = new Set<string>();
              datasets.filter((item) => item.valid).forEach((item) => {
                if (!current.has(item.id)) next.add(item.id);
              });
              return next;
            })}>反选</button>
            <button type="button" onClick={() => refreshRuns().catch(() => undefined)}>刷新历史</button>
          </div>
          <div className="batch-dataset-list">
            {loading && <p>正在读取数据集…</p>}
            {visibleDatasets.map((item) => (
              <label key={item.id} className={`batch-dataset${item.valid ? '' : ' is-invalid'}`}>
                <input type="checkbox" disabled={!item.valid} checked={selected.has(item.id)} onChange={() => toggleDataset(item.id)} />
                <span><strong>{item.id} · {item.title}</strong><small>{item.size || '未知尺寸'} · {item.query || item.error}</small></span>
              </label>
            ))}
          </div>
          <label className="batch-history-label">历史轮次
            <select value={run?.runId ?? ''} onChange={(event) => {
              const next = runs.find((item) => item.runId === event.target.value) ?? null;
              setRun(next);
              setSelectedSampleId('');
              setDetail(null);
            }}>
              <option value="">尚无记录</option>
              {runs.map((item) => <option key={item.runId} value={item.runId}>{item.runId}</option>)}
            </select>
          </label>
        </aside>

        <main className="batch-results">
          <section className="batch-summary">
            <div className="batch-progress"><span style={{ width: `${progress}%` }} /></div>
            <div className="batch-metrics">
              <span><b>{run?.completed ?? 0}/{run?.total ?? 0}</b>进度</span>
              <span><b>{run?.success ?? 0}</b>成功</span>
              <span><b>{run?.degraded ?? 0}</b>降级</span>
              <span><b>{run?.failed ?? 0}</b>失败</span>
              <span><b>{run?.averageElapsedMs ?? 0}ms</b>平均耗时</span>
              <span><b>{run?.traceWarnings ?? 0}</b>Trace 告警</span>
            </div>
            <div className="batch-run-meta"><span>{run ? `${statusLabel(run.status)} · ${run.runId}` : '选择样本后启动批跑'}</span><span>{run?.endpoint ?? config.toolWsBaseUrl}</span></div>
          </section>

          <section className="batch-workspace">
            <div className="batch-table-panel">
              <div className="batch-table-toolbar">
                <strong>样本结果</strong>
                <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
                  <option value="all">全部状态</option><option value="success">成功</option><option value="degraded">降级</option><option value="failed">失败</option><option value="unsupported">不支持</option>
                </select>
                <select value={traceFilter} onChange={(event) => setTraceFilter(event.target.value)}>
                  <option value="all">全部 Trace</option><option value="available">Trace 正常</option><option value="warning">Trace 告警</option>
                </select>
              </div>
              <div className="batch-table-scroll">
                <table><thead><tr><th>样本</th><th>状态</th><th>耗时</th><th>尝试</th><th>Trace</th></tr></thead>
                  <tbody>{visibleSamples.map((sample) => (
                    <tr key={sample.id} className={selectedSampleId === sample.id ? 'is-selected' : ''} onClick={() => setSelectedSampleId(sample.id)}>
                      <td><strong>{sample.id}</strong><small>{sample.title}</small></td>
                      <td><span className={`batch-status ${sample.status}`}>{statusLabel(sample.status)}</span>{sample.errorCode && <small>{sample.errorCode}</small>}</td>
                      <td>{sample.elapsedMs ? `${sample.elapsedMs}ms` : '—'}</td>
                      <td>{sample.attemptCount}</td>
                      <td><span className={`batch-trace ${sample.traceStatus}`}>{sample.traceStatus}</span><small>{sample.traceRecordCount} 条</small></td>
                    </tr>
                  ))}</tbody></table>
                {!visibleSamples.length && <div className="batch-empty">暂无匹配结果</div>}
              </div>
            </div>

            <div className="batch-detail-panel">
              <div className="batch-detail-tabs">
                {['preview', 'request', 'response', 'artifact', 'trace'].map((tab) => <button type="button" key={tab} className={detailTab === tab ? 'is-active' : ''} onClick={() => setDetailTab(tab)}>{({ preview: '预览', request: '请求', response: '响应', artifact: '产物', trace: 'Trace' } as Record<string, string>)[tab]}</button>)}
              </div>
              <div className="batch-detail-body">
                {!detail && <div className="batch-empty">选择已完成的样本查看详情</div>}
                {detail && detailTab === 'preview' && (activeAttempt?.genui
                  ? <CardRenderer key={`${run?.runId}-${selectedSampleId}`} initialValue={activeAttempt.genui} assetBaseUrl={DEFAULT_ASSET_BASE_URL} />
                  : <div className="batch-empty">该样本没有可预览的 GenUI</div>)}
                {detail && detailTab === 'request' && <pre>{jsonText(activeAttempt?.request)}</pre>}
                {detail && detailTab === 'response' && <pre>{jsonText(activeAttempt?.response)}</pre>}
                {detail && detailTab === 'artifact' && <pre>{jsonText(activeAttempt?.blocks)}</pre>}
                {detail && detailTab === 'trace' && <pre>{jsonText(activeAttempt?.trace)}</pre>}
              </div>
            </div>
          </section>
        </main>
      </div>
    </div>
  );
}
