"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import Chart from "@/components/Chart";
import Status from "@/components/Status";
import { fmtTime, useApi } from "@/lib/api";

type Step = { region: string; first_seen: string; first_breakout: string | null; peak_z: number | null };
type Row = { id: number; name: string; origin_region: string | null; spread_path: Step[]; last_seen: string };

const COLORS: Record<string, string> = {
  GC: "#dc2626",
  NA: "#2563eb",
  SEA: "#16a34a",
  LATAM: "#f59e0b",
  EU: "#7c3aed",
};

export default function SpreadPage() {
  const [days, setDays] = useState(7);
  const { data, error, loading } = useApi<Row[]>(`/spread?days=${days}&limit=30`);

  const option = useMemo(() => {
    const rows = [...(data ?? [])].reverse();
    const names = rows.map((r) => (r.name.length > 24 ? `${r.name.slice(0, 23)}…` : r.name));
    const regions = [...new Set(rows.flatMap((r) => r.spread_path.map((p) => p.region)))];
    return {
      tooltip: {
        trigger: "item",
        formatter: (p: { seriesName: string; value: [string, number, number | null] }) =>
          `${rows[p.value[1]]?.name}<br/>${p.seriesName}：${fmtTime(p.value[0])}　峰值 z ${p.value[2] ?? "—"}`,
      },
      legend: { top: 0, data: regions },
      grid: { left: 220, right: 30, top: 30, bottom: 30 },
      xAxis: { type: "time" },
      yAxis: { type: "category", data: names },
      series: [
        ...rows.map((r, y) => ({
          type: "line",
          name: `path-${r.id}`,
          data: r.spread_path.map((p) => [p.first_breakout ?? p.first_seen, y]),
          lineStyle: { color: "#9ca3af" },
          symbol: "none",
          silent: true,
          tooltip: { show: false },
        })),
        ...regions.map((region) => ({
          type: "scatter",
          name: region,
          itemStyle: { color: COLORS[region] ?? "#6b7280" },
          symbolSize: 12,
          data: rows.flatMap((r, y) =>
            r.spread_path
              .filter((p) => p.region === region)
              .map((p) => [p.first_breakout ?? p.first_seen, y, p.peak_z]),
          ),
        })),
      ],
    };
  }, [data]);

  return (
    <>
      <h1>跨区扩散时间轴</h1>
      <p className="muted">每行一个话题，点的位置是该区域首次突破阈值（未突破则为首次出现）的时间，从左到右即扩散顺序。</p>
      <div className="toolbar">
        <label>最近</label>
        <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
          {[3, 7, 14, 30].map((d) => (
            <option key={d} value={d}>
              {d} 天
            </option>
          ))}
        </select>
      </div>
      <Status loading={loading} error={error} empty={!!data && data.length === 0} />
      {data && data.length > 0 && (
        <>
          <div className="card">
            <Chart option={option} height={Math.max(260, data.length * 30 + 80)} />
          </div>
          <table>
            <thead>
              <tr>
                <th>话题</th>
                <th>路径</th>
                <th>最近出现</th>
              </tr>
            </thead>
            <tbody>
              {data.map((r) => (
                <tr key={r.id}>
                  <td>
                    <Link href={`/topics/${r.id}`}>{r.name}</Link>
                  </td>
                  <td>{r.spread_path.map((p) => p.region).join(" → ")}</td>
                  <td>{fmtTime(r.last_seen)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </>
  );
}
