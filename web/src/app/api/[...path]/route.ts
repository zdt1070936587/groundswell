import type { NextRequest } from "next/server";

// 浏览器只访问同源 /api/*，由这里转发到 FastAPI 并附加 token，token 不下发到浏览器。
const BASE = process.env.GS_API_BASE ?? "http://127.0.0.1:8000";
const TOKEN = process.env.GS_API_TOKEN ?? "";

export const dynamic = "force-dynamic";

type Ctx = { params: Promise<{ path: string[] }> };

async function proxy(req: NextRequest, ctx: Ctx): Promise<Response> {
  const { path } = await ctx.params;
  const url = new URL(`/api/${path.map(encodeURIComponent).join("/")}`, BASE);
  url.search = req.nextUrl.search;
  const headers: Record<string, string> = { accept: req.headers.get("accept") ?? "*/*" };
  const contentType = req.headers.get("content-type");
  if (contentType) headers["content-type"] = contentType;
  if (TOKEN) headers.authorization = `Bearer ${TOKEN}`;
  const body = req.method === "GET" || req.method === "HEAD" ? undefined : await req.text();
  try {
    const res = await fetch(url, { method: req.method, headers, body, cache: "no-store" });
    return new Response(await res.arrayBuffer(), {
      status: res.status,
      headers: { "content-type": res.headers.get("content-type") ?? "application/json" },
    });
  } catch (err) {
    return Response.json({ detail: `API 不可达：${String(err)}` }, { status: 502 });
  }
}

export { proxy as DELETE, proxy as GET, proxy as PATCH, proxy as POST };
