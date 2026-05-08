import Badge from "@/components/Badge";
import ErrorBox from "@/components/ErrorBox";
import LoadingSpinner from "@/components/LoadingSpinner";
import { useProducts } from "@/hooks/useProducts";
import type { MonitoringProduct } from "@/types";

export default function MonitoringPage() {
  const { data, isLoading, error, refetch } = useProducts();

  if (isLoading) return <LoadingSpinner />;
  if (error) {
    return (
      <ErrorBox
        message={(error as Error).message}
        onRetry={() => refetch()}
      />
    );
  }
  if (!data) return null;

  const danger = data.filter((p) => p.monitoring_status === "danger");
  const warning = data.filter((p) => p.monitoring_status === "warning");
  const ok = data.filter((p) => p.monitoring_status === "ok");

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Monitoring</h1>
        <button
          onClick={() => refetch()}
          className="text-xs px-3 py-1.5 border border-slate-300 dark:border-slate-600 rounded hover:bg-slate-100 dark:hover:bg-slate-700"
        >
          更新
        </button>
      </div>

      <Section
        title="Danger（赤字 / 緊急）"
        items={danger}
        tone="danger"
        empty="現在 danger は 0 件"
      />
      <Section
        title="Warning（利益率低下）"
        items={warning}
        tone="warning"
        empty="現在 warning は 0 件"
      />
      <Section
        title="OK"
        items={ok}
        tone="ok"
        empty="現在 ok は 0 件"
      />
    </div>
  );
}

function Section({
  title,
  items,
  tone,
  empty,
}: {
  title: string;
  items: MonitoringProduct[];
  tone: "ok" | "warning" | "danger";
  empty: string;
}) {
  return (
    <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg">
      <div className="p-4 border-b border-slate-200 dark:border-slate-700 flex items-center justify-between">
        <h2 className="font-semibold">{title}</h2>
        <Badge tone={tone}>{items.length}</Badge>
      </div>
      <ul className="divide-y divide-slate-200 dark:divide-slate-700">
        {items.length === 0 && (
          <li className="p-4 text-sm text-slate-500">{empty}</li>
        )}
        {items.map((p) => (
          <li
            key={p.id}
            className="p-3 flex items-center justify-between text-sm gap-3"
          >
            <span className="truncate flex-1">{p.title}</span>
            <span className="text-slate-500 whitespace-nowrap text-xs">
              {p.current_profit_rate !== null
                ? `${p.current_profit_rate.toFixed(1)}%`
                : "-"}
              {" / "}
              score {p.overall_score ?? "-"}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
