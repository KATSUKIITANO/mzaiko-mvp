interface Props {
  label?: string;
}

export default function LoadingSpinner({ label = "読み込み中..." }: Props) {
  return (
    <div className="flex items-center justify-center py-12 text-slate-500 dark:text-slate-400">
      <svg
        className="animate-spin h-5 w-5 mr-3"
        viewBox="0 0 24 24"
        fill="none"
      >
        <circle
          cx="12"
          cy="12"
          r="10"
          stroke="currentColor"
          strokeWidth="4"
          className="opacity-25"
        />
        <path
          className="opacity-75"
          fill="currentColor"
          d="M4 12a8 8 0 018-8V0C5.4 0 0 5.4 0 12h4z"
        />
      </svg>
      {label}
    </div>
  );
}
