interface Props {
  title?: string;
  message: string;
  onRetry?: () => void;
}

export default function ErrorBox({
  title = "エラー",
  message,
  onRetry,
}: Props) {
  return (
    <div className="bg-rose-50 dark:bg-rose-950 border border-rose-200 dark:border-rose-800 rounded-lg p-4">
      <div className="font-semibold text-rose-700 dark:text-rose-300">
        {title}
      </div>
      <div className="text-sm text-rose-600 dark:text-rose-400 mt-1 break-words">
        {message}
      </div>
      {onRetry && (
        <button
          onClick={onRetry}
          className="mt-3 text-xs px-3 py-1.5 border border-rose-300 dark:border-rose-700 rounded hover:bg-rose-100 dark:hover:bg-rose-900"
        >
          再試行
        </button>
      )}
    </div>
  );
}
