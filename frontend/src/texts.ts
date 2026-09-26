// 管理页的固定文案（与 Python 侧 `texts.py` 同一约定：说明发生了什么、可以怎么做）。
// 恢复面板按服务端给的机器码取文案，组件不自己拼接中文长句（设计 §58 的元数据恢复语义）。

export interface RecoveryNotice {
  title: string;
  detail: string;
}

// 键与服务端 `ConfigStatus.error` 的稳定码一一对应；四种故障必须互相可区分。
export const RECOVERY_NOTICES: Record<string, RecoveryNotice> = {
  metadata_corrupt: {
    title: "配置元数据已损坏",
    detail:
      "活动档案的指针文件读不出内容（语法损坏或结构不符合预期）。程序不会自动重建它，以免覆盖原始现场。",
  },
  metadata_unreadable: {
    title: "配置元数据暂时读不到",
    detail:
      "指针文件被占用或没有读取权限，因此无法确认当前档案。这不等于数据不存在，也不是首次运行。",
  },
  metadata_unsupported_version: {
    title: "配置元数据版本过新",
    detail:
      "指针文件来自更新的程序版本；按未知格式解释可能损坏既有数据，因此保持原样不动。",
  },
  metadata_pointer_invalid: {
    title: "活动档案指针无效",
    detail:
      "指针缺失或不是合法的档案标识。程序不会另建档案，以免把已有数据丢在一边。",
  },
};

const FALLBACK_NOTICE: RecoveryNotice = {
  title: "配置数据需要恢复",
  detail: "检测到本机配置的元数据异常；程序不自动修复，也不改写任何文件。",
};

/** 本阶段只做只读提示：不提供修复、重建或「重新开始」按钮（F2）。 */
export const RECOVERY_NEXT_STEP: string =
  "下一步：请保留现场，不要删除数据目录、不要手工改写 launcher.json；需要处理时把此处的机器码一并提供给维护者。";

export function recoveryNotice(code: string | null | undefined): RecoveryNotice {
  return (code ? RECOVERY_NOTICES[code] : undefined) ?? FALLBACK_NOTICE;
}

// 409 冲突码的固定文案：与服务端信封的 `code` 一一对应（F3 的生命周期门）。
// 组件只按码取文案，不自己拼接中文长句；未命中的码仍回退到服务端文案或原始码。
export const CONFLICT_NOTICES: Record<string, string> = {
  lifecycle_busy:
    "另一个生命周期操作正在进行（站点测试或启停），请等它结束后再试。",
  desktop_settings_conflict:
    "桌面设置已被另一个页面改过，请刷新后重试。",
};

export function conflictNotice(code: string | null | undefined): string | undefined {
  return code ? CONFLICT_NOTICES[code] : undefined;
}

// 登录启动的状态文案（N4 §8、§60）：键是服务端 `effective_state` 的机器码，
// 与 `RECOVERY_NOTICES` 同构。`enabled` 的语义只是「登记完整且路径有效」：
// 系统侧的禁用决定本程序读不到、也不覆盖，所以不得写成「下次登录必定启动」。
export const STARTUP_STATUS_NOTICES: Record<string, RecoveryNotice> = {
  enabled: {
    title: "登录启动已登记",
    detail:
      "启动项登记完整且程序路径有效。Windows 可能延迟执行，或按你在系统设置里的选择跳过；本程序不修改该选择，也不保证每次登录都会启动。",
  },
  disabled: {
    title: "登录启动已关闭",
    detail: "注册表里没有本程序的启动项，且本次关闭已回读确认。",
  },
  needs_repair: {
    title: "启动项需要修复",
    detail:
      "意图为开启但登记不完整：启动项可能被删除、程序目录被移动，或可执行文件不在原位置。",
  },
  unknown: {
    title: "启动项状态未知",
    detail:
      "读不到注册表、同名值无法确认是本程序写入的，或上次操作没有回读确认；程序不会据此报告为已关闭。",
  },
};

