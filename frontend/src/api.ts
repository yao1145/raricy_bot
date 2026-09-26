// 会话与 API 封装（LIGHT_EDITION_DESIGN §8.1、§11）。
//
// 引导令牌只从 URL fragment 取一次，兑换成功后立刻把地址栏清干净；
// 之后的写请求都带上会话绑定的 CSRF 值。密码与 API Key 从不写进浏览器存储。

export interface ApiFailure {
  ok: false;
  code: string;
  field?: string;
  /** 服务端 `texts.py` 的固定文案；只有稳定码不足以说明时才出现。 */
  message?: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly field: string | null;
  /** 服务端固定文案；不覆盖 Error.message（那里放稳定码，便于日志定位）。 */
  readonly detail: string | null;

  constructor(status: number, code: string, field: string | null, detail: string | null = null) {
    super(code);
    this.status = status;
    this.code = code;
    this.field = field;
    this.detail = detail;
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
  const detail = typeof payload.message === "string" ? payload.message : null;
  return new ApiError(status, code, field, detail);
}

export interface StatusSnapshot {
  instance_id: string;
  saved_revision?: number | null;
  running_revision?: number | null;
  restart_required?: boolean;
  /** 状态 DTO 的档案身份（N1、D-135）：D-146 起过渡入口按它们带上下文。 */
  active_profile_id?: string | null;
  running_profile_id?: string | null;
  startup_profile_id?: string | null;
  profile_epoch?: number | null;
  pending_operation?: {
    operation_id: string;
    kind: string;
    state: string;
    stage: string | null;
    profile_id: string | null;
    revision: number | null;
  } | null;
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

/** 一张账号卡片（`GET /api/profiles` 的数组元素，§59 的 ProfileCard）。 */
export interface ProfileCard {
  profile_id: string;
  display_name: string;
  account: string | null;
  site_user_id: string | null;
  identity_state: string;
  state: string;
  profile_revision: number;
  config: { state: string; revision: number | null; error: string | null };
  is_active: boolean;
  is_running: boolean;
  is_startup_target: boolean;
  credentials: {
    backend: { name: string; available: boolean };
    cleanup_pending: boolean;
    historical_managed: number;
    unknown_ownership: boolean;
  };
  /** 服务端判定的可行动作；页面不自己推断（§60）。 */
  actions: string[];
}

export interface ProfileCatalog {
  active_profile_id: string | null;
  active_epoch: number;
  catalog_revision: number;
  schema_version: number;
}

export interface ProfilesView {
  ok: true;
  catalog: ProfileCatalog;
  profiles: ProfileCard[];
}

/** 删除预览（只读）：类别、大小与事实提醒，不含任何删除动作（§59）。 */
export interface RemovalPreview {
  profile_id: string;
  display_name: string;
  site_user_id: string | null;
  state: string;
  is_active: boolean;
  running: boolean;
  is_startup_target: boolean;
  scope: string;
  allowed_scopes: string[];
  categories: { key: string; size_bytes: number; removable: boolean }[];
  size_bytes: number;
  size_complete: boolean;
  credentials: { managed: number; cleanup_pending: boolean; unknown_ownership: boolean };
  profile_revision: number;
  catalog_revision: number;
}

/** 幂等键：形状与服务端一致（`[A-Za-z0-9_-]{8,64}`），每次用户动作一个新键。 */
export function newIdempotencyKey(prefix: string): string {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  const hex = Array.from(bytes, (item) => item.toString(16).padStart(2, "0")).join("");
  return `${prefix}-${hex}`;
}

export interface ConfigView {
  ok: true;
  /** 视图所属的档案 id；还没有档案时是 null（服务端 `_config_view()`）。 */
  profile_id: string | null;
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
}

/** 桌面偏好（§58）：`desktop.json` 的三个开关与它自己的 revision。 */
export interface DesktopSettingsView {
  ok: true;
  settings_revision: number;
  launch_at_sign_in: boolean;
  start_bot_on_launch: boolean;
  startup_profile_id: string | null;
}

/** 登录启动项的一次观测事实（§59）：只含布尔事实与本程序算出的命令。 */
export interface StartupFacts {
  requested_enabled: boolean;
  registration_present: boolean;
  command_matches: boolean;
  executable_exists: boolean;
  effective_state: string;
  divergence: boolean;
  last_apply_result: string;
  pending_apply: { action: string; command: string } | null;
  expected_command: string;
}

export interface StartupStatusView extends StartupFacts {
  ok: true;
  settings_revision: number;
}

export interface DesktopSettingsSaveResult {
  ok: true;
  settings_revision: number;
  applied: boolean;
  startup: StartupFacts;
}

export interface OperationView {
  id: string;
  kind: string;
  state: string;
  /** 协调器的固定阶段码；管理器的单次启停为 null（§59）。 */
  stage?: string | null;
  result: string | null;
  profile_id?: string | null;
  revision: number | null;
  finished: boolean;
}

export function getStatus(): Promise<{ status: StatusSnapshot }> {
  return apiGet("/api/status");
}

export function getConfig(): Promise<ConfigView> {
  return apiGet("/api/config");
}

export function getDesktopSettings(): Promise<DesktopSettingsView> {
  return apiGet("/api/desktop-settings");
}

/** 写桌面偏好；`expected_settings_revision` 由调用方取当前值（§58）。 */
export function saveDesktopSettings(
  payload: Record<string, unknown>,
): Promise<DesktopSettingsSaveResult> {
  return apiWrite("/api/desktop-settings", payload);
}

export function getStartupStatus(): Promise<StartupStatusView> {
  return apiGet("/api/desktop/startup-status");
}

/** 修复启动项：只带版本守卫，服务端按当前程序路径重新生成命令（§59）。 */
export function repairStartup(expectedSettingsRevision: number): Promise<DesktopSettingsSaveResult> {
  return apiWrite(
    "/api/desktop/startup-repair",
    { expected_settings_revision: expectedSettingsRevision },
    "POST",
  );
}

export function saveConfig(payload: Record<string, unknown>): Promise<{ revision: number }> {
  return apiWrite("/api/config", payload);
}

export function saveDraft(payload: Record<string, unknown>): Promise<{ revision: number }> {
  return apiWrite("/api/config/draft", payload);
}

/** 读向导草稿（只含非敏感字段与它自己的 revision）。 */
export function getDraft(): Promise<{ revision: number; values: Record<string, unknown> }> {
  return apiGet("/api/config/draft");
}

export function validateConfig(payload: Record<string, unknown>): Promise<{ ok: true }> {
  return apiWrite("/api/config/validate", payload, "POST");
}

/** 启停的过渡门上下文（§59、D-146）：活动档案与代次，首次设置为 null / 0。 */
export interface BotContext {
  profileId: string | null;
  epoch: number | null;
  revision?: number | null;
}

export function botAction(
  action: "start" | "stop" | "restart",
  context: BotContext = { profileId: null, epoch: null },
): Promise<{ operation_id: string }> {
  const body: Record<string, unknown> = {};
  if (action !== "stop") {
    // 过渡入口要求档案与代次在场：旧页面缺字段会被 409 client_upgrade_required 拒绝。
    body.profile_id = context.profileId;
    body.expected_profile_epoch = context.epoch ?? 0;
  }
  if (typeof context.revision === "number") body.revision = context.revision;
  return apiWrite(`/api/bot/${action}`, body, "POST");
}

export function getOperation(id: string): Promise<{ operation: OperationView }> {
  return apiGet(`/api/operations/${encodeURIComponent(id)}`);
}

/** 轮询一次异步操作直到收尾；预算内没结束返回最后一次观测（不假装已完成）。 */
export async function waitForOperation(
  operationId: string,
  attempts = 180,
  intervalMs = 500,
): Promise<OperationView> {
  let last = await getOperation(operationId);
  for (let attempt = 1; attempt < attempts && !last.operation.finished; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
    last = await getOperation(operationId);
  }
  return last.operation;
}

export function getProfiles(): Promise<ProfilesView> {
  return apiGet("/api/profiles");
}

/** 建立非活动档案（首个档案在空数据根上同时成为活动档案），需幂等键。 */
export function createProfile(payload: Record<string, unknown>): Promise<{
  ok: true;
  profile_id: string;
  catalog_revision: number;
}> {
  return apiWrite("/api/profiles", payload, "POST");
}

export function getProfileDraft(
  profileId: string,
): Promise<{ revision: number; values: Record<string, unknown> }> {
  return apiGet(`/api/profiles/${encodeURIComponent(profileId)}/draft`);
}

export function saveProfileDraft(
  profileId: string,
  payload: Record<string, unknown>,
): Promise<{ revision: number }> {
  return apiWrite(`/api/profiles/${encodeURIComponent(profileId)}/draft`, payload);
}

/** 登录站点、确认稳定 ID 并签发内存票据；不写档案、不落盘输入（§59）。 */
export function verifyProfile(
  profileId: string,
  account: string,
  password: string,
): Promise<{
  ok: boolean;
  verification_id?: string;
  site_user_id?: string;
  chat_ready?: boolean;
  expires_in?: number;
  detail?: string;
}> {
  return apiWrite(
    `/api/profiles/${encodeURIComponent(profileId)}/verify`,
    { account, password },
    "POST",
  );
}

/** 保存目标档案的配置；首次绑定身份必须带上同一次验证的票据与输入（§59）。 */
export function saveProfileConfig(
  profileId: string,
  payload: Record<string, unknown>,
): Promise<{ revision: number; profile_revision: number }> {
  return apiWrite(`/api/profiles/${encodeURIComponent(profileId)}/config`, payload);
}

export function activateProfile(
  profileId: string,
  payload: Record<string, unknown>,
): Promise<{ operation_id: string }> {
  return apiWrite(`/api/profiles/${encodeURIComponent(profileId)}/activate`, payload, "POST");
}

/** 删除预览：只读，返回类别、大小与一次性确认令牌（300 秒）。 */
export function removalPreview(
  profileId: string,
  scope: string,
): Promise<{ preview: RemovalPreview; confirmation_token: string; expires_in: number }> {
  return apiWrite(
    `/api/profiles/${encodeURIComponent(profileId)}/removal-preview`,
    { scope },
    "POST",
  );
}

export function removeProfile(
  profileId: string,
  payload: Record<string, unknown>,
): Promise<{ operation_id: string }> {
  return apiWrite(`/api/profiles/${encodeURIComponent(profileId)}/remove`, payload, "POST");
}

/** 清除指定类别的受管凭据；破坏性操作走 202 + operation_id（§6.1）。 */
export function clearCredentials(
  profileId: string,
  payload: Record<string, unknown>,
): Promise<{ operation_id: string }> {
  return apiWrite(
    `/api/profiles/${encodeURIComponent(profileId)}/credentials/clear`,
    payload,
    "POST",
  );
}

export function testSite(payload: Record<string, unknown> = {}): Promise<{
  ok: boolean;
  detail: string;
  elapsed_ms: number;
  account_id?: string;
}> {
  return apiWrite("/api/test/site", payload, "POST");
}

export function testModel(payload: Record<string, unknown> = {}): Promise<{
  ok: boolean;
  detail: string;
  elapsed_ms: number;
}> {
  return apiWrite("/api/test/model", payload, "POST");
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
  /** 事件归属（§59）：全局事件为 null，不是缺键。 */
  profile_id?: string | null;
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
