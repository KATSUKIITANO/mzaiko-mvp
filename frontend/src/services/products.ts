import { api, getApiBaseUrl, getStoredApiKey } from "./api";
import type { MonitoringProduct } from "@/types";

export async function fetchMonitoringProducts(): Promise<MonitoringProduct[]> {
  const res = await api.get<MonitoringProduct[]>("/monitoring/products");
  return res.data;
}

/**
 * CSV ダウンロード用 URL を組み立てる。
 * 注: <a href> でブラウザに直接ダウンロードさせるため、X-API-Key は
 * クエリパラメータでは渡せない。サーバ側はこの URL もブラウザの
 * cookie/同一オリジン経由で受ける想定。本MVPでは認証必須のため、
 * fetch でブロブ取得後 download する方式を提供する。
 */
export function buildExportCsvUrl(
  params: Record<string, string | undefined>
): string {
  const search = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") search.set(k, v);
  }
  const qs = search.toString();
  return `${getApiBaseUrl()}/export/products.csv${qs ? "?" + qs : ""}`;
}

/**
 * X-API-Key を付けて CSV を blob で取得し、ブラウザにダウンロードさせる。
 */
export async function downloadProductsCsv(
  params: Record<string, string | undefined>
): Promise<void> {
  const url = buildExportCsvUrl(params);
  const apiKey = getStoredApiKey();

  const res = await fetch(url, {
    headers: apiKey ? { "X-API-Key": apiKey } : {},
  });
  if (!res.ok) {
    throw new Error(`CSV ダウンロード失敗: HTTP ${res.status}`);
  }
  const blob = await res.blob();
  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = objectUrl;
  a.download = "products.csv";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(objectUrl);
}