const FALLBACK_STATUS_NOTICE: RecoveryNotice = {
  title: "启动项状态未知",
  detail: "服务端没有给出可识别的状态码；请刷新后重试。",
};

export function startupStatusNotice(code: string | null | undefined): RecoveryNotice {
  return (code ? STARTUP_STATUS_NOTICES[code] : undefined) ?? FALLBACK_STATUS_NOTICE;
}

// 上次应用结果码的固定文案（与 `last_apply_result` 一一对应）。
export const STARTUP_RESULT_NOTICES: Record<string, string> = {
  ok: "上次应用已回读确认。",
  not_attempted: "还没有执行过启动项应用。",
  command_too_long: "上次应用失败：命令超过长度上限，请把应用目录移到更短的路径。",
  path_unusable: "上次应用失败：当前形态或程序路径不能用于登录启动。",
  registration_conflict:
    "上次应用失败：注册表里的同名项不是本程序写入的，程序不会覆盖或删除它。",
  apply_failed: "上次应用失败：可能被权限或系统策略拒绝。",
  read_failed: "上次读注册表失败，无法确认登记事实。",
};

export function startupResultNotice(code: string | null | undefined): string | undefined {
  return code ? STARTUP_RESULT_NOTICES[code] : undefined;
}

// 账号页文案（N2 §9、§59、§60）：机器码 → 固定中文长句的唯一来源。
// 稳定码的说法与 `src/raricy_launcher/texts.py` 的对应常量保持一致；组件只按码取，
// 不自己拼接长句，也不另写一套说法。

/** 档案状态徽标（`ProfileCard.state`）。 */
export const PROFILE_STATE_LABELS: Record<string, string> = {
  active: "在用",
  detached: "已移除（保留数据）",
  deleting: "删除中",
};

/** 配置状态徽标（`ProfileCard.config.state` 与状态 DTO 的 `config.state`）。 */
export const CONFIG_STATE_LABELS: Record<string, string> = {
  configured: "已配置",
  needs_credentials: "需要重新填写凭据",
  needs_setup: "尚未配置",
  invalid: "配置无效",
  no_selection: "没有选中账号",
};

/** 身份状态徽标（`ProfileCard.identity_state`）。 */
export const IDENTITY_LABELS: Record<string, string> = {
  verified: "身份已验证",
  unverified: "身份未验证",
};

/** `actions` 的按钮标签：服务端给动作，页面只负责起名字（§60）。 */
export const PROFILE_ACTION_LABELS: Record<string, string> = {
  activate: "选中",
  activate_and_start: "选中并启动",
  edit: "编辑设置",
  clear_credentials: "清除保存的凭据",
  remove: "移除账号（保留数据）",
  purge: "彻底删除",
  verify: "验证身份",
  rebind: "重新绑定",
};

/** 切换操作的结果码（§59）：如实报「只选中」与「被停止意图取消」。 */
export const ACTIVATE_RESULTS: Record<string, string> = {
  started: "已经切换并启动目标账号。",
  selected: "已经选中目标账号（这次没有启动机器人）。",
  selected_only: "切换期间收到了停止请求：目标账号已选中，但没有启动机器人。",
  cancelled_by_stop: "切换期间收到了停止请求：仍停在原来的账号，没有启动机器人。",
  start_failed: "目标账号已选中，但启动失败；它不会自动回到原来的账号，请查看状态与近期事件。",
};

/**
 * 账号相关稳定码的固定文案（409/422）：与服务端 `texts.py` 的常量同义。
 * 服务端在这几个码上会另带 `message`（页面优先用服务端那份），这里的表是
 * 页面层的同源兜底：没有 message 时也给出「接下来做什么」。
 */
