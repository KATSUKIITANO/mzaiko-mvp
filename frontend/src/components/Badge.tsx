import type { ReactNode } from "react";

export type BadgeTone =
  | "ok"
  | "warning"
  | "danger"
  | "neutral"
  | "info"
  | "success";

interface Props {
  children: ReactNode;
  tone?: BadgeTone;
}

const STYLES: Record<BadgeTone, string> = {
  ok: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
  success:
    "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
  warning:
    "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200",
  danger: "bg-rose-100 text-rose-800 dark:bg-rose-900 dark:text-rose-200",
  neutral:
    "bg-slate-100 text-slate-700 dark:bg-slate-700 dark:text-slate-300",
  info: "bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-200",
};

export default function Badge({ children, tone = "neutral" }: Props) {
  return (
    <span
      className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${STYLES[tone]}`}
    >
      {children}
    </span>
  );
}
