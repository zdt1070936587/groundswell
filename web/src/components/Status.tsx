export default function Status({ loading, error, empty }: { loading?: boolean; error?: string | null; empty?: boolean }) {
  if (error) return <p className="error">加载失败：{error}</p>;
  if (loading) return <p className="muted">加载中…</p>;
  if (empty) return <p className="muted">暂无数据</p>;
  return null;
}
