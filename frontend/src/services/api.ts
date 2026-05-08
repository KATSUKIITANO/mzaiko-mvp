import axios, { AxiosError } from "axios";

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL || "/api";
const STORAGE_KEY = "mzaiko_api_key";

export const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 60_000,
});

// X-API-Key を localStorage から自動付与
api.interceptors.request.use((config) => {
  const key = localStorage.getItem(STORAGE_KEY);
  if (key) {
    config.headers.set("X-API-Key", key);
  }
  return config;
});

// 共通エラー整形
api.interceptors.response.use(
  (res) => res,
  (err: AxiosError<{ detail?: string }>) => {
    const detail = err.response?.data?.detail;
    if (detail) {
      err.message = detail;
    }
    return Promise.reject(err);
  }
);

export const getStoredApiKey = (): string =>
  localStorage.getItem(STORAGE_KEY) || "";

export const setStoredApiKey = (key: string): void => {
  if (key) {
    localStorage.setItem(STORAGE_KEY, key);
  } else {
    localStorage.removeItem(STORAGE_KEY);
  }
};

export const getApiBaseUrl = (): string => API_BASE_URL;
