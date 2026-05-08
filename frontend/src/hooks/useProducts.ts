import { useQuery } from "@tanstack/react-query";
import { fetchMonitoringProducts } from "@/services/products";

export function useProducts() {
  return useQuery({
    queryKey: ["products", "monitoring"],
    queryFn: fetchMonitoringProducts,
    refetchInterval: 60_000,
  });
}
