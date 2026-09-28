"use client";

import Link from "next/link";
import { useState } from "react";
import Status from "@/components/Status";
import { IDEA_STATUS_LABELS, api, fmtTime, send, useApi } from "@/lib/api";

type Idea = {
  id: number;
  title: string;
  status: string;
  region: string;
  owner: string | null;
  topic_id: number | null;
  opportunity_id: number | null;
  updated_at: string;
};
type IdeaDetail = Idea & {
  notes: string | null;
  events: { ts: string; from_status: string | null; to_status: string; note: string | null; user_name: string }[];
  comments: { ts: string; user_name: string; body: string }[];
};

const STATUSES = Object.keys(IDEA_STATUS_LABELS);

function IdeaCard({ idea, onChange }: { idea: Idea; onChange: () => void }) {
  const [detail, setDetail] = useState<IdeaDetail | null>(null);
  const [comment, setComment] = useState("");

  const open = async () => setDetail(detail ? null : await api<IdeaDetail>(`/ideas/${idea.id}`));
  const move = async (status: string) => {
    await send(`/ideas/${idea.id}`, "PATCH", { status });
    onChange();
  };
  const assign = async () => {
    const owner = window.prompt("负责人", idea.owner ?? "");
    if (owner === null) return;
    await send(`/ideas/${idea.id}`, "PATCH", { owner });
    onChange();
  };
  const addComment = async () => {
    if (!comment.trim()) return;
    await send(`/ideas/${idea.id}/comments`, "POST", { body: comment });
    setComment("");
    setDetail(await api<IdeaDetail>(`/ideas/${idea.id}`));
  };

  return (
    <div className="card">
      <div>
        <span className="tag">{idea.region}</span>
        <a onClick={open} style={{ cursor: "pointer" }}>
          {idea.title}
        </a>
      </div>
      <div className="muted">
        #{idea.id} · 负责人 {idea.owner ?? "—"} · {fmtTime(idea.updated_at)}
        {idea.topic_id && (
          <>
            {" "}
            · <Link href={`/topics/${idea.topic_id}`}>话题</Link>
          </>
        )}
      </div>
      <div className="toolbar" style={{ marginTop: 6, marginBottom: 0 }}>
        <select value={idea.status} onChange={(e) => move(e.target.value)}>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {IDEA_STATUS_LABELS[s]}
            </option>
          ))}
        </select>
        <button onClick={assign}>分派</button>
      </div>
      {detail && (
        <div style={{ marginTop: 8 }}>
          <ul className="plain">
            {detail.events.map((e, i) => (
              <li key={i} className="muted">
                {fmtTime(e.ts)} {e.from_status ? IDEA_STATUS_LABELS[e.from_status] : "∅"} → {IDEA_STATUS_LABELS[e.to_status]}{" "}
                {e.note}
              </li>
            ))}
            {detail.comments.map((c, i) => (
              <li key={`c${i}`}>
                <b>{c.user_name}</b>：{c.body} <span className="muted">{fmtTime(c.ts)}</span>
              </li>
            ))}
          </ul>
          <div className="toolbar">
            <input value={comment} onChange={(e) => setComment(e.target.value)} placeholder="写评论…" style={{ flex: 1 }} />
            <button onClick={addComment}>评论</button>
          </div>
        </div>
      )}
    </div>
  );
}

export default function IdeasPage() {
  const { data, error, loading, reload } = useApi<Idea[]>("/ideas");
  return (
    <>
      <h1>点子库</h1>
      <p className="muted">候选 → 观察 → 已验证 → 归档。商机页点“有价值”或“加入点子库”会自动进入候选。</p>
      <Status loading={loading} error={error} />
      <div className="kanban">
        {STATUSES.map((s) => (
          <div key={s} className="col">
            <h2>
              {IDEA_STATUS_LABELS[s]}（{(data ?? []).filter((i) => i.status === s).length}）
            </h2>
            {(data ?? [])
              .filter((i) => i.status === s)
              .map((i) => (
                <IdeaCard key={i.id} idea={i} onChange={reload} />
              ))}
          </div>
        ))}
      </div>
    </>
  );
}
