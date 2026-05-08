interface Props {
  title: string;
  value: string | number;
  subtitle?: string;
  tone?: "default" | "success" | "warning" | "danger" | "info";
}

const TONES: Record<string, string> = {
  default: "border-slate-200 dark:border-slate-700",
  success: "border-emerald-300 dark:border-emerald-700",
  warning: "border-amber-300 dark:border-amber-700",
  danger: "border-rose-300 dark:border-rose-700",
  info: "border-blue-300 dark:border-blue-700",
};

export default function StatCard({
  title,
  value,
  subtitle,
  tone = "default",
}: Props) {
  return (
    <div
      className={`bg-white dark:bg-slate-800 border ${TONES[tone]} rounded-lg p-4 shadow-sm`}
    >
      <div className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
        {title}
      </div>
      <div className="mt-1 text-2xl font-bold text-slate-900 dark:text-slate-100">
        {value}
      </div>
      {subtitle && (
        <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">
          {subtitle}
        </div>
      )}
    </div>
  );
}
