"""HKCU 登录启动项：协议、固定错误与标准库实现（设计 §8、§59、D-148）。

只碰本产品自己的登记项：根固定 ``winreg.HKEY_CURRENT_USER``，子键固定
``Software\\Microsoft\\Windows\\CurrentVersion\\Run``，值名由调用方给出（服务层只用
``RaricyBotLight``）。不枚举、不遍历其他启动项，也不读、不写未文档化的
``StartupApproved`` —— 系统侧的禁用决定由 Windows 自己记录，本程序只如实报告自己
知道的事实（INTERFACES §59）。

``winreg`` 是标准库，但只在 Windows 上存在：本模块本身在任何平台都可 import
（测试要拿协议与错误类型），真正的 ``import winreg`` 放在方法里惰性执行。

错误消息一律是稳定类别码，不透传 ``WinError`` 原文：注册表错误文本里可能带路径
或别的应用的值名，而它会被写进设置文件与日志。

视图：访问掩码按 brief 只要 ``KEY_READ`` / ``KEY_SET_VALUE``，不额外指定
``KEY_WOW64_*``，因此操作的是与进程位数相同的注册表视图（发行包与系统同为 64 位）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# Run 键的确切位置（确切值总表）：根只用 HKCU，机器级根在本文件里一次都不出现。
RUN_KEY: str = r"Software\Microsoft\Windows\CurrentVersion\Run"

# 适配层的稳定类别码：只在平台层与服务层之间流转，不对外（服务层映射为
# `read_failed` / `apply_failed`）；对外稳定码见 INTERFACES §58/§59。
REGISTRY_READ_FAILED: str = "startup_registry_read_failed"
REGISTRY_WRITE_FAILED: str = "startup_registry_write_failed"
REGISTRY_DELETE_FAILED: str = "startup_registry_delete_failed"


class StartupRegistryError(Exception):
    """启动项读写的固定错误；消息是稳定类别码，不是 ``WinError`` 原文。"""


@runtime_checkable
class StartupRegistry(Protocol):
    """登录启动项的读写边界：业务层只依赖这三个方法。

    真实实现只写当前用户的 Run 键（``WinRegistryStartup``）；测试与离线流程一律
    注入内存替身，不打开真实注册表（N4 计划的全局约束）。
    """

    def read_value(self, name: str) -> tuple[bool, str | None]:
        """读取一个值：``(是否存在, 内容)``。

        键或值不存在返回 ``(False, None)``，这不是错误；读不到（权限、策略）抛
        ``StartupRegistryError``。存在但不是字符串类型的值（如 REG_DWORD）返回
        ``(True, None)``：存在性是事实，内容无法当作命令确认。
        """
        ...

    def write_value(self, name: str, command: str) -> None:
        """以 ``REG_SZ`` 写入一个值；失败抛 ``StartupRegistryError``。"""
        ...

    def delete_value(self, name: str) -> None:
        """删除一个值；值不存在视为已完成（幂等）。"""
        ...


def _winreg():
    """惰性导入标准库 ``winreg``：非 Windows 上 import 本模块本身不应失败。"""
    import winreg

    return winreg


class WinRegistryStartup:
    """``winreg`` 实现：只读 ``KEY_READ``、只写 ``KEY_SET_VALUE``，根只用 HKCU。"""

    def read_value(self, name: str) -> tuple[bool, str | None]:
        winreg = _winreg()
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
                try:
                    value, _kind = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    # Run 键在、值不在：正常状态，不是错误。
                    return (False, None)
        except FileNotFoundError:
            # Run 键本身不存在（极少见）：等同没有任何启动项。
            return (False, None)
        except OSError as exc:
            # 权限、策略、句柄错误：必须报「读不到」。返回 (False, None) 会让界面
            # 把「读不出来」显示成「已关闭」，正是本设计要避免的谎报。
            raise StartupRegistryError(REGISTRY_READ_FAILED) from exc
        if not isinstance(value, str):
            # 同名值是别的类型：存在但内容无法确认，交给服务层按「非本产品持有」处理。
            return (True, None)
        return (True, value)

    def write_value(self, name: str, command: str) -> None:
        winreg = _winreg()
        try:
            key = winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
            )
        except OSError as exc:
            raise StartupRegistryError(REGISTRY_WRITE_FAILED) from exc
        try:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
        except OSError as exc:
            raise StartupRegistryError(REGISTRY_WRITE_FAILED) from exc
        finally:
            winreg.CloseKey(key)

    def delete_value(self, name: str) -> None:
        winreg = _winreg()
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    return
        except FileNotFoundError:
            return
        except OSError as exc:
            raise StartupRegistryError(REGISTRY_DELETE_FAILED) from exc
