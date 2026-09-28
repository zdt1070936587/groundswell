"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useMemo } from "react";
import Chart from "@/components/Chart";
import Status from "@/components/Status";
import { CATEGORY_LABELS, fmt, fmtTime, useApi } from "@/lib/api";

type SeriesRow = { region: string; hour: string; mentions: number; z: number | null };
type Detail = {
  id: number;
  name: string;
  summary: string | null;
  category: string | null;
  keywords: string[];
  item_count: number;
  source_count: number;
  origin_region: string | null;
  active_regions: string[];
  spread_path: { region: string; first_breakout: string | null; first_seen: string }[] | null;
  slow_variables: string[];
  first_seen: string;
  last_seen: string;
  items: { id: number; title: string; url: string | null; source_id: string; region: string | null; fetched_at: string }[];
  opportunities: { id: number; region: string; summary: string | null; total_score: number; is_gap: boolean }[];
  similar: { id: number; name: string; similarity: number; first_seen: string }[];
  series: SeriesRow[];
};

export default function TopicPage() {
  const { id } = useParams<{ id: string }>();
  const { data, error, loading } = useApi<Detail>(`/topics/${id}`);

  const option = useMemo(() => {
    const byRegion = new Map<string, [string, number][]>();
    for (const r of data?.series ?? []) {
      if (!byRegion.has(r.region)) byRegion.set(r.region, []);
      byRegion.get(r.region)!.push([r.hour, r.mentions]);
    }
    return {
      tooltip: { trigger: "axis" },
      legend: { top: 0 },
      grid: { left: 40, right: 20, top: 30, bottom: 30 },
      xAxis: { type: "time" },
      yAxis: { type: "value", name: "提及" },
      series: [...byRegion.entries()].map(([name, points]) => ({
        name,
        type: "line",
        showSymbol: points.length < 48,
        data: points,
      })),
    };
  }, [data]);

  if (!data) return <Status loading={loading} error={error} />;
  return (
    <>
      <h1>
        {data.name} <span className="tag">{CATEGORY_LABELS[data.category ?? ""] ?? "未分类"}</span>
      </h1>
      {data.summary && <p>{data.summary}</p>}
      <div className="metrics">
        <div>条目<b>{data.item_count}</b></div>
        <div>来源<b>{data.source_count}</b></div>
        <div>起源区域<b>{data.origin_region ?? "—"}</b></div>
        <div>首次出现<b>{fmtTime(data.first_seen)}</b></div>
        <div>最近出现<b>{fmtTime(data.last_seen)}</b></div>
      </div>
      <p className="muted">
        关键词：{data.keywords.join(" · ") || "—"}　活跃区域：{data.active_regions.join(", ") || "—"}
        {data.slow_variables.length > 0 && <>　慢变量：{data.slow_variables.join("、")}</>}
      </p>
      {data.spread_path && data.spread_path.length > 0 && (
        <p>
          扩散路径：
          {data.spread_path.map((p, i) => (
            <span key={p.region}>
              {i > 0 && " → "}
              <b>{p.region}</b>
              <span className="muted">（{fmtTime(p.first_breakout ?? p.first_seen)}）</span>
            </span>
          ))}
        </p>
      )}
      <div className="card">
        <h2>近 7 天各区域提及</h2>
        {data.series.length ? <Chart option={option} height={300} /> : <p className="muted">暂无统计</p>}
      </div>
      <div className="grid2">
        <div className="card">
          <h2>条目（{data.items.length}）</h2>
          <ul className="plain">
            {data.items.map((it) => (
              <li key={it.id}>
                {it.url ? <a href={it.url} target="_blank" rel="noreferrer">{it.title}</a> : it.title}{" "}
                <span className="muted">
                  {it.source_id} · {it.region ?? "?"} · {fmtTime(it.fetched_at)}
                </span>
              </li>
            ))}
          </ul>
        </div>
        <div>
          <div className="card">
            <h2>商机</h2>
            {data.opportunities.length === 0 && <p className="muted">尚未分析</p>}
            {data.opportunities.map((o) => (
              <p key={o.id}>
                <span className="tag">{o.region}</span>
                {o.is_gap && <span className="tag gap">区域空白</span>}
                <b>{fmt(o.total_score)}</b> {o.summary}
              </p>
            ))}
          </div>
          <div className="card">
            <h2>历史相似话题</h2>
            <ul className="plain">
              {data.similar.map((s) => (
                <li key={s.id}>
                  <Link href={`/topics/${s.id}`}>{s.name}</Link>{" "}
                  <span className="muted">
                    相似度 {fmt(s.similarity, 2)} · {fmtTime(s.first_seen)}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </div>
    </>
  );
}
