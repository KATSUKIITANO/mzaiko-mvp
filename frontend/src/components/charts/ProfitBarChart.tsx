import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { MonitoringProduct } from "@/types";

interface Props {
  products: MonitoringProduct[];
}

const BUCKETS: { label: string; min: number; max: number }[] = [
  { label: "<0%", min: -Infinity, max: 0 },
  { label: "0-10%", min: 0, max: 10 },
  { label: "10-20%", min: 10, max: 20 },
  { label: "20-30%", min: 20, max: 30 },
  { label: "30%+", min: 30, max: Infinity },
];

export default function ProfitBarChart({ products }: Props) {
  const data = BUCKETS.map((b) => ({
    name: b.label,
    count: products.filter((p) => {
      const r = p.current_profit_rate;
      if (r === null || r === undefined) return false;
      return r >= b.min && r < b.max;
    }).length,
  }));

  return (
    <ResponsiveContainer width="100%" height={250}>
      <BarChart data={data}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
        <XAxis dataKey="name" stroke="#94a3b8" fontSize={12} />
        <YAxis allowDecimals={false} stroke="#94a3b8" fontSize={12} />
        <Tooltip />
        <Bar dataKey="count" fill="#3b82f6" radius={[4, 4, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}
