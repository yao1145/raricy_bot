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
