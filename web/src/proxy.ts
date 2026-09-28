import { NextResponse, type NextRequest } from "next/server";

// 可选的 Basic Auth：设置 GS_WEB_USER / GS_WEB_PASSWORD 后整站需要登录（单用户内部工具）。
export function proxy(req: NextRequest) {
  const user = process.env.GS_WEB_USER;
  const password = process.env.GS_WEB_PASSWORD;
  if (!user || !password) return NextResponse.next();
  const header = req.headers.get("authorization") ?? "";
  if (header.startsWith("Basic ")) {
    const decoded = atob(header.slice(6));
    const sep = decoded.indexOf(":");
    if (sep > 0 && decoded.slice(0, sep) === user && decoded.slice(sep + 1) === password) {
      return NextResponse.next();
    }
  }
  return new NextResponse("需要登录", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="groundswell"' },
  });
}

export const config = { matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"] };
