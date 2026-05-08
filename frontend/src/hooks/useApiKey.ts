import { useState } from "react";
import { getStoredApiKey, setStoredApiKey } from "@/services/api";

export function useApiKey() {
  const [apiKey, setApiKeyState] = useState<string>(() => getStoredApiKey());

  const setApiKey = (key: string): void => {
    setStoredApiKey(key);
    setApiKeyState(key);
  };

  return { apiKey, setApiKey, hasKey: apiKey.length > 0 };
}
