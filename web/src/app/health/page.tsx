"use client";

import Status from "@/components/Status";
import { fmtTime, useApi } from "@/lib/api";

type Source = {
  id: string;
  name: string;
  kind: string;
  enabled: boolean;
  last_success_at: string | null;
  consecutive_failures: number;
  last_error: string | null;
  last_item_count: number;
  runs: number;
  ok_runs: number;
};

type Kpi = {
  backtest: { generated_at: string; golden_version: string; hit_rate: number | null; avg_lead_days: number | null; with_data: number; cases: number }[];
  ranker: { kind: string; trained_at: string; samples: number; train_auc: number | null } | null;
};

function KpiCard() {
  const { data } = useApi<Kpi>("/kpi");
  if (!data) return null;
  const last = data.backtest.at(-1);
  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h2>系统 KPI</h2>
      <div className="metrics">
        <div>
          回测命中率<b>{last?.hit_rate != null ? `${Math.round(last.hit_rate * 100)}%` : "—"}</b>
        </div>
        <div>
          平均领先天数<b>{last?.avg_lead_days ?? "—"}</b>
        </div>
        <div>
          有数据案例<b>{last ? `${last.with_data}/${last.cases}` : "—"}</b>
        </div>
        <div>
          排序模型<b>{data.ranker ? `${data.ranker.kind} · ${data.ranker.samples} 样本` : "未训练"}</b>
        </div>
      </div>
      {!last && <p className="muted">尚未回测：运行 gs backtest。</p>}
    </div>
  );
}

export default function HealthPage() {
  const { data, error, loading } = useApi<Source[]>("/health/sources?hours=24");
  const rows = (data ?? []).filter((s) => s.enabled);
  return (
    <>
      <h1>数据源健康（24 小时）</h1>
      <Status loading={loading} error={error} empty={!!data && rows.length === 0} />
      {rows.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>源</th>
              <th>类型</th>
              <th>最近成功</th>
              <th className="num">成功 / 运行</th>
              <th className="num">上次条目</th>
              <th className="num">连续失败</th>
              <th>最近错误</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((s) => (
              <tr key={s.id}>
                <td>
                  {s.name} <span className="muted">{s.id}</span>
                </td>
                <td>{s.kind}</td>
                <td>{fmtTime(s.last_success_at)}</td>
                <td className="num">
                  {s.ok_runs} / {s.runs}
                </td>
                <td className="num">{s.last_item_count}</td>
                <td className="num" style={{ color: s.consecutive_failures ? "var(--bad)" : undefined }}>
                  {s.consecutive_failures}
                </td>
                <td className="muted">{(s.last_error ?? "").slice(0, 120)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <KpiCard />
    </>
  );
}
