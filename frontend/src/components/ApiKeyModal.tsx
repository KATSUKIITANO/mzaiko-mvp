import { useState } from "react";

interface Props {
  initial: string;
  onClose: () => void;
  onSave: (key: string) => void;
}

export default function ApiKeyModal({ initial, onClose, onSave }: Props) {
  const [value, setValue] = useState<string>(initial);

  return (
    <div
      className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center px-4"
      onClick={onClose}
    >
      <div
        className="bg-white dark:bg-slate-800 rounded-lg shadow-xl max-w-md w-full p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold mb-2">API Key 設定</h2>
        <p className="text-sm text-slate-500 dark:text-slate-400 mb-4">
          X-API-Key ヘッダに送る秘密鍵を入力してください。値は localStorage
          に保存され、Axios interceptor が自動付与します。
        </p>
        <input
          type="password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="your-secret-key"
          className="w-full border border-slate-300 dark:border-slate-600 dark:bg-slate-900 rounded px-3 py-2 mb-4 text-sm"
        />
        <div className="flex justify-end gap-2">
          <button
            onClick={() => onSave("")}
            className="px-3 py-2 text-sm rounded text-rose-600 hover:bg-rose-50 dark:hover:bg-slate-700"
          >
            クリア
          </button>
          <button
            onClick={onClose}
            className="px-3 py-2 text-sm rounded hover:bg-slate-100 dark:hover:bg-slate-700"
          >
            キャンセル
          </button>
          <button
            onClick={() => onSave(value.trim())}
            className="px-3 py-2 text-sm rounded bg-blue-600 text-white hover:bg-blue-700"
          >
            保存
          </button>
        </div>
      </div>
    </div>
  );
}
