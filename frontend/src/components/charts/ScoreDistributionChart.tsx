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
  { label: "0-20", min: 0, max: 20 },
  { label: "20-40", min: 20, max: 40 },
  { label: "40-60", min: 40, max: 60 },
  { label: "60-80", min: 60, max: 80 },
  { label: "80-100", min: 80, max: 101 },
];

export default function ScoreDistributionChart({ products }: Props) {
  const data = BUCKETS.map((b) => ({
    name: b.label,
    count: products.filter((p) => {
      const s = p.overall_score;
      if (s === null || s === undefined) return false;
      return s >= b.min && s < b.max;
    }).length,
  }));

  return (
    <ResponsiveContainer width="100%" height={250}>
      <BarChart data={data}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
        <XAxis dataKey="name" stroke="#94a3b8" fontSize={12} />
        <YAxis allowDecimals={false} stroke="#94a3b8" fontSize={12} />
        <Tooltip />
        <Bar dataKey="count" fill="#8b5cf6" radius={[4, 4, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}