export const ACCOUNT_CODE_NOTICES: Record<string, string> = {
  client_upgrade_required:
    "页面版本过旧，缺少当前账号与代次信息；请刷新页面后重试。为避免把改动写到刚刚切换过去的账号上，这次请求没有执行。",
  verification_required:
    "这个账号还没有验证过身份；请先填写站点账号与密码并完成一次性验证，再保存设置。",
  verification_invalid: "验证票据无效、已过期或已经使用过；请重新验证身份后再保存。",
  verification_mismatch:
    "这次提交的账号或密码与验证时输入的不一致；请按验证时的那份输入重新提交，或重新验证身份。",
  profile_identity_taken:
    "这个站点账号已经绑定在另一个账号档案上；请改用它，或先在那个档案上处理。",
  profile_identity_mismatch:
    "登录得到的站点账号与这个已移除档案原来的账号不一致；已移除的档案只能重新绑定原来的账号。",
  profile_state_conflict:
    "这个账号正在删除中，不能再修改配置或草稿；请先完成删除，或换一个账号。",
  profile_revision_conflict:
    "账号记录在本次编辑期间变过（例如改名或状态变化）；请刷新后重试。",
  target_not_ready:
    "目标账号还没有可用的配置与凭据，无法选中或启动；请先把它配置好并验证身份。",
  idempotency_key_required: "这次请求缺少或不符合幂等键要求；请由页面重新发起。",
  idempotency_conflict: "同一个幂等键被用在了一次内容不同的请求上；请用新的键重试。",
  credential_scope_required: "清除凭据要至少选择一项（密码或模型 Key）。",
  credentials_index_corrupt:
    "凭据归属索引读不出来，无法安全清除；为避免删错账号的条目，这次操作没有执行。",
  credentials_index_unreadable:
    "凭据归属索引读不出来，无法安全清除；为避免删错账号的条目，这次操作没有执行。",
  credentials_index_unsupported_version:
    "凭据归属索引读不出来，无法安全清除；为避免删错账号的条目，这次操作没有执行。",
  removal_scope_invalid:
    "删除范围不合法：只支持「移除账号（保留本地数据）」与「彻底删除」；已移除（保留数据）的档案只能彻底删除。",
  removal_token_invalid: "确认令牌无效、已过期或已经使用过；请重新生成删除预览并再次确认。",
  removal_preview_stale:
    "预览已经过期：档案或目录在这次预览之后发生了变化；请重新生成预览并再次确认。",
  removal_unsafe_path:
    "目标路径里包含链接、junction 或其他重解析点，或路径无法安全解析；本次删除已停止，请先人工检查档案目录。",
  data_in_use:
    "档案数据目录正被占用（可能还有别的程序在读写）。已清理的部分不会回滚；请先退出占用数据的程序，再重新预览后继续。",
  credential_backend_unavailable:
    "系统凭据库当前不可用，无法撤销该档案的凭据引用：本地数据与配置都还在，请稍后重试。",
  bot_running: "机器人正在运行：身份验证需要先停止它，避免站点会话被挤掉。",
  quitting: "程序正在退出，启动与创建入口已经关闭；请重新打开 Light 后再试。",
  invalid_test_input: "站点账号或密码的格式不符合要求（账号 1–256 字符、密码 1–4096 字符）。",
  not_found: "找不到这个档案或操作；它可能已经被删除，请刷新后重试。",
};

export function accountCodeNotice(code: string | null | undefined): string | undefined {
  return code ? ACCOUNT_CODE_NOTICES[code] : undefined;
}

// 概览页在「没有可用当前账号」时的提示面板（§9、§59）：每种情况各自的标题、
// 说明与行动按钮，不再把所有情况都带回空的首次设置向导；恢复面板保持只读。
export interface AccountNotice {
  title: string;
  detail: string;
  action: string | null;
  /** 行动按钮去哪个页签；`null` 表示没有按钮（只解释事实）。 */
  target: "accounts" | "settings" | null;
}

