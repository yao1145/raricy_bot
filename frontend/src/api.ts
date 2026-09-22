// 会话与 API 封装（LIGHT_EDITION_DESIGN §8.1、§11）。
//
// 引导令牌只从 URL fragment 取一次，兑换成功后立刻把地址栏清干净；
// 之后的写请求都带上会话绑定的 CSRF 值。密码与 API Key 从不写进浏览器存储。

export interface ApiFailure {
  ok: false;
  code: string;
  field?: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly field: string | null;

  constructor(status: number, code: string, field: string | null) {
    super(code);
    this.status = status;
    this.code = code;
    this.field = field;
  }
}

let csrfToken: string | null = null;

export function bootstrapToken(): string | null {
  const match = window.location.hash.match(/#token=([A-Za-z0-9_\-]+)/);
  return match ? match[1] : null;
}

export function clearFragment(): void {
  if (window.location.hash) {
    history.replaceState(null, "", window.location.pathname + window.location.search);
  }
}

async function parse(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return {};
  }
}

export async function exchange(token: string): Promise<void> {
  const response = await fetch("/api/session/exchange", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  const body = (await parse(response)) as { csrf?: string };
  if (!response.ok || typeof body.csrf !== "string") {
    throw new ApiError(response.status, "exchange_failed", null);
  }
  csrfToken = body.csrf;
}

export function haveSession(): boolean {
  return csrfToken !== null;
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin" });
  const body = await parse(response);
  if (!response.ok) throw failure(response.status, body);
  return body as T;
}

export async function apiWrite<T>(path: string, body: unknown, method = "PUT"): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-Raricy-CSRF": csrfToken ?? "",
    },
    body: JSON.stringify(body ?? {}),
  });
  const parsed = await parse(response);
  if (!response.ok) throw failure(response.status, parsed);
  return parsed as T;
}

function failure(status: number, body: unknown): ApiError {
  const payload = (body ?? {}) as Partial<ApiFailure>;
  const code = typeof payload.code === "string" ? payload.code : "request_failed";
  const field = typeof payload.field === "string" ? payload.field : null;
  return new ApiError(status, code, field);
}

export interface StatusSnapshot {
  instance_id: string;
  config: { state: string; revision: number | null; account: string | null; error: string | null };
  process: {
    state: string;
    pid: number | null;
    forced_stop: boolean;
    exit_reason: string | null;
    operation_id: string | null;
  };
  worker: { freshness: string; snapshot: Record<string, any> | null };
  tests: Record<string, { state: string; ok?: boolean; detail?: string }>;
}

export interface ConfigView {
  ok: true;
  revision: number | null;
  state: string;
  account: string | null;
  values: Record<string, unknown>;
  editable: string[];
  defaults: Record<string, unknown>;
  credentials: {
    password: { configured: boolean };
    llm_api_key: { configured: boolean };
    backend: { name: string; available: boolean };
  };
  start_bot_on_launch: boolean;
}

export interface OperationView {
  id: string;
  kind: string;
  state: string;
  result: string | null;
  revision: number | null;
  finished: boolean;
}

export function getStatus(): Promise<{ status: StatusSnapshot }> {
  return apiGet("/api/status");
}

export function getConfig(): Promise<ConfigView> {
  return apiGet("/api/config");
}

export function saveConfig(payload: Record<string, unknown>): Promise<{ revision: number }> {
  return apiWrite("/api/config", payload);
}

export function saveDraft(payload: Record<string, unknown>): Promise<{ revision: number }> {
  return apiWrite("/api/config/draft", payload);
}

export function validateConfig(payload: Record<string, unknown>): Promise<{ ok: true }> {
  return apiWrite("/api/config/validate", payload, "POST");
}

export function botAction(
  action: "start" | "stop" | "restart",
  revision?: number,
): Promise<{ operation_id: string }> {
  const body: Record<string, unknown> = {};
  if (typeof revision === "number") body.revision = revision;
  return apiWrite(`/api/bot/${action}`, body, "POST");
}

export function getOperation(id: string): Promise<{ operation: OperationView }> {
  return apiGet(`/api/operations/${encodeURIComponent(id)}`);
}

export function testSite(): Promise<{ ok: boolean; detail: string; elapsed_ms: number }> {
  return apiWrite("/api/test/site", {}, "POST");
}

export function testModel(): Promise<{ ok: boolean; detail: string; elapsed_ms: number }> {
  return apiWrite("/api/test/model", {}, "POST");
}

export function kbStatus(): Promise<{ ok: true; files: number; worker: unknown }> {
  return apiGet("/api/kb/status");
}

export function kbImport(name: string, content: string): Promise<{ name: string }> {
  return apiWrite("/api/kb/import", { name, content }, "POST");
}

export function quit(): Promise<{ message: string }> {
  return apiWrite("/api/launcher/quit", {}, "POST");
}

export interface LogEvent {
  id: string;
  at: number;
  level: string;
  event: string;
  fields: Record<string, unknown>;
}

/** 订阅近期事件流；返回关闭函数。reconnect 由浏览器自动重试（§12）。 */
export function subscribeEvents(onEvent: (event: LogEvent) => void, onGap: () => void): () => void {
  const source = new EventSource("/api/logs/stream", { withCredentials: true });
  source.addEventListener("message", () => undefined);
  source.onmessage = (message) => {
    try {
      onEvent(JSON.parse(message.data) as LogEvent);
    } catch {
      // 单条坏帧不影响整条流。
    }
  };
  source.addEventListener("gap", () => onGap());
  source.addEventListener("heartbeat", () => undefined);
  return () => source.close();
}
