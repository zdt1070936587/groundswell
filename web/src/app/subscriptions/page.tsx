"use client";

import { useState } from "react";
import Status from "@/components/Status";
import { send, useApi } from "@/lib/api";

type Sub = {
  id: number;
  name: string;
  keywords: string[];
  regions: string[];
  categories: string[];
  min_score: number;
  channel: string;
};

const split = (s: string) =>
  s
    .split(/[,，\s]+/)
    .map((x) => x.trim())
    .filter(Boolean);

export default function SubscriptionsPage() {
  const { data, error, loading, reload } = useApi<Sub[]>("/subscriptions");
  const [form, setForm] = useState({ name: "", keywords: "", regions: "", min_score: "2" });
  const [msg, setMsg] = useState("");

  const add = async () => {
    if (!form.name || !form.keywords) return setMsg("名称和关键词必填");
    await send("/subscriptions", "POST", {
      name: form.name,
      keywords: split(form.keywords),
      regions: split(form.regions),
      min_score: Number(form.min_score) || 2,
    });
    setForm({ name: "", keywords: "", regions: "", min_score: "2" });
    setMsg("已添加");
    reload();
  };
  const remove = async (id: number) => {
    await send(`/subscriptions/${id}`, "DELETE");
    reload();
  };

  return (
    <>
      <h1>关键词 / 区域订阅</h1>
      <p className="muted">话题命中关键词且得分达到阈值时，每小时检查一次并推送钉钉。</p>
      <div className="card toolbar">
        <input placeholder="名称" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <input placeholder="关键词（逗号分隔）" value={form.keywords} onChange={(e) => setForm({ ...form, keywords: e.target.value })} />
        <input placeholder="区域（可空，如 GC,NA）" value={form.regions} onChange={(e) => setForm({ ...form, regions: e.target.value })} />
        <input style={{ width: 80 }} value={form.min_score} onChange={(e) => setForm({ ...form, min_score: e.target.value })} />
        <button className="primary" onClick={add}>
          添加
        </button>
        {msg && <span className="muted">{msg}</span>}
      </div>
      <Status loading={loading} error={error} empty={!!data && data.length === 0} />
      {data && data.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>名称</th>
              <th>关键词</th>
              <th>区域</th>
              <th className="num">最低分</th>
              <th>渠道</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data.map((s) => (
              <tr key={s.id}>
                <td>{s.name}</td>
                <td>{s.keywords.join(", ")}</td>
                <td>{s.regions.join(", ") || "全部"}</td>
                <td className="num">{s.min_score}</td>
                <td>{s.channel === "default" ? "默认机器人" : s.channel.slice(0, 30)}</td>
                <td>
                  <button className="bad" onClick={() => remove(s.id)}>
                    删除
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
