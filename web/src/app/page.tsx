"use client";

import Link from "next/link";
import { useState } from "react";
import Status from "@/components/Status";
import { CATEGORY_LABELS, fmt, useApi, type Meta } from "@/lib/api";

type Topic = {
  id: number;
  name: string;
  category: string | null;
  mentions: number;
  max_z: number | null;
  max_score: number | null;
  sources: number;
  origin_region: string | null;
  active_regions: string[];
  keywords: string[];
};
type ItemRow = { id: number; title: string; url: string | null; source_id: string; pct: number };
type KeywordRow = { key: string; mentions: number; max_z: number | null };
type Rankings = { topics: Topic[]; items?: ItemRow[]; keywords?: KeywordRow[] };

export default function RankingsPage() {
  const meta = useApi<Meta>("/meta");
  const [region, setRegion] = useState("GLOBAL");
  const [sort, setSort] = useState("heat");
  const [hours, setHours] = useState(24);
  const [businessOnly, setBusinessOnly] = useState(false);
  const q = `/rankings?region=${region}&sort=${sort}&hours=${hours}&business_only=${businessOnly}&limit=50`;
  const { data, error, loading } = useApi<Rankings>(q);

  return (
    <>
      <h1>热度 / 增长排行榜</h1>
      <div className="toolbar">
        <label>区域</label>
        <select value={region} onChange={(e) => setRegion(e.target.value)}>
          {(meta.data?.regions ?? [{ code: "GLOBAL", name: "全球" }]).map((r) => (
            <option key={r.code} value={r.code}>
              {r.code} · {r.name}
            </option>
          ))}
        </select>
        <label>排序</label>
        <select value={sort} onChange={(e) => setSort(e.target.value)}>
          <option value="heat">热度</option>
          <option value="growth">增长（z ≥ 阈值）</option>
          <option value="mentions">提及数</option>
        </select>
        <label>窗口</label>
        <select value={hours} onChange={(e) => setHours(Number(e.target.value))}>
          {[6, 24, 72, 168].map((h) => (
            <option key={h} value={h}>
              {h} 小时
            </option>
          ))}
        </select>
        <label>
          <input type="checkbox" checked={businessOnly} onChange={(e) => setBusinessOnly(e.target.checked)} /> 只看业务类
        </label>
      </div>
      <Status loading={loading} error={error} />
      {data && data.topics.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>话题</th>
              <th>类别</th>
              <th className="num">提及</th>
              <th className="num">最大 z</th>
              <th className="num">得分</th>
              <th className="num">源数</th>
              <th>起源 / 活跃区域</th>
            </tr>
          </thead>
          <tbody>
            {data.topics.map((t, i) => (
              <tr key={t.id}>
                <td>{i + 1}</td>
                <td>
                  <Link href={`/topics/${t.id}`}>{t.name}</Link>
                  <div className="muted">{(t.keywords ?? []).slice(0, 5).join(" · ")}</div>
                </td>
                <td>{CATEGORY_LABELS[t.category ?? ""] ?? "未分类"}</td>
                <td className="num">{fmt(t.mentions, 0)}</td>
                <td className="num">{fmt(t.max_z, 2)}</td>
                <td className="num">{fmt(t.max_score, 2)}</td>
                <td className="num">{t.sources}</td>
                <td>
                  {t.origin_region ?? "—"} / {(t.active_regions ?? []).join(", ") || "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {data && data.topics.length === 0 && (
        <div className="grid2">
          <div className="card">
            <h2>热门条目（尚无话题统计时的降级视图）</h2>
            <ul className="plain">
              {(data.items ?? []).map((it) => (
                <li key={it.id}>
                  {it.url ? <a href={it.url} target="_blank" rel="noreferrer">{it.title}</a> : it.title}{" "}
                  <span className="muted">{it.source_id}</span>
                </li>
              ))}
            </ul>
          </div>
          <div className="card">
            <h2>关键词增长</h2>
            <ul className="plain">
              {(data.keywords ?? []).map((k) => (
                <li key={k.key}>
                  {k.key} <span className="muted">提及 {fmt(k.mentions, 0)} · z {fmt(k.max_z, 2)}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </>
  );
}
