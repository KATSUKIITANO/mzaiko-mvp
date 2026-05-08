import { useState } from "react";
import ErrorBox from "@/components/ErrorBox";
import LoadingSpinner from "@/components/LoadingSpinner";
import { useRanking } from "@/hooks/useRanking";

const CATEGORIES: string[] = [
  "videogames",
  "camera",
  "electronics",
  "hobby",
  "toys",
  "kitchen",
  "books",
  "computers",
  "beauty",
  "office",
];

export default function RankingPage() {
  const [category, setCategory] = useState<string>("videogames");
  const [limit, setLimit] = useState<number>(20);
  const [submitted, setSubmitted] = useState<{
    category: string;
    limit: number;
  }>({ category, limit });

  const { data, isLoading, error, refetch, isFetching } = useRanking(
    submitted.category,
    submitted.limit,
    true
  );

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitted({ category, limit });
  };

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">Amazon Ranking</h1>

      <form
        onSubmit={handleSubmit}
        className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg p-4 flex items-center gap-3 flex-wrap"
      >
        <label className="text-sm flex items-center gap-2">
          Category
          <select
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className="text-sm border border-slate-300 dark:border-slate-600 rounded px-2 py-1.5 dark:bg-slate-900"
          >
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm flex items-center gap-2">
          Limit
          <input
            type="number"
            min={1}
            max={100}
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            className="w-20 border border-slate-300 dark:border-slate-600 rounded px-2 py-1.5 dark:bg-slate-900"
          />
        </label>
        <button
          type="submit"
          className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded hover:bg-blue-700"
        >
          取得
        </button>
        {isFetching && (
          <span className="text-xs text-slate-500">取得中...</span>
        )}
      </form>

      {isLoading && <LoadingSpinner label="ランキング取得中（初回は十数秒）..." />}
      {error && (
        <ErrorBox
          message={(error as Error).message}
          onRetry={() => refetch()}
        />
      )}

      {data && (
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg">
          <div className="p-3 border-b border-slate-200 dark:border-slate-700 text-sm text-slate-600 dark:text-slate-400">
            {data.category} — {data.count} 件
          </div>
          <ul className="divide-y divide-slate-200 dark:divide-slate-700">
            {data.products.map((p) => (
              <li
                key={p.asin}
                className="p-3 flex items-center gap-3 text-sm"
              >
                <span className="w-10 text-right font-bold text-slate-500 tabular-nums">
                  #{p.rank}
                </span>
                <a
                  href={p.amazon_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex-1 truncate text-blue-600 dark:text-blue-400 hover:underline"
                >
                  {p.title || p.asin}
                </a>
                <span className="text-xs text-slate-500 dark:text-slate-400 font-mono whitespace-nowrap">
                  {p.asin}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