export const ACCOUNT_NOTICES: Record<string, AccountNotice> = {
  no_selection: {
    title: "当前没有选中的账号",
    detail: "已有账号档案，但一个都没有选中。请到「账号」页选择一个账号，或添加一个新账号。",
    action: "去账号页",
    target: "accounts",
  },
  needs_credentials: {
    title: "需要重新填写凭据",
    detail:
      "当前账号缺少可用的密码或模型 Key（可能刚被清除，或系统凭据库不可用）。填写并保存后，机器人才能启动。",
    action: "去当前账号设置",
    target: "settings",
  },
  invalid: {
    title: "配置无效",
    detail:
      "当前账号的正式配置读不出来或结构不符合预期。先到设置页检查字段并重新保存；无法恢复时按诊断说明保留现场。",
    action: "去当前账号设置",
    target: "settings",
  },
  identity_unverified: {
    title: "身份未验证",
    detail:
      "这个账号还没有验证过站点身份。未验证的账号不能启动机器人；请在账号页完成一次性验证。",
    action: "去账号页验证",
    target: "accounts",
  },
};

// 清除保存的凭据（§6.1、§60）：类别标签、确认与待办文案。
export const CREDENTIAL_KIND_LABELS: Record<string, string> = {
  password: "站点密码",
  llm_api_key: "模型 Key",
};

// 验证身份 / 重新绑定（§4.2、§6.2、§59）：登录会挤掉同一个账号的会话，
// 因此运行中必须先停止机器人，而且验证完不自动重启（停止是用户的动作）。
export const VERIFY_HINT: string =
  "验证会真的用这次填写的账号与密码登录站点，只签发内存票据（600 秒、一次性），不写入档案；" +
  "机器人正在运行时这次登录会挤掉它的站点会话，所以要先停止它。";

export const REBIND_HINT: string =
  "已移除（保留数据）的档案只能重新绑定原来的站点账号：登录得到的稳定 ID 与原来不一致会被拒绝。";

export const VERIFY_STOP_CONFIRM: string =
  "身份验证需要用这份账号密码登录站点，机器人正在运行时会被挤掉会话。需要暂时停止当前机器人，" +
  "验证完成后不会自动重启它。确定继续吗？";

export const VERIFY_STOP_FAILED: string =
  "机器人没有在预期时间内停下，身份验证没有开始；请到状态页确认进程状态后再试。";

export const VERIFY_SAVED: string =
  "身份已验证并保存。机器人停在停止状态：需要时到「状态」页手动启动，程序不会自动重启它。";

export const VERIFY_CHAT_NOTICE: string =
  "登录成功，但聊天权限探测没有通过；身份已经写入，聊天权限请按站点侧确认。";

export const CREDENTIALS_CLEAR_CONFIRM: string =
  "清除会先停止该账号正在运行的机器人，再撤销它在当前配置与全部历史快照里引用过的条目；" +
  "清掉的秘密不会从旧快照里复活，档案随后进入「需要重新填写凭据」。这不会注销站点账号，" +
  "也不影响站点上已经发布的内容。确定继续吗？";

export const CREDENTIALS_CLEANUP_PENDING: string =
  "凭据清理还没有全部完成：部分条目未能从系统凭据库删除，档案在清理完成前不能启动；" +
  "可以稍后重试，或按使用手册在 Windows「凭据管理器」中手工核对 RaricyBotLight 的条目。";

export const CREDENTIALS_UNKNOWN_OWNERSHIP: string =
  "有历史快照读不出来：无法从中判断还引用过哪些凭据。能读到的引用照样撤销，但可能仍有残留；" +
  "必要时按使用手册在 Windows「凭据管理器」中人工核对。";

/** 清除操作的结果码（§59）：如实报部分完成，不谎报已清除。 */
export const CREDENTIALS_CLEAR_RESULTS: Record<string, string> = {
  cleared: "已清除所选凭据；档案现在需要重新填写凭据，补填后即可再次启动。",
  cleared_partial:
    "凭据清除只完成了一部分：档案已进入「需要重新填写凭据」，但仍有清理待办，请稍后重试。",
  clear_failed: "清除没有完成：这次没有撤销任何凭据，请稍后重试或查看近期事件。",
};

