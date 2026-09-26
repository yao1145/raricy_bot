"""Launcher 的固定文案。

与站内 Bot 的 `raricy_bot.texts` 分开归属：这里只放桌面入口、激活与
本地管理页要用的字符串。语气约定与核心一致：说明发生了什么、用户
接下来可以怎么做；不使用表情符号。
"""

from __future__ import annotations

APP_NAME: str = "Raricy Bot Light"

# 桌面入口分派（main.py）。
ENTRY_ACTIVATED: str = "检测到正在运行的实例，正在打开管理页。"
ENTRY_ACTIVATE_FAILED: str = "检测到已有实例在运行，但无法连接；请稍后再试。"
ENTRY_UNSUPPORTED_PLATFORM: str = "当前平台暂不支持运行 Light 桌面版。"
ENTRY_INTERNAL_ERROR: str = "启动失败，请查看诊断日志后重试。"

# 管理页原型（L0）的退出结果。
QUIT_ACKNOWLEDGED: str = "已退出，可以关闭本页。"

# 凭据删除（F1、D-131）：配置提交里没有「删除」动作 —— 它会在同一个提交事务里撤掉
# 运行实例与回退快照仍在引用的凭据。清除改走独立的「清除保存的凭据」入口
# （`POST /api/profiles/{id}/credentials/clear`，N2）。接口用这条固定文案指向新入口，
# 避免用户把它读成「缺凭据」，也避免把三件事（停止机器人 / 清除凭据 / 移除账号）混为一谈。
CREDENTIAL_DELETE_UNAVAILABLE: str = (
    "保存设置时不支持直接删除凭据；请使用账号页的「清除保存的凭据」独立操作"
    "（密码、模型 Key 或两者都可以单独清除），它会先停止机器人并撤销该档案的"
    "受管历史引用。"
)

# 清除部分失败（§6.1、D-144）：凭据撤了但清理待办还在；档案在清理完成前不能启动，
# 页面必须如实显示这条，不得谎报已清除。
CREDENTIALS_CLEANUP_PENDING: str = (
    "凭据清理还没有全部完成：部分条目未能从系统凭据库删除，档案在清理完成前不能启动；"
    "可以稍后重试，或按使用手册在 Windows「凭据管理器」中手工核对 RaricyBotLight 的条目。"
)

# 移除账号（N2、§6.2、D-145）：预览、确认与部分完成的固定文案。删除不可撤销，
# 文案必须把两件事分开说：移除账号（保留本地数据）与彻底删除。
REMOVAL_SCOPE_KEEP_DATA: str = "移除账号（保留本地数据）"
REMOVAL_SCOPE_PURGE_DATA: str = "彻底删除（配置、数据、知识库、记忆与日志一并删除）"
REMOVAL_IRREVERSIBLE: str = (
    "移除账号没有撤销：保留数据时档案会转为「已移除」，可以重新绑定同一账号；"
    "彻底删除会清掉本机数据，只剩一个标记已删除的墓碑。"
)
REMOVAL_RETRY_HINT: str = (
    "删除没有全部完成：已清理的部分不会回滚，档案停在「删除中」；"
    "可以重新生成预览后继续，未完成的类别会接着处理。"
)
# 稳定码的固定文案（API 的 `message` 字段用它们，页面按码取同样的说法）。
REMOVAL_UNSAFE_PATH: str = (
    "目标路径里包含链接、junction 或其他重解析点，或路径无法安全解析：为避免删到档案"
    "外面，本次删除已停止。请先人工检查档案目录，必要时先停用相关链接再重试。"
)
REMOVAL_PREVIEW_STALE: str = (
    "预览已经过期：档案或目录在这次预览之后发生了变化（账号状态、配置或 revision 变了）；"
    "请重新生成预览并再次确认。"
)
REMOVAL_TOKEN_INVALID: str = (
    "确认令牌无效、已过期或已经使用过；请重新生成删除预览并再次确认。"
)
REMOVAL_SCOPE_INVALID: str = (
    "删除范围不合法：只支持「移除账号（保留本地数据）」与「彻底删除」；"
    "已移除（保留数据）的档案只能彻底删除。"
)
REMOVAL_DATA_IN_USE: str = (
    "档案数据目录正被占用（可能还有别的程序在读写）。已清理的部分不会回滚；"
    "请先退出占用数据的程序，再重新生成预览后继续。"
)
REMOVAL_CREDENTIAL_BACKEND_UNAVAILABLE: str = (
    "系统凭据库当前不可用，无法撤销该档案的凭据引用：本次删除停在清理凭据这一步，"
    "本地数据与配置都还在。请稍后重试，重试会从这一步继续。"
)
# 「有档案但一个都没选中」（§9.1、D-145）：界面据此进账号页，而不是空的首次设置向导。
ACCOUNT_NO_SELECTION: str = (
    "当前没有选中的账号。请到「账号」页选择一个已有账号，或添加一个新账号。"
)

