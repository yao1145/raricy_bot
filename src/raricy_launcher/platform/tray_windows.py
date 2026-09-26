"""Windows 托盘窗口层：一个隐藏的顶层窗口 + 通知区域图标（N3 Task 4，契约见 §61.4）。

**为什么是隐藏的顶层窗口，而不是 `HWND_MESSAGE` 消息窗口**：`TaskbarCreated`、
`WM_POWERBROADCAST`、`WM_QUERYENDSESSION`/`WM_ENDSESSION` 都是**广播给顶层窗口**的，
消息窗口收不到 —— 用消息窗口省下的那点资源换来的代价是 Explorer 重启后图标再也回不来、
注销/关机也看不到。

窗口回调只做三件事：把消息映射成 `tray_model` 词表里的常量交给 `on_message`、渲染
（`NIM_MODIFY`）、返回。回调内**不做**文件/注册表/网络/keyring/子进程/等待：耗时的启停与
完整快照都在协调器线程里（§7.2）。任何异常都在回调里被吞掉并记
`launcher.tray_callback_failed` —— 抛回消息循环会连带弄死托盘本身。

**本机 pywin32 事实**（都用一个真实调用复核过，写在这里免得下一个人再猜；本机
pywin32 build 312 + Python 3.13）：

- `win32gui` 有 `NIM_ADD`/`NIM_MODIFY`/`NIM_DELETE`/`NIM_SETVERSION` 与
  `NIF_ICON`/`NIF_TIP`/`NIF_MESSAGE`；没有 `NOTIFYICON_VERSION_4`、`NIN_SELECT`、
  `NIN_KEYSELECT`（按 shellapi.h 本地定义），也没有 `RegisterClassEx`（只有 `RegisterClass`）。
- `Shell_NotifyIcon(Message, nid)` 的 `nid` 是**元组**
  `(hwnd, uID, uFlags, uCallbackMessage, hIcon, szTip)`；`NIM_SETVERSION` 用**八元组**
  （第 7 位 `szInfo` 留空串、第 8 位是 `uTimeout`/`uVersion` 联合槽，放版本号 4）。
  成功返回 `None`，**失败抛 `pywintypes.error`**（不是返回 0）。
- `SM_CXSMICON`/`SM_CYSMICON` 在 `win32con`（不在 `win32gui`），值用
  `win32api.GetSystemMetrics(...)` 取。
- `LoadImage(..., LR_LOADFROMFILE)` 在文件缺失时**抛** `pywintypes.error`，不是返回 0。
- `GetMessage(None, 0, 0)` 返回**列表** `[ret, (hwnd, msg, wParam, lParam, time, pt)]`，
  `WM_QUIT` 时是 `[0, (...)]`（不是整数 0）；`TranslateMessage`/`DispatchMessage` 收的是
  内层那个消息元组。旧版 pywin32 在 `WM_QUIT` 时直接返回整数 0，两种形状都按退出处理。
- `DestroyWindow`/`DestroyIcon`/`UnregisterClass`/`PostMessage` 对已销毁的对象会抛
  `pywintypes.error`，释放路径必须自己兜住（`IsWindow` 不够，句柄值仍可能已经失效）。

`TrayError` 的稳定码只有 `tray_window_failed`（类注册/窗口创建/消息循环失败）、
`tray_icon_missing`（图标文件缺失或加载不出来）、`tray_icon_failed`（加进通知区域失败）。
`run()` 把窗口层的一切失败都归一成这三种码：`Controller` 只捕获
`(PlatformError, TrayError, OSError)`，而 `pywintypes.error` **不是** `OSError` 的子类，
逸出的原始异常会直接结束进程，让「托盘建不起来仍继续运行」的降级路径落空。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

import pywintypes
import win32api
import win32con
import win32gui

from raricy_bot.logging_setup import get_logger, log_event

from .. import texts, tray_model
from ..tray_model import TrayView
from . import TrayError

# 窗口类名固定：同名类重复注册会失败，所以它同时保证「一个进程一份托盘窗口」。
WINDOW_CLASS_NAME: str = "RaricyBotLight.TrayWindow"

# 通知区域里本图标的 id；一个进程只有一个图标。
NOTIFY_ID: int = 1

# uCallbackMessage 与「状态更新投递」消息：都取 WM_APP 区段，避开系统消息。
WM_TRAY_CALLBACK: int = win32con.WM_APP + 1
WM_TRAY_PRESENT: int = win32con.WM_APP + 2

# NOTIFYICON_VERSION_4：左键单击/双击合并成 NIN_SELECT（v4 不再单独送 WM_LBUTTONDBLCLK），
# 菜单坐标随 wParam 传，事件码在 lParam 的低 16 位、图标 id 在高 16 位。
NOTIFY_VERSION: int = 4

# pywin32 没有导出这两个（按 shellapi.h 本地定义）。
NIN_SELECT: int = win32con.WM_USER + 0
NIN_KEYSELECT: int = win32con.WM_USER + 1

# Explorer 重建任务栏时广播的消息名。消息 id 是会话内的动态值，只在 `run()` 里
# 用 `RegisterWindowMessage` 取（同一个字符串总是返回同一个 id），不写成模块常量。
TASKBAR_CREATED_NAME: str = "TaskbarCreated"

# 图标名 → 文件名：与 `tray_model.ICON_*` 一一对应，三个文件随包发行（§60）。
ICON_FILES: dict[str, str] = {
    "normal": "tray-normal.ico",
    "stopped": "tray-stopped.ico",
    "attention": "tray-attention.ico",
}

# 还没有视图时的占位图标：中性（不声称在线），第一帧到达后立刻被覆盖。
_PLACEHOLDER_ICON: str = tray_model.ICON_STOPPED

_LOG_CALLBACK_FAILED: str = "launcher.tray_callback_failed"
_LOG_READD_FAILED: str = "launcher.tray_icon_readd_failed"


def _signed_word(word: int) -> int:
    """把 16 位无符号字按有符号解释（`GET_X_LPARAM`/`GET_Y_LPARAM` 的语义）。"""
    return word - 0x10000 if word & 0x8000 else word


def _get_x_lparam(value: int) -> int:
    """`GET_X_LPARAM`：低 16 位；多显示器在左侧时菜单坐标可能是负数。"""
    return _signed_word(value & 0xFFFF)


def _get_y_lparam(value: int) -> int:
    """`GET_Y_LPARAM`：高 16 位，同样按有符号解释。"""
    return _signed_word((value >> 16) & 0xFFFF)


def _stable_error_code(exc: BaseException) -> str:
    """把异常折成稳定类别码：win32 层只报类别，不拼系统原文。"""
    return "win32_error" if isinstance(exc, pywintypes.error) else type(exc).__name__


class WinTrayIcon:
    """通知区域图标的 Windows 实现（`platform.TrayIcon` 协议，契约见 §61.4）。

    线程归属：

    - `run()` 必须在调用线程里建窗口并跑消息循环，**窗口因此属于那个线程**；
      `_owner_thread` 记下它，真正的释放（`NIM_DELETE`/`DestroyIcon`/`DestroyWindow`/
      `UnregisterClass`）只在 `run()` 的 `finally` 里、仍在那个线程上执行。
    - `present()` / `request_close()` / `close()` 任意线程可调：只改锁保护的状态再
      投消息，窗口未创建或已销毁时不留异常。**窗口尚未创建时两者都不丢请求**：
      首帧视图先存下来、在 `NIM_ADD` 之后套用；关闭请求先记下来、在窗口建好之后、
      进消息循环之前兑现（`PostMessage` 到还不存在的窗口会把它丢掉）。
    - 图标句柄与窗口句柄只在拥有窗口的线程上读写（`run()` 建、`finally` 释放）。
    """

    def __init__(self, *, icon_dir: Path, on_message: Callable[[str], None]) -> None:
        self._icon_dir = Path(icon_dir)
        self._on_message = on_message
        self._logger = get_logger("launcher.tray")

        # 只在拥有窗口的线程上读写的窗口状态。
        self._icons: dict[str, int] = {}
        self._window_class = None
        self._hinstance = 0
        self._icon_added = False
        self._taskbar_created = 0

        # 跨线程共享的状态：一律在 `self._lock` 下读写。
        self._lock = threading.Lock()
        self._view: TrayView | None = None
        self._hwnd = 0
        self._owner_thread: int | None = None
        self._close_requested = False

    # --- 协议：run / present / request_close / close -----------------------

    def run(self) -> None:
        """在调用线程创建窗口与图标并跑消息循环；失败抛 `TrayError`（稳定码）。"""
        self._owner_thread = threading.get_ident()
        hwnd = 0
        try:
            self._hinstance = win32api.GetModuleHandle(None)
            self._taskbar_created = win32gui.RegisterWindowMessage(TASKBAR_CREATED_NAME)
            self._register_class()
            hwnd = self._create_window()
            with self._lock:
                self._hwnd = hwnd
            self._load_icons()
            self._add_icon(hwnd)
            # 首帧早于窗口：协调器通常在 `run()` 之前就 `present()` 了，这里把它套上。
            self._safe_render()
            with self._lock:
                pending_close = self._close_requested
            if pending_close:
                # 退出请求早于窗口：`request_quit()` 可能先调了 `request_close()`，
                # 而那时窗口还不存在、`PostMessage` 会丢掉这条请求，随后消息循环
                # 无人叫醒。窗口建好之后、进循环之前兑现它。
                self._close_window(hwnd)
                return
            self._message_loop()
        except TrayError:
            raise
        except Exception as exc:
            # 归一到稳定码：`pywintypes.error` 不是 `OSError` 的子类，逸出 `run()`
            # 会让 `Controller` 的降级捕获元组接不住，直接结束进程。
            raise TrayError("tray_window_failed") from exc
        finally:
            self._teardown(hwnd)

    def present(self, view: TrayView) -> None:
        """呈现视图；线程安全，窗口未创建或已销毁时只存下视图（不是丢弃）。

        窗口线程收到 `WM_TRAY_PRESENT` 后才 `NIM_MODIFY`：`present()` 自己绝不碰
        窗口与图标句柄，任何线程都可以调。
        """
        with self._lock:
            self._view = view
            hwnd = self._hwnd
        if not hwnd:
            return
        self._post(hwnd, WM_TRAY_PRESENT)

    def request_close(self) -> None:
        """请求关闭消息循环；线程安全。

        窗口已创建时投递 `WM_CLOSE`；尚未创建时**记下请求**，由 `run()` 在建好窗口
        之后兑现（「窗口还没准备好」不能成为丢退出请求的理由）。已销毁时是空操作。
        """
        with self._lock:
            self._close_requested = True
            hwnd = self._hwnd
        self._post(hwnd, win32con.WM_CLOSE)

    def close(self) -> None:
        """幂等释放；真正的释放在拥有窗口的线程上（`run()` 的 `finally`）完成。

        非拥有线程调用等价于 `request_close()`（登记意图 + 投递一次 `WM_CLOSE`），
        绝不跨线程 `DestroyWindow`；拥有线程调用时窗口要么还没建、要么已经由 `run()`
        释放，因此这里只登记意图。
        """
        with self._lock:
            self._close_requested = True
            hwnd = self._hwnd
        if threading.get_ident() == self._owner_thread:
            return
        self._post(hwnd, win32con.WM_CLOSE)

    # --- 窗口与图标（只在拥有窗口的线程上）--------------------------------

    def _register_class(self) -> None:
        """注册窗口类；`pywin32` 只有 `RegisterClass`，没有 `RegisterClassEx`。"""
        window_class = win32gui.WNDCLASS()
        window_class.hInstance = self._hinstance
        window_class.lpszClassName = WINDOW_CLASS_NAME
        window_class.lpfnWndProc = self._wnd_proc
        window_class.style = win32con.CS_HREDRAW | win32con.CS_VREDRAW
        try:
            win32gui.RegisterClass(window_class)
        except pywintypes.error as exc:
            raise TrayError("tray_window_failed") from exc
        # 窗口存活期间回调与 WNDCLASS 必须保持可达：被 GC 收走等于把窗口过程变野指针。
        self._window_class = window_class

    def _create_window(self) -> int:
        """创建**隐藏的顶层窗口**：`WS_OVERLAPPED`、不带 `WS_VISIBLE`、`parent = 0`。

        不能改成 `HWND_MESSAGE` 消息窗口：`TaskbarCreated`、`WM_POWERBROADCAST`、
        `WM_QUERYENDSESSION`/`WM_ENDSESSION` 都只广播给顶层窗口。
        """
        try:
            hwnd = win32gui.CreateWindowEx(
                0,
                WINDOW_CLASS_NAME,
                texts.APP_NAME,
                win32con.WS_OVERLAPPED,
                0,
                0,
                0,
                0,
                0,
                0,
                self._hinstance,
                None,
            )
        except pywintypes.error as exc:
            raise TrayError("tray_window_failed") from exc
        if not hwnd:
            raise TrayError("tray_window_failed")
        return hwnd

    def _load_icons(self) -> None:
        """加载三个图标：`LR_LOADFROMFILE` + 系统小图标尺寸；失败抛 `tray_icon_missing`。"""
        size_x = win32api.GetSystemMetrics(win32con.SM_CXSMICON)
        size_y = win32api.GetSystemMetrics(win32con.SM_CYSMICON)
        loaded: dict[str, int] = {}
        try:
            for name, filename in ICON_FILES.items():
                handle = win32gui.LoadImage(
                    0,
                    str(self._icon_dir / filename),
                    win32con.IMAGE_ICON,
                    size_x,
                    size_y,
                    win32con.LR_LOADFROMFILE,
                )
                if not handle:
                    raise TrayError("tray_icon_missing")
                loaded[name] = handle
        except TrayError:
            self._destroy_icons(loaded)
            raise
        except (pywintypes.error, OSError) as exc:
            # 文件缺失时 `LoadImage` 抛错（不是返回 0）；已加载的先还回去。
            self._destroy_icons(loaded)
            raise TrayError("tray_icon_missing") from exc
        self._icons = loaded

    @staticmethod
    def _destroy_icons(icons: dict[str, int]) -> None:
        """销毁一批图标句柄；句柄可能已被系统回收，逐个兜住。"""
        for handle in icons.values():
            try:
                win32gui.DestroyIcon(handle)
            except pywintypes.error:
                pass

    def _add_icon(self, hwnd: int) -> None:
        """加图标（`NIM_ADD`）并声明 v4 协议（`NIM_SETVERSION`）；失败抛稳定码。"""
        icon, tip = self._icon_and_tip()
        flags = win32gui.NIF_MESSAGE | win32gui.NIF_ICON | win32gui.NIF_TIP
        try:
            win32gui.Shell_NotifyIcon(
                win32gui.NIM_ADD, (hwnd, NOTIFY_ID, flags, WM_TRAY_CALLBACK, icon, tip)
            )
            # 加成功就标记：之后（含失败路径）的释放都要把它摘掉，避免幽灵图标。
            self._icon_added = True
            # `NIM_SETVERSION` 的 uFlags 被系统忽略，照原样带上同一份标志；
            # 第 7 位是 szInfo（留空串），第 8 位是 uTimeout/uVersion 联合槽。
            win32gui.Shell_NotifyIcon(
                win32gui.NIM_SETVERSION,
                (hwnd, NOTIFY_ID, flags, WM_TRAY_CALLBACK, icon, tip, "", NOTIFY_VERSION),
            )
        except pywintypes.error as exc:
            raise TrayError("tray_icon_failed") from exc

    def _remove_icon(self, hwnd: int) -> None:
        """从通知区域摘掉图标；幂等（没加过是空操作），失败不抛出。"""
        if not self._icon_added:
            return
        self._icon_added = False
        if not hwnd:
            return
        try:
            win32gui.Shell_NotifyIcon(
                win32gui.NIM_DELETE, (hwnd, NOTIFY_ID, 0, 0, 0, "")
            )
        except pywintypes.error:
            pass

    def _icon_and_tip(self) -> tuple[int, str]:
        """当前视图对应的 `(HICON, tooltip)`；还没有视图时给中性占位。

        视图可能由任意线程写，`self._icons` 只在拥有窗口的线程上写，所以只需要
        在锁里取视图（本函数也只应在拥有窗口的线程上调用）。
        """
        with self._lock:
            view = self._view
        if view is None:
            return self._icons.get(_PLACEHOLDER_ICON, 0), ""
        icon = self._icons.get(view.icon)
        if icon is None:
            # 词表外的图标名一律按占位处理：窗口层不认识别的字符串。
            icon = self._icons.get(_PLACEHOLDER_ICON, 0)
        return icon, view.tooltip[: tray_model.TOOLTIP_MAX_CHARS]

    def _render_now(self) -> None:
        """把最新视图套到图标上（`NIM_MODIFY`）；只在拥有窗口的线程上调用。"""
        hwnd = self._hwnd
        if not hwnd or not self._icon_added:
            return
        icon, tip = self._icon_and_tip()
        win32gui.Shell_NotifyIcon(
            win32gui.NIM_MODIFY,
            (hwnd, NOTIFY_ID, win32gui.NIF_ICON | win32gui.NIF_TIP, WM_TRAY_CALLBACK, icon, tip),
        )

    def _safe_render(self) -> None:
        """渲染兜底：失败只记类别码，绝不抛回消息循环或 `run()`。"""
        try:
            self._render_now()
        except Exception as exc:
            self._log_callback_failure(exc)

    # --- 关闭与释放 -------------------------------------------------------

    def _destroy_window(self, hwnd: int) -> None:
        """摘图标、清句柄、销毁窗口；每步幂等，绝不抛出。"""
        self._remove_icon(hwnd)
        with self._lock:
            if self._hwnd == hwnd:
                self._hwnd = 0
        if hwnd and win32gui.IsWindow(hwnd):
            try:
                win32gui.DestroyWindow(hwnd)
            except pywintypes.error:
                pass

    def _close_window(self, hwnd: int) -> None:
        """关闭路径：销毁窗口并 `PostQuitMessage` 结束消息循环。"""
        self._destroy_window(hwnd)
        win32gui.PostQuitMessage(0)

    def _teardown(self, hwnd: int) -> None:
        """`run()` 的 `finally`：仍在拥有窗口的线程上释放图标与窗口。

        WM_CLOSE 路径可能已经摘过图标、销毁过窗口，`run()` 也可能在建到一半时失败，
        所以每一步都要能重复执行、都能对着「还没有」的状态跑。
        """
        self._destroy_window(hwnd)
        self._destroy_icons(self._icons)
        self._icons = {}
        self._window_class = None
        try:
            win32gui.UnregisterClass(WINDOW_CLASS_NAME, self._hinstance)
        except pywintypes.error:
            pass

    # --- 消息循环与回调 ---------------------------------------------------

    def _message_loop(self) -> None:
        """取线程消息（`hwnd=None`，同时取窗口消息与线程消息）直到 `WM_QUIT`。

        本机 pywin32 312 实测：返回**列表** `[ret, (hwnd, msg, wParam, lParam, time, pt)]`，
        `WM_QUIT` 时是 `[0, (...)]`；`TranslateMessage`/`DispatchMessage` 收的是内层那个
        消息元组。旧版 pywin32 在 `WM_QUIT` 时直接返回整数 0，同样退出；连形状都认不出来
        时也退出 —— 宁可结束托盘也不能在无人叫醒的循环里死等。
        """
        while True:
            result = win32gui.GetMessage(None, 0, 0)
            if result == 0:
                return
            if not isinstance(result, (list, tuple)) or result[0] == 0:
                return
            win32gui.TranslateMessage(result[1])
            win32gui.DispatchMessage(result[1])

    def _wnd_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        """窗口回调：只映射词表、渲染、返回；异常绝不抛回消息循环。"""
        try:
            return self._dispatch(hwnd, msg, wparam, lparam)
        except Exception as exc:
            self._log_callback_failure(exc)
            return 0

    def _dispatch(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        """窗口回调的实际分派；只做映射与渲染，没有任何 I/O 与等待。"""
        if msg == WM_TRAY_PRESENT:
            self._safe_render()
            return 0
        if msg == win32con.WM_CLOSE:
            self._close_window(hwnd)
            return 0
        if msg == WM_TRAY_CALLBACK:
            self._handle_tray_callback(hwnd, wparam, lparam)
            return 0
        if self._taskbar_created and msg == self._taskbar_created:
            self._readd_icon(hwnd)
            return 0
        if msg == win32con.WM_QUERYENDSESSION:
            # MSDN：返回 TRUE 表示同意结束会话。**立即同意**，不弹窗、不阻塞、
            # 不试图取消；协调器只重画一次（关机随时可能被取消，不能声明正在退出）。
            self._submit(tray_model.TRAY_EVENT_SESSION_QUERY)
            return True
        if msg == win32con.WM_ENDSESSION:
            # MSDN：wParam == 0 表示「关机被取消」，此时什么都不做；返回值系统忽略。
            if wparam != 0:
                self._submit(tray_model.TRAY_EVENT_SESSION_END)
            return 0
        if msg == win32con.WM_POWERBROADCAST:
            if wparam == win32con.PBT_APMSUSPEND:
                self._submit(tray_model.TRAY_EVENT_POWER_SUSPEND)
            elif wparam in (
                win32con.PBT_APMRESUMESUSPEND,
                win32con.PBT_APMRESUMEAUTOMATIC,
            ):
                self._submit(tray_model.TRAY_EVENT_POWER_RESUME)
            # 其余电源广播（如 PBT_APMQUERYSUSPEND、PBT_POWERSETTINGCHANGE）忽略。
            return True
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _handle_tray_callback(self, hwnd: int, wparam: int, lparam: int) -> None:
        """通知区域回调（v4）：按键事件在 `lParam` 低 16 位，坐标在 `wParam`。"""
        event = lparam & 0xFFFF
        if event in (NIN_SELECT, NIN_KEYSELECT):
            # v4 不再单独送 WM_LBUTTONDBLCLK：左键单击与双击合并成 NIN_SELECT
            # （键盘选中是 NIN_KEYSELECT），与设计 §7.1「双击图标执行同一动作」等效。
            self._submit(tray_model.TRAY_COMMAND_OPEN_ADMIN)
            return
        if event == win32con.WM_CONTEXTMENU:
            self._show_menu(hwnd, wparam)
            return
        # 其余（含 NIN_BALLOON*）一律忽略：N3 不做气泡通知，也不认识别的字符串。

    def _show_menu(self, hwnd: int, wparam: int) -> None:
        """右键菜单：现场按当前视图构建，选中项用**同一份**视图映射回命令。

        `TrackPopupMenu` 会一直阻塞到菜单收起（或系统取消），期间协调器照常渲染，
        `self._view` 可能已经换了一版 —— 所以先把视图拷成局部变量：菜单项 id 与命令的
        对应关系必须来自弹出时那一份，不能用新视图去解释旧菜单的返回值。
        """
        with self._lock:
            view = self._view
        if view is None:
            return
        commands: dict[int, str] = {}
        menu = win32gui.CreatePopupMenu()
        try:
            for index, item in enumerate(view.menu, start=1):
                if item.separator_before:
                    # 分隔符是**独立**的一次 `AppendMenu`：Win32 的 `MF_SEPARATOR` 只画一条
                    # 横线，`lpNewItem` 与 `uIDNewItem` 都被忽略。写成 `MF_STRING |
                    # MF_SEPARATOR` 就会把「带命令的项」变成不可选中的空线 —— 账号行文案消失，
                    # `start`/`open_diagnostics`/`quit` 从托盘不可达（`TrackPopupMenu` 永远
                    # 拿不到它们的 id）。`separator_before` 是「这一项前面加一条分隔线」，
                    # 不是「这一项是分隔符」。
                    win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
                flags = win32con.MF_STRING
                if not item.enabled:
                    flags |= win32con.MF_GRAYED
                win32gui.AppendMenu(menu, flags, index, item.label)
                commands[index] = item.command
            # MSDN：弹菜单前把窗口设为前台、收尾补一条 WM_NULL，否则菜单在点击别处时
            # 可能不消失。两者都是尽力而为，失败不影响菜单本身。
            try:
                win32gui.SetForegroundWindow(hwnd)
            except pywintypes.error:
                pass
            selected = win32gui.TrackPopupMenu(
                win32con.TPM_RETURNCMD | win32con.TPM_RIGHTBUTTON | win32con.TPM_NONOTIFY,
                _get_x_lparam(wparam),
                _get_y_lparam(wparam),
                0,
                hwnd,
                menu,
            )
        finally:
            try:
                win32gui.DestroyMenu(menu)
            except pywintypes.error:
                pass
            self._post(hwnd, win32con.WM_NULL)
        # 0 = 用户取消；空串 = 展示行（账号行/状态行），都不是命令。
        command = commands.get(selected, "")
        if command:
            self._submit(command)

    def _readd_icon(self, hwnd: int) -> None:
        """Explorer 重建任务栏后重加图标（`TaskbarCreated`）。

        菜单每次弹出都现场构建，不需要重建；重加失败只记
        `launcher.tray_icon_readd_failed`（error 是稳定码），不崩、不重启任何东西。
        """
        try:
            self._remove_icon(hwnd)
            self._add_icon(hwnd)
        except TrayError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                _LOG_READD_FAILED,
                status="failed",
                error=str(exc),
            )
            return
        self._submit(tray_model.TRAY_EVENT_TASKBAR_CREATED)

    # --- 小工具 -----------------------------------------------------------

    def _submit(self, message: str) -> None:
        """把词表里的字符串交给协调器；`on_message` 只入队，不做 I/O（§7.2）。"""
        self._on_message(message)

    def _post(self, hwnd: int, msg: int) -> None:
        """投递一条消息；窗口已销毁（句柄失效）时静默忽略。"""
        if not hwnd:
            return
        try:
            win32gui.PostMessage(hwnd, msg, 0, 0)
        except pywintypes.error:
            pass

    def _log_callback_failure(self, exc: BaseException) -> None:
        log_event(
            self._logger,
            logging.WARNING,
            _LOG_CALLBACK_FAILED,
            status="failed",
            error=_stable_error_code(exc),
        )
