import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "观潮 Groundswell",
  description: "多区域舆情 → 话题 → 商机",
};

const NAV = [
  ["/", "排行榜"],
  ["/opportunities", "商机"],
  ["/matrix", "信息差矩阵"],
  ["/spread", "扩散时间轴"],
  ["/ideas", "点子库"],
  ["/subscriptions", "订阅"],
  ["/reports", "报告"],
  ["/health", "源健康"],
] as const;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <header className="top">
          <Link href="/" className="brand">
            观潮 <span>Groundswell</span>
          </Link>
          <nav>
            {NAV.map(([href, label]) => (
              <Link key={href} href={href}>
                {label}
              </Link>
            ))}
          </nav>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
