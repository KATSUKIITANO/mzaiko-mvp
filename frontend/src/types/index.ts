export interface TopProduct {
  id: number;
  title: string;
  overall_score: number | null;
  profit_rate: number | null;
}

export interface DashboardSummary {
  total_products: number;
  profitable_products: number;
  allowed_products: number;
  blocked_products: number;
  warning_products: number;
  danger_products: number;
  average_profit_rate: number;
  average_overall_score: number;
  top_profitable_products: TopProduct[];
  top_scored_products: TopProduct[];
}

export type MonitoringStatus = "ok" | "warning" | "danger";

export interface MonitoringProduct {
  id: number;
  title: string;
  price: number;
  selling_price: number | null;
  profit: number | null;
  current_profit_rate: number | null;
  monitoring_status: MonitoringStatus;
  profit_score: number | null;
  demand_score: number | null;
  competition_score: number | null;
  overall_score: number | null;
  risk_level: string;
  risk_reason: string[] | null;
  is_profitable: boolean;
  is_allowed: boolean;
}

export interface RankingProduct {
  rank: number;
  title: string;
  amazon_url: string;
  asin: string;
}

export interface RankingResponse {
  category: string;
  count: number;
  products: RankingProduct[];
}

export interface ApiErrorResponse {
  detail: string;
}