// 删除账号（§6.2、§60）：范围标签、两步确认文案与结果码。
export const REMOVAL_SCOPE_LABELS: Record<string, string> = {
  keep_data: "移除账号（保留本地数据）",
  purge_data: "彻底删除（配置、数据、知识库、记忆与日志一并删除）",
};

export const REMOVAL_SCOPE_DETAILS: Record<string, string> = {
  keep_data:
    "停止该账号的机器人、撤销它引用过的凭据，并把档案转为「已移除」；配置、数据库、知识库、记忆与日志都留在磁盘上，之后可以用同一站点账号重新绑定。",
  purge_data:
    "在上一项的基础上，再删除该档案的配置、草稿与配置版本、数据库、长期记忆、知识库与日志，只留一个「已删除」的标记文件和空的锁目录。",
};

/** 两种模式都不提供撤销，也不影响站点上的已发内容（预览对话框常驻显示）。 */
export const REMOVAL_IRREVERSIBLE: string =
  "移除账号没有撤销：保留数据时档案会转为「已移除」，可以重新绑定同一账号；彻底删除会把本机数据清掉，只剩一个标记已删除的墓碑。两种模式都不删除站点上的任何内容，也不做磁盘安全擦除。";

export const REMOVAL_CONFIRM_KEEP: string =
  "我确认移除这个账号（保留本地数据），并且明白没有撤销。";

export const REMOVAL_CONFIRM_PURGE: string =
  "我确认彻底删除这个账号的本机数据，并且明白没有撤销、无法恢复。";

export const REMOVAL_UNKNOWN_CREDENTIALS: string =
  "有读不出来的历史快照：无法确认还引用过哪些凭据；能撤销的会照常撤销，但可能仍有残留，需要时到 Windows「凭据管理器」人工核对。";

export const REMOVAL_CLEANUP_PENDING: string =
  "这个档案还有未完成的凭据清理待办；删除会先处理它，个别条目可能仍需人工核对。";

export const REMOVAL_SIZE_INCOMPLETE: string =
  "类别大小是有界扫描的估计值（条目过多或有目录读不到时只统计到部分），不是精确占用。";

export const REMOVAL_CATEGORY_LABELS: Record<string, string> = {
  config: "配置与草稿",
  revisions: "配置版本",
  runtime: "运行记录",
  database: "数据库",
  memory: "长期记忆",
  knowledge: "知识库",
  logs: "日志",
  credentials: "凭据引用",
};

/** 删除操作的结果码（§59）：部分完成如实显示，并指向重试。 */
export const REMOVAL_RESULTS: Record<string, string> = {
  removed_detached: "已移除账号并保留本地数据；档案仍可以重新绑定同一站点账号。",
  removed_purged: "已彻底删除该账号的本机数据，只留下标记文件和空的锁目录。",
  removed_partial:
    "删除没有全部完成：已清理的部分不会回滚，档案停在「删除中」；重新预览后可以从剩下的类别继续。",
  remove_failed:
    "删除没有完成：这次没有满足执行条件（数据被占用、凭据库不可用或路径不安全）；记录停在出事的阶段，重新预览后可以继续。",
};

/** remove 操作停在的阶段（§59）：说明这次卡在哪一步。 */
export const REMOVAL_STAGE_LABELS: Record<string, string> = {
  preview: "准备删除",
  stop: "停止机器人并取得数据排他锁",
  clear_credentials: "撤销凭据引用",
  detach: "转为已移除",
  purge_data: "删除数据",
  finalize: "收尾",
};

export const REMOVAL_RETRY_HINT: string =
  "已清理的部分不会回滚；「重新预览」会列出还剩哪些类别，确认后从这一步继续。";

export const REMOVAL_IN_PROGRESS: string =
  "删除还在进行中；账号页与状态页会继续刷新它的阶段，等它收尾或失败后再决定是否重新预览。";
