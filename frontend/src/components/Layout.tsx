import { useState, type ReactNode } from "react";
import { Link, NavLink } from "react-router-dom";
import { useApiKey } from "@/hooks/useApiKey";
import ApiKeyModal from "./ApiKeyModal";

interface Props {
  children: ReactNode;
}

const NAV_ITEMS = [
  { to: "/dashboard", label: "Dashboard" },
  { to: "/products", label: "Products" },
  { to: "/monitoring", label: "Monitoring" },
  { to: "/ranking", label: "Ranking" },
];

const LINK_BASE =
  "px-3 py-2 rounded-md text-sm font-medium transition-colors whitespace-nowrap";
const ACTIVE = "bg-slate-200 dark:bg-slate-700 text-slate-900 dark:text-slate-100";
const INACTIVE =
  "text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800";

export default function Layout({ children }: Props) {
  const [showKeyModal, setShowKeyModal] = useState(false);
  const { apiKey, setApiKey, hasKey } = useApiKey();

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-slate-900">
      <header className="bg-white dark:bg-slate-800 border-b border-slate-200 dark:border-slate-700 sticky top-0 z-10">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 flex items-center justify-between h-14">
          <Link
            to="/dashboard"
            className="text-base sm:text-lg font-bold text-slate-900 dark:text-slate-100"
          >
            無在庫販売 MVP
          </Link>
          <nav className="hidden md:flex items-center gap-1">
            {NAV_ITEMS.map((it) => (
              <NavLink
                key={it.to}
                to={it.to}
                className={({ isActive }) =>
                  `${LINK_BASE} ${isActive ? ACTIVE : INACTIVE}`
                }
              >
                {it.label}
              </NavLink>
            ))}
          </nav>
          <button
            onClick={() => setShowKeyModal(true)}
            className={`text-xs px-3 py-1.5 border rounded-md whitespace-nowrap ${
              hasKey
                ? "border-emerald-300 text-emerald-700 dark:border-emerald-700 dark:text-emerald-300"
                : "border-amber-300 text-amber-700 dark:border-amber-700 dark:text-amber-300"
            } hover:bg-slate-100 dark:hover:bg-slate-700`}
          >
            {hasKey ? "API Key 設定済" : "API Key 未設定"}
          </button>
        </div>
        <nav className="md:hidden flex overflow-x-auto px-4 pb-2 gap-1 border-t border-slate-200 dark:border-slate-700">
          {NAV_ITEMS.map((it) => (
            <NavLink
              key={it.to}
              to={it.to}
              className={({ isActive }) =>
                `${LINK_BASE} flex-shrink-0 ${isActive ? ACTIVE : INACTIVE}`
              }
            >
              {it.label}
            </NavLink>
          ))}
        </nav>
      </header>
      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
        {children}
      </main>
      {showKeyModal && (
        <ApiKeyModal
          initial={apiKey}
          onClose={() => setShowKeyModal(false)}
          onSave={(k) => {
            setApiKey(k);
            setShowKeyModal(false);
          }}
        />
      )}
    </div>
  );
}
