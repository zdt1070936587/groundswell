"use client";

import Link from "next/link";
import { useState } from "react";
import Status from "@/components/Status";
import { CATEGORY_LABELS, api, fmt, send, useApi, type Meta } from "@/lib/api";

type Opp = {
  id: number;
  topic_id: number;
  topic_name: string;
  category: string | null;
  region: string;
  model: string;
  summary: string | null;
  scores: Record<string, number>;
  total_score: number;
  rank_score: number | null;
  tags: string[];
  why_now: string | null;
  distribution_path: string | null;
  product_shapes: unknown[];
  counter_arguments: unknown[];
  known_players: unknown[];
  durability: string;
  is_gap: boolean;
  feedback: string | null;
};
type Evidence = { kind: string; snippet: string; url: string | null; source_id: string };

const SCORE_LABELS: Record<string, string> = {
  demand: "需求",
  growth: "增长",
  willingness_to_pay: "付费意愿",
  competition_gap: "竞争空白",
  feasibility: "可行性",
  distribution: "分发",
  durability: "持续性",
  regional_fit: "区域适配",
};

function list(values: unknown[]): string {
  return values.map((v) => (typeof v === "string" ? v : JSON.stringify(v))).join("；") || "—";
}

function OppCard({ o, onChange }: { o: Opp; onChange: () => void }) {
  const [evidence, setEvidence] = useState<Evidence[] | null>(null);
  const [msg, setMsg] = useState("");

  const feedback = async (label: string) => {
    const r = await send<{ idea_id: number | null }>(`/opportunities/${o.id}/feedback`, "POST", { label });
    setMsg(r.idea_id ? `已记录，并加入点子库 #${r.idea_id}` : "已记录");
    onChange();
  };
  const toIdeas = async () => {
    const r = await send<{ id: number }>("/ideas", "POST", { opportunity_id: o.id });
    setMsg(`已加入点子库 #${r.id}`);
  };
  const loadEvidence = async () => {
    const d = await api<{ evidence: Evidence[] }>(`/opportunities/${o.id}`);
    setEvidence(d.evidence);
  };

  return (
    <div className="card">
      <div>
        <span className="tag">{o.region}</span>
        {o.is_gap && <span className="tag gap">区域空白</span>}
        <span className="tag">{CATEGORY_LABELS[o.category ?? ""] ?? "未分类"}</span>
        <Link href={`/topics/${o.topic_id}`}>
          <b>{o.topic_name}</b>
        </Link>{" "}
        <span className="muted">
          总分 {fmt(o.total_score)}
          {o.rank_score !== null && ` · 排序分 ${fmt(o.rank_score, 2)}`} · {o.model}
        </span>
      </div>
      <p>{o.summary}</p>
      <ul className="plain">
        <li>为什么是现在：{o.why_now || "—"}</li>
        <li>分发路径：{o.distribution_path || "—"}</li>
        <li>产品形态：{list(o.product_shapes)}</li>
        <li>反方论点：{list(o.counter_arguments)}</li>
        <li>已知玩家：{list(o.known_players)}</li>
        <li>
          打分：
          {Object.entries(o.scores ?? {}).map(([k, v]) => (
            <span key={k} className="muted">
              {SCORE_LABELS[k] ?? k} {fmt(v, 0)}　
            </span>
          ))}
        </li>
        <li>标签：{o.tags.map((t) => <span key={t} className="tag">#{t}</span>)}</li>
      </ul>
      {evidence && (
        <ul className="plain">
          {evidence.map((e, i) => (
            <li key={i}>
              <span className="tag">{e.kind}</span>
              {e.url ? <a href={e.url} target="_blank" rel="noreferrer">{e.snippet}</a> : e.snippet}{" "}
              <span className="muted">{e.source_id}</span>
            </li>
          ))}
        </ul>
      )}
      <div className="toolbar">
        {!evidence && <button onClick={loadEvidence}>查看证据</button>}
        <button className="good" onClick={() => feedback("good")}>👍 有价值</button>
        <button onClick={() => feedback("meh")}>一般</button>
        <button className="bad" onClick={() => feedback("bad")}>👎 无价值</button>
        <button onClick={toIdeas}>加入点子库</button>
        {o.feedback && <span className="muted">已反馈：{o.feedback}</span>}
        {msg && <span className="muted">{msg}</span>}
      </div>
    </div>
  );
}

export default function OpportunitiesPage() {
  const meta = useApi<Meta>("/meta");
  const [region, setRegion] = useState("");
  const [days, setDays] = useState(7);
  const { data, error, loading, reload } = useApi<Opp[]>(
    `/opportunities?days=${days}&limit=50${region ? `&region=${region}` : ""}`,
  );

  return (
    <>
      <h1>商机</h1>
      <div className="toolbar">
        <label>区域</label>
        <select value={region} onChange={(e) => setRegion(e.target.value)}>
          <option value="">全部</option>
          {(meta.data?.regions ?? []).map((r) => (
            <option key={r.code} value={r.code}>
              {r.code} · {r.name}
            </option>
          ))}
        </select>
        <label>最近</label>
        <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
          {[1, 3, 7, 30].map((d) => (
            <option key={d} value={d}>
              {d} 天
            </option>
          ))}
        </select>
      </div>
      <Status loading={loading} error={error} empty={!!data && data.length === 0} />
      {data?.map((o) => <OppCard key={o.id} o={o} onChange={reload} />)}
    </>
  );
}