# 账号 API 的稳定码文案（N2 Task 4、§59）。页面按码取这里的说法；服务端在码本身
# 不足以说明「接下来做什么」时把它放进响应信封的 `message`（与既有机制同一套）。
CLIENT_UPGRADE_REQUIRED: str = (
    "页面版本过旧，缺少当前账号与代次信息；请刷新页面后重试。"
    "为避免把改动写到刚刚切换过去的账号上，这次请求没有被执行。"
)
VERIFICATION_REQUIRED: str = (
    "这个账号还没有验证过身份；请先填写站点账号与密码并完成一次性验证，再保存设置。"
)
VERIFICATION_INVALID: str = (
    "验证票据无效、已过期或已经使用过；请重新验证身份后再保存。"
)
VERIFICATION_MISMATCH: str = (
    "这次提交的账号或密码与验证时输入的不一致；请按验证时的那份输入重新提交，"
    "或重新验证身份。"
)
PROFILE_IDENTITY_TAKEN: str = (
    "这个站点账号已经绑定在另一个账号档案上；请改用它，或先在那个档案上处理。"
)
PROFILE_IDENTITY_MISMATCH: str = (
    "登录得到的站点账号与这个已移除档案原来的账号不一致；"
    "已移除的档案只能重新绑定原来的账号。"
)
PROFILE_STATE_CONFLICT: str = (
    "这个账号正在删除中，不能再修改配置或草稿；请先完成删除，或换一个账号。"
)
PROFILE_REVISION_CONFLICT: str = (
    "账号记录在本次编辑期间变过（例如改名或状态变化）；请刷新后重试。"
)
TARGET_NOT_READY: str = (
    "目标账号还没有可用的配置与凭据，无法选中或启动；请先把它配置好并验证身份。"
)
IDEMPOTENCY_KEY_REQUIRED: str = (
    "这次请求缺少或不符合幂等键要求；请由页面重新发起。"
)
IDEMPOTENCY_CONFLICT: str = (
    "同一个幂等键被用在了一次内容不同的请求上；请用新的键重试。"
)
CREDENTIAL_SCOPE_REQUIRED: str = (
    "清除凭据要至少选择一项（密码或模型 Key）。"
)
CREDENTIALS_INDEX_BROKEN: str = (
    "凭据归属索引读不出来，无法安全清除；为避免删错账号的条目，这次操作没有执行。"
    "请按使用手册处理索引文件后重试。"
)

# 托盘菜单（N3）：顺序与可用性由 tray_model 固定，文案只在这里维护。
TRAY_MENU_OPEN_ADMIN: str = "打开管理页"
TRAY_MENU_START: str = "启动机器人"
TRAY_MENU_STOP: str = "停止机器人"
TRAY_MENU_RESTART: str = "重启机器人"
TRAY_MENU_OPEN_DIAGNOSTICS: str = "打开诊断目录"
TRAY_MENU_QUIT: str = "退出 Light"

# 托盘的账号行与状态行（展示行，不可点击）。
TRAY_ACCOUNT_PREFIX: str = "账号："
TRAY_ACCOUNT_UNSET: str = "未设置"
TRAY_STATUS_PREFIX: str = "状态："

# 托盘状态标签：同一时刻只显示一条，判定顺序见 tray_model.status_label()。
TRAY_STATUS_NEEDS_SETUP: str = "未设置账号"
TRAY_STATUS_NEEDS_CREDENTIALS: str = "需重新填写凭据"
TRAY_STATUS_RECOVERY: str = "配置需要修复"
TRAY_STATUS_INVALID: str = "配置不可用"
TRAY_STATUS_STOPPED: str = "已停止"
TRAY_STATUS_STARTING: str = "启动中"
TRAY_STATUS_RUNNING: str = "运行中"
TRAY_STATUS_STOPPING: str = "正在停止"
TRAY_STATUS_FAILED: str = "启动失败"
TRAY_STATUS_FORCED_STOP: str = "上次运行被强制结束"
TRAY_STATUS_RUNNING_STALE: str = "运行中（状态过期）"
TRAY_STATUS_RUNNING_UNKNOWN: str = "运行中（暂无上报）"
TRAY_STATUS_SUSPENDED: str = "睡眠中（未在线）"
TRAY_STATUS_AWAITING_REPORT: str = "已恢复，等待新上报"
TRAY_STATUS_QUITTING: str = "正在退出"

# tooltip 只由应用名与状态标签组成，不含账号、pid、路径或原始错误（§61）。
TRAY_TOOLTIP_FORMAT: str = "{app} - {status}"
# 登录启动（N4 §8、§59）：错误响应按既有 ApiError(message=...) 机制带这些固定文案。
STARTUP_COMMAND_TOO_LONG: str = (
    "启动项命令超过 260 个字符，Windows 的启动项容纳不下；"
    "请把应用目录移到更短的路径后重试。"
)
STARTUP_PATH_UNUSABLE: str = (
    "当前形态或程序路径不能用于登录启动（开发形态、路径不存在或不可用）；"
    "请使用安装版，并确认程序文件仍在原位置。"
)
STARTUP_REGISTRATION_CONFLICT: str = (
    "注册表里同名的启动项不是本程序写入的；为避免破坏其他应用，"
    "程序不会覆盖或删除它，请先在系统的「启动应用」列表里确认该项来源。"
)
STARTUP_APPLY_FAILED: str = (
    "启动项操作没有完成：可能是权限或系统策略拒绝，也可能暂时读不到注册表；"
    "桌面偏好保持不变，可稍后重试。"
)
DESKTOP_SETTING_MOVED: str = (
    "「打开程序时启动机器人」已移到「桌面」页的桌面设置；请在那一页修改。"
)
