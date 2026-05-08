import { useQuery } from "@tanstack/react-query";
import { fetchAmazonRanking } from "@/services/ranking";

export function useRanking(category: string, limit: number, enabled: boolean) {
  return useQuery({
    queryKey: ["ranking", category, limit],
    queryFn: () => fetchAmazonRanking(category, limit),
    enabled,
    staleTime: 5 * 60 * 1000,
  });
}
