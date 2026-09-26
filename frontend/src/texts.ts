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
