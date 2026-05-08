import {
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
} from "recharts";

interface Props {
  okCount: number;
  warningCount: number;
  dangerCount: number;
}

const COLORS = {
  ok: "#10b981",
  warning: "#f59e0b",
  danger: "#ef4444",
};

export default function MonitoringPieChart({
  okCount,
  warningCount,
  dangerCount,
}: Props) {
  const data = [
    { name: "OK", value: okCount, color: COLORS.ok },
    { name: "Warning", value: warningCount, color: COLORS.warning },
    { name: "Danger", value: dangerCount, color: COLORS.danger },
  ].filter((d) => d.value > 0);

  if (data.length === 0) {
    return (
      <div className="h-[250px] flex items-center justify-center text-sm text-slate-400">
        データなし
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={250}>
      <PieChart>
        <Pie
          data={data}
          dataKey="value"
          cx="50%"
          cy="50%"
          outerRadius={80}
          label
        >
          {data.map((entry, idx) => (
            <Cell key={idx} fill={entry.color} />
          ))}
        </Pie>
        <Tooltip />
        <Legend />
      </PieChart>
    </ResponsiveContainer>
  );
}
