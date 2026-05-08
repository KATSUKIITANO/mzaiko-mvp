import { api } from "./api";
import type { DashboardSummary } from "@/types";

export async function fetchDashboardSummary(): Promise<DashboardSummary> {
  const res = await api.get<DashboardSummary>("/dashboard/summary");
  return res.data;
}
