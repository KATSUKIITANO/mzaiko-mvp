import { api } from "./api";
import type { RankingResponse } from "@/types";

export async function fetchAmazonRanking(
  category: string,
  limit: number
): Promise<RankingResponse> {
  const res = await api.get<RankingResponse>("/amazon/ranking", {
    params: { category, limit },
  });
  return res.data;
}
