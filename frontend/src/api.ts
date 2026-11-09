import type { RunView, RunCreate } from "./api.generated";
export type { RunView, RecordView } from "./api.generated";
export interface Settings {
  openai_configured: boolean;
  models: string[];
  default_model: string;
  stages: string[];
  demo_question: string;
  subscription_credit_docs: string;
  paid_api_enabled: boolean;
  role_profiles: Record<string, { model: string; reasoning: string }>;
  chatgpt: {
    profiles: Array<{
      id: string;
      label: string;
      registration: string;
      connected: boolean;
      plan_enabled: boolean;
    }>;
    active: { id: string; label: string; plan_enabled: boolean } | null;
    usage_url: string;
  };
}
export async function request<T>(url: string, body?: unknown): Promise<T> {
  const response = await fetch(
    url,
    body === undefined
      ? undefined
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  if (!response.ok) {
    const message = await response.text();
    try {
      throw new Error(JSON.parse(message).detail || message);
    } catch (e) {
      if (e instanceof SyntaxError) throw new Error(message);
      throw e;
    }
  }
  return response.json();
}
export const api = {
  settings: () => request<Settings>("/api/settings"),
  runs: () => request<RunView[]>("/api/runs"),
  run: (id: string) => request<RunView>(`/api/runs/${id}`),
  create: (data: RunCreate) => request<RunView>("/api/runs", data),
  action: (id: string, action: string, body: unknown = {}) =>
    request<RunView>(`/api/runs/${id}/${action}`, body),
};
