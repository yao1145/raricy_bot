// 表单字段的轻量元数据（设计 §5.3）：标签与帮助在这里，业务校验仍在服务端。
// `key` 必须与 Launcher 的 EDITABLE_FIELDS 一致；服务端不认识的关键字会被拒绝。

export type FieldKind = "text" | "number" | "bool" | "list" | "level" | "prompt";

export interface FieldSpec {
  key: string;
  label: string;
  kind: FieldKind;
  group: "simple" | "advanced";
  help?: string;
}

export const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];

export const FIELDS: FieldSpec[] = [
  {
    key: "model.base_url",
    label: "模型 API 地址",
    kind: "text",
    group: "simple",
    help: "兼容 OpenAI 的地址；非本机地址必须是 https，且不能带用户名或查询串",
  },
  { key: "model.model", label: "模型名称", kind: "text", group: "simple" },
  {
    key: "model.vision_enabled",
    label: "图片理解",
    kind: "bool",
    group: "simple",
    help: "开启后图片会送往模型服务，且模型必须支持图片",
  },
  { key: "comments.enabled", label: "博客评论", kind: "bool", group: "simple" },
  {
    key: "knowledge_base.enabled",
    label: "本地知识库",
    kind: "bool",
    group: "simple",
    help: "启用前必须选定使用范围并导入至少一份 Markdown",
  },
  {
    key: "knowledge_base.access_mode",
    label: "知识库访问模式",
    kind: "text",
    group: "simple",
    help: "allowlist（默认，只放行下面列出的用户）或 all_chat（所有精确 @ 的会话）",
  },
  {
    key: "knowledge_base.allowed_channel_kinds",
    label: "知识库可用频道",
    kind: "list",
    group: "simple",
    help: "逗号分隔：dm 与/或 lobby",
  },
  {
    key: "knowledge_base.allowed_user_ids",
    label: "知识库允许的用户 ID",
    kind: "list",
    group: "simple",
    help: "逗号分隔的稳定用户 ID",
  },
  { key: "memory.enabled", label: "长期记忆", kind: "bool", group: "simple" },
  {
    key: "memory.access_mode",
    label: "记忆访问模式",
    kind: "text",
    group: "simple",
    help: "allowlist 或 all",
  },
  {
    key: "memory.allow_user_list",
    label: "记忆允许的用户 ID",
    kind: "list",
    group: "simple",
    help: "逗号分隔；写命令只对这些人生效",
  },
  {
    key: "memory.admin_user_list",
    label: "共同记忆管理员",
    kind: "list",
    group: "simple",
    help: "逗号分隔；与「允许使用者」是两份名单",
  },
  { key: "system_prompt", label: "System Prompt", kind: "prompt", group: "simple" },
  { key: "model.temperature", label: "温度", kind: "number", group: "advanced" },
  { key: "model.timeout_seconds", label: "模型超时（秒）", kind: "number", group: "advanced" },
  {
    key: "model.max_output_tokens",
    label: "单次输出 token 上限",
    kind: "number",
    group: "advanced",
  },
  {
    key: "site.request_timeout_seconds",
    label: "站点请求超时（秒）",
    kind: "number",
    group: "advanced",
  },
  { key: "behavior.context_turns", label: "上下文轮数", kind: "number", group: "advanced" },
  {
    key: "behavior.context_input_tokens",
    label: "上下文输入 token 上限",
    kind: "number",
    group: "advanced",
  },
  {
    key: "behavior.max_input_chars",
    label: "单条输入字符上限",
    kind: "number",
    group: "advanced",
  },
  { key: "behavior.max_output_chars", label: "回复字符上限", kind: "number", group: "advanced" },
  { key: "behavior.concurrency", label: "并发", kind: "number", group: "advanced" },
  { key: "behavior.queue_size", label: "队列长度", kind: "number", group: "advanced" },
  {
    key: "comments.recent_poll_seconds",
    label: "评论轮询间隔（秒）",
    kind: "number",
    group: "advanced",
  },
  {
    key: "comments.notification_poll_seconds",
    label: "通知轮询间隔（秒）",
    kind: "number",
    group: "advanced",
  },
  { key: "comments.context_turns", label: "评论上下文轮数", kind: "number", group: "advanced" },
  {
    key: "comments.context_input_tokens",
    label: "评论上下文 token 上限",
    kind: "number",
    group: "advanced",
  },
  { key: "logging.level", label: "日志级别", kind: "level", group: "advanced" },
];

/** 服务端取值 → 输入框文本；空值保持空串（草稿允许「还没填」）。 */
export function toInput(spec: FieldSpec, value: unknown): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

/** 输入框文本 → 服务端取值；空白一律视为「没填」，交给服务端的草稿规则。 */
export function fromInput(spec: FieldSpec, text: string): unknown {
  const trimmed = text.trim();
  if (spec.kind === "bool") return trimmed === "true";
  if (spec.kind === "number") {
    if (!trimmed) return "";
    const parsed = Number(trimmed);
    return Number.isFinite(parsed) ? parsed : text;
  }
  if (spec.kind === "list") {
    if (!trimmed) return [];
    return trimmed
      .split(/[,，\s]+/)
      .map((item) => item.trim())
      .filter(Boolean);
  }
  return text;
}
