import { useMemo, useState } from "react";
import Badge, { type BadgeTone } from "@/components/Badge";
import ErrorBox from "@/components/ErrorBox";
import LoadingSpinner from "@/components/LoadingSpinner";
import { useProducts } from "@/hooks/useProducts";
import { downloadProductsCsv } from "@/services/products";
import type { MonitoringStatus } from "@/types";

export default function ProductsPage() {
  const { data, isLoading, error, refetch } = useProducts();

  const [profitableOnly, setProfitableOnly] = useState(false);
  const [allowedOnly, setAllowedOnly] = useState(false);
  const [riskFilter, setRiskFilter] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [minScore, setMinScore] = useState<number>(0);
  const [downloadError, setDownloadError] = useState<string>("");

  const filtered = useMemo(() => {
    if (!data) return [];
    return data.filter((p) => {
      if (profitableOnly && !p.is_profitable) return false;
      if (allowedOnly && !p.is_allowed) return false;
      if (riskFilter && p.risk_level !== riskFilter) return false;
      if (statusFilter && p.monitoring_status !== statusFilter) return false;
      if (minScore > 0 && (p.overall_score ?? 0) < minScore) return false;
      return true;
    });
  }, [data, profitableOnly, allowedOnly, riskFilter, statusFilter, minScore]);

  if (isLoading) return <LoadingSpinner />;
  if (error) {
    return (
      <ErrorBox
        message={(error as Error).message}
        onRetry={() => refetch()}
      />
    );
  }

  const handleDownload = async () => {
    setDownloadError("");
    try {
      await downloadProductsCsv({
        profitable_only: profitableOnly ? "true" : undefined,
        allowed_only: allowedOnly ? "true" : undefined,
        monitoring_status: statusFilter || undefined,
        min_overall_score: minScore > 0 ? String(minScore) : undefined,
      });
    } catch (e) {
      setDownloadError((e as Error).message);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h1 className="text-2xl font-bold">Products</h1>
        <button
          onClick={handleDownload}
          className="px-3 py-2 text-sm rounded bg-blue-600 text-white hover:bg-blue-700"
        >
          CSV ダウンロード
        </button>
      </div>

      {downloadError && (
        <ErrorBox title="CSV ダウンロード失敗" message={downloadError} />
      )}

      <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg p-4 grid grid-cols-2 md:grid-cols-5 gap-3 items-center">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={profitableOnly}
            onChange={(e) => setProfitableOnly(e.target.checked)}
          />
          Profitable only
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={allowedOnly}
            onChange={(e) => setAllowedOnly(e.target.checked)}
          />
          Allowed only
        </label>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="text-sm border border-slate-300 dark:border-slate-600 rounded px-2 py-1.5 dark:bg-slate-900"
        >
          <option value="">全 status</option>
          <option value="ok">ok</option>
          <option value="warning">warning</option>
          <option value="danger">danger</option>
        </select>
        <select
          value={riskFilter}
          onChange={(e) => setRiskFilter(e.target.value)}
          className="text-sm border border-slate-300 dark:border-slate-600 rounded px-2 py-1.5 dark:bg-slate-900"
        >
          <option value="">全 risk</option>
          <option value="low">low</option>
          <option value="high">high</option>
        </select>
        <label className="flex items-center gap-2 text-sm">
          Min Score
          <input
            type="number"
            min={0}
            max={100}
            value={minScore}
            onChange={(e) => setMinScore(Number(e.target.value))}
            className="w-20 border border-slate-300 dark:border-slate-600 rounded px-2 py-1.5 dark:bg-slate-900"
          />
        </label>
      </div>

      <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead className="bg-slate-50 dark:bg-slate-700">
            <tr>
              <Th>Title</Th>
              <Th align="right">Amazon</Th>
              <Th align="right">eBay</Th>
              <Th align="right">Profit</Th>
              <Th align="right">Rate</Th>
              <Th align="right">Score</Th>
              <Th>Risk</Th>
              <Th>Status</Th>
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 && (
              <tr>
                <td
                  colSpan={8}
                  className="text-center py-8 text-slate-500"
                >
                  該当する商品がありません
                </td>
              </tr>
            )}
            {filtered.map((p) => (
              <tr
                key={p.id}
                className="border-t border-slate-200 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-900/50"
              >
                <Td>
                  <span className="line-clamp-2">{p.title}</span>
                </Td>
                <Td align="right">{p.price.toLocaleString()}</Td>
                <Td align="right">
                  {p.selling_price !== null
                    ? p.selling_price.toLocaleString()
                    : "-"}
                </Td>
                <Td align="right">
                  {p.profit !== null ? p.profit.toLocaleString() : "-"}
                </Td>
                <Td align="right">
                  {p.current_profit_rate !== null
                    ? `${p.current_profit_rate.toFixed(1)}%`
                    : "-"}
                </Td>
                <Td align="right">{p.overall_score ?? "-"}</Td>
                <Td>
                  <Badge tone={p.risk_level === "high" ? "danger" : "ok"}>
                    {p.risk_level}
                  </Badge>
                </Td>
                <Td>
                  <Badge tone={statusTone(p.monitoring_status)}>
                    {p.monitoring_status}
                  </Badge>
                </Td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="text-xs text-slate-500">
        {filtered.length} / {data?.length ?? 0} 件表示
      </div>
    </div>
  );
}

function Th({
  children,
  align,
}: {
  children: React.ReactNode;
  align?: "right";
}) {
  return (
    <th
      className={`px-3 py-2 font-semibold whitespace-nowrap ${
        align === "right" ? "text-right" : "text-left"
      }`}
    >
      {children}
    </th>
  );
}

function Td({
  children,
  align,
}: {
  children: React.ReactNode;
  align?: "right";
}) {
  return (
    <td
      className={`px-3 py-2 ${align === "right" ? "text-right tabular-nums" : ""}`}
    >
      {children}
    </td>
  );
}

function statusTone(status: MonitoringStatus): BadgeTone {
  if (status === "warning") return "warning";
  if (status === "danger") return "danger";
  return "ok";
}
