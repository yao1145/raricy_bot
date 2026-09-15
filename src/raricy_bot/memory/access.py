"""Beta 接入策略：谁能用记忆能力、谁掌握共同记忆管理权（INTERFACES §28）。

纯内存、无 I/O（D-55）。`allowlist` 是**总闸**，不是「共同记忆的读白名单」：共同记忆读取、
私有记忆读写、记忆命令与自动提取都要求非空稳定 `user_id` 出现在 `allow_user_list` 里；
`all` 模式只放开共同记忆，私有记忆与命令仍要求非空 `user_id` 且频道为 DM（D-56）。

判据一律是站点稳定的 `author.id`；站点 DTO 的 `is_admin` 字段与本模块无关 —— 那是站点权限，
不是记忆权限。`enabled=False`（默认）时四个方法全 False，调用方据此走原有无记忆路径（D-60）。
"""

from __future__ import annotations

from ..config import MemoryConfig

# 私聊频道标识：私有记忆与记忆管理命令只在 DM 生效（§28）。
_DM_CHANNEL_KIND: str = "dm"


class MemoryAccessPolicy:
    """按 `MemoryConfig` 求值的 Beta 门禁；构造时取快照，之后只读。"""

    def __init__(self, config: MemoryConfig) -> None:
        self._enabled: bool = bool(config.enabled)
        # 只有逐字的 "all" 才是全开模式；其余取值（含配置校验拒绝的非法值）一律按灰度处理。
        self._allow_all: bool = config.access_mode == "all"
        self._allow_user_list: frozenset[str] = frozenset(config.allow_user_list)
        self._admin_user_list: frozenset[str] = frozenset(config.admin_user_list)

    def permits_common(self, user_id: str | None) -> bool:
        """能否读取共同记忆（`all_user` / `lobby`）。

        `all` 模式对所有消息作者为真，`user_id` 为 None（评论作者 ID 为空）也为真；
        `allowlist` 模式要求非空 `user_id` 且在名单内。
        """
        if not self._enabled:
            return False
        if self._allow_all:
            return True
        return bool(user_id) and user_id in self._allow_user_list

    def permits_private(self, user_id: str | None, channel_kind: str) -> bool:
        """能否读取/写入该用户的私有记忆：通过接入门且频道为 DM。

        `all` 模式下同样要求非空稳定 `user_id`：大区、评论与没有作者 ID 的请求永远拿不到私有记忆。
        """
        if not self._enabled:
            return False
        if channel_kind != _DM_CHANNEL_KIND:
            return False
        return self._passes_gate(user_id)

    def permits_commands(self, user_id: str | None, channel_kind: str) -> bool:
        """能否执行记忆管理命令：与私有记忆同一道门，只在 DM 生效（§34.1）。

        大区里的同一文本由 Router 回固定本地提示，不进入本模块。
        """
        return self.permits_private(user_id, channel_kind)

    def is_admin(self, user_id: str | None) -> bool:
        """能否治理共同记忆：**同时**满足接入门与 `admin_user_list`。

        绝不看站点 DTO 的 `is_admin`：那道门在 `allowlist` 模式下会被接入门拒绝的账号，
        不能一边被 Beta 门禁挡住、一边握着 approve / reject / delete（D-55）。
        """
        if not self._enabled:
            return False
        if not self._passes_gate(user_id):
            return False
        return user_id in self._admin_user_list

    def _passes_gate(self, user_id: str | None) -> bool:
        """接入门：`all` 模式只要求非空 `user_id`，`allowlist` 模式还要求在名单内。"""
        if not user_id:
            return False
        if self._allow_all:
            return True
        return user_id in self._allow_user_list
