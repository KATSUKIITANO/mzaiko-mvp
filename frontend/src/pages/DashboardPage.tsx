import ErrorBox from "@/components/ErrorBox";
import LoadingSpinner from "@/components/LoadingSpinner";
import StatCard from "@/components/StatCard";
import MonitoringPieChart from "@/components/charts/MonitoringPieChart";
import ProfitBarChart from "@/components/charts/ProfitBarChart";
import ScoreDistributionChart from "@/components/charts/ScoreDistributionChart";
import { useDashboard } from "@/hooks/useDashboard";
import { useProducts } from "@/hooks/useProducts";
import type { TopProduct } from "@/types";

export default function DashboardPage() {
  const dash = useDashboard();
  const products = useProducts();

  if (dash.isLoading) return <LoadingSpinner />;
  if (dash.error) {
    return (
      <ErrorBox
        message={(dash.error as Error).message}
        onRetry={() => dash.refetch()}
      />
    );
  }
  if (!dash.data) return null;

  const d = dash.data;
  const okCount = Math.max(
    0,
    d.allowed_products - d.warning_products - d.danger_products
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Dashboard</h1>
        <button
          onClick={() => dash.refetch()}
          className="text-xs px-3 py-1.5 border border-slate-300 dark:border-slate-600 rounded hover:bg-slate-100 dark:hover:bg-slate-700"
        >
          更新
        </button>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-3">
        <StatCard title="Total" value={d.total_products} />
        <StatCard
          title="Profitable"
          value={d.profitable_products}
          tone="success"
        />
        <StatCard title="Allowed" value={d.allowed_products} tone="info" />
        <StatCard title="Blocked" value={d.blocked_products} tone="danger" />
        <StatCard title="Warning" value={d.warning_products} tone="warning" />
        <StatCard title="Danger" value={d.danger_products} tone="danger" />
        <StatCard
          title="Avg Rate"
          value={`${d.average_profit_rate.toFixed(1)}%`}
          subtitle={`Avg score ${d.average_overall_score.toFixed(1)}`}
        />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <ChartCard title="Monitoring Status">
          <MonitoringPieChart
            okCount={okCount}
            warningCount={d.warning_products}
            dangerCount={d.danger_products}
          />
        </ChartCard>
        <ChartCard title="Profit Rate Distribution">
          {products.data ? (
            <ProfitBarChart products={products.data} />
          ) : (
            <LoadingSpinner label="" />
          )}
        </ChartCard>
        <ChartCard title="Overall Score Distribution">
          {products.data ? (
            <ScoreDistributionChart products={products.data} />
          ) : (
            <LoadingSpinner label="" />
          )}
        </ChartCard>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <TopList title="Top Profitable" products={d.top_profitable_products} />
        <TopList title="Top Scored" products={d.top_scored_products} />
      </div>
    </div>
  );
}

function ChartCard({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-white dark:bg-slate-800 rounded-lg p-4 border border-slate-200 dark:border-slate-700">
      <h2 className="text-sm font-semibold mb-2 text-slate-700 dark:text-slate-300">
        {title}
      </h2>
      {children}
    </div>
  );
}

function TopList({
  title,
  products,
}: {
  title: string;
  products: TopProduct[];
}) {
  return (
    <div className="bg-white dark:bg-slate-800 rounded-lg border border-slate-200 dark:border-slate-700">
      <h2 className="text-sm font-semibold p-3 border-b border-slate-200 dark:border-slate-700">
        {title}
      </h2>
      <ul className="divide-y divide-slate-200 dark:divide-slate-700">
        {products.length === 0 && (
          <li className="p-4 text-sm text-slate-500">該当なし</li>
        )}
        {products.map((p) => (
          <li
            key={p.id}
            className="p-3 flex justify-between items-center text-sm gap-3"
          >
            <span className="truncate flex-1">{p.title}</span>
            <span className="text-slate-600 dark:text-slate-400 whitespace-nowrap text-xs">
              score {p.overall_score ?? "-"} /{" "}
              {p.profit_rate !== null && p.profit_rate !== undefined
                ? `${p.profit_rate.toFixed(1)}%`
                : "-"}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
