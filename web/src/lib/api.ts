"use client";

import { useCallback, useEffect, useState } from "react";

export type Json = Record<string, unknown>;

export async function api<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  const type = res.headers.get("content-type") ?? "";
  const body = type.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const detail = typeof body === "object" && body && "detail" in body ? body.detail : body;
    throw new Error(`${res.status} ${String(detail)}`);
  }
  return body as T;
}

export function send<T = unknown>(path: string, method: string, data?: unknown): Promise<T> {
  return api<T>(path, { method, body: data === undefined ? undefined : JSON.stringify(data) });
}

export function useApi<T>(path: string | null) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!path) return;
    let alive = true;
    setLoading(true);
    setError(null);
    api<T>(path)
      .then((d) => alive && setData(d))
      .catch((e: Error) => alive && setError(e.message))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [path, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}

export type Meta = {
  regions: { code: string; name: string; timezone: string }[];
  report_regions: string[];
  categories: { code: string; label: string }[];
  business_categories: string[];
  idea_statuses: string[];
};

export const CATEGORY_LABELS: Record<string, string> = {
  entertainment: "娱乐",
  society: "社会",
  demand: "需求",
  tech: "技术",
  consumer: "消费",
  policy: "政策",
};

export const IDEA_STATUS_LABELS: Record<string, string> = {
  candidate: "候选",
  watching: "观察",
  validated: "已验证",
  archived: "归档",
};

export function fmt(value: unknown, digits = 1): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(digits) : String(value);
}

export function fmtTime(value: unknown): string {
  if (!value) return "—";
  const d = new Date(String(value));
  return Number.isNaN(d.getTime())
    ? String(value)
    : d.toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
