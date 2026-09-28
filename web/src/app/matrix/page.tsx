"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import Chart from "@/components/Chart";
import Status from "@/components/Status";
import { CATEGORY_LABELS, useApi, type Meta } from "@/lib/api";

type Row = {
  id: number;
  name: string;
  category: string | null;
  n_regions: number;
  best_z: number | null;
  z_by_region: Record<string, number | null>;
};

export default function MatrixPage() {
  const meta = useApi<Meta>("/meta");
  const [days, setDays] = useState(7);
  const { data, error, loading } = useApi<Row[]>(`/matrix?days=${days}&limit=30`);
  const regions = useMemo(
    () => (meta.data?.regions ?? []).map((r) => r.code).filter((c) => c !== "GLOBAL"),
    [meta.data],
  );

  const option = useMemo(() => {
    const rows = [...(data ?? [])].reverse();
    // 区域不在 z_by_region 中 = 无讨论（不画格子）；在但 z 为空 = 有讨论、历史不足以算 z（冷启动）
    const cells: { value: [number, number, number]; noZ: boolean }[] = [];
    rows.forEach((r, y) =>
      regions.forEach((c, x) => {
        if (!r.z_by_region || !(c in r.z_by_region)) return;
        const z = r.z_by_region[c];
        cells.push({ value: [x, y, z === null ? 0 : Number(z)], noZ: z === null });
      }),
    );
    return {
      tooltip: {
        formatter: (p: { data: { value: [number, number, number]; noZ: boolean } }) =>
          `${rows[p.data.value[1]]?.name}<br/>${regions[p.data.value[0]]}：` +
          (p.data.noZ ? "有讨论，历史不足未计算 z" : `z = ${p.data.value[2].toFixed(2)}`),
      },
      grid: { left: 220, right: 30, top: 20, bottom: 60 },
      xAxis: { type: "category", data: regions, splitArea: { show: true } },
      yAxis: {
        type: "category",
        data: rows.map((r) => (r.name.length > 24 ? `${r.name.slice(0, 23)}…` : r.name)),
        splitArea: { show: true },
      },
      visualMap: {
        min: -1,
        max: 5,
        calculable: true,
        orient: "horizontal",
        left: "center",
        bottom: 0,
        inRange: { color: ["#f3f4f6", "#fde68a", "#f97316", "#b91c1c"] },
      },
      series: [
        {
          type: "heatmap",
          data: cells,
          label: {
            show: true,
            formatter: (p: { data: { value: [number, number, number]; noZ: boolean } }) =>
              p.data.noZ ? "○" : p.data.value[2].toFixed(1),
          },
        },
      ],
    };
  }, [data, regions]);

  return (
    <>
      <h1>跨区信息差矩阵</h1>
      <p className="muted">
        行是近期在多个区域活跃、或在某区域显著起飞的业务类话题；格子是该区域的最大 z 值。深色 = 已起飞，“○” = 有讨论但历史不足以计算 z，空白 = 该区域尚无讨论（潜在信息差）。
      </p>
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
      {data && data.length > 0 && regions.length > 0 && (
        <>
          <div className="card">
            <Chart option={option} height={Math.max(260, data.length * 28 + 90)} />
          </div>
          <table>
            <thead>
              <tr>
                <th>话题</th>
                <th>类别</th>
                <th className="num">活跃区域数</th>
                <th className="num">最大 z</th>
              </tr>
            </thead>
            <tbody>
              {data.map((r) => (
                <tr key={r.id}>
                  <td>
                    <Link href={`/topics/${r.id}`}>{r.name}</Link>
                  </td>
                  <td>{CATEGORY_LABELS[r.category ?? ""] ?? "未分类"}</td>
                  <td className="num">{r.n_regions}</td>
                  <td className="num">{r.best_z?.toFixed(2) ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </>
  );
}
