"""托盘右键弹窗：复用托盘线程的消息循环绘制四项深色操作面板。"""

from __future__ import annotations

from collections.abc import Callable

import pywintypes
import win32api
import win32con
import win32gui

from .. import texts
from ..tray_model import TrayView


POPUP_CLASS_NAME = "RaricyBotLight.TrayPopup"

_BACKGROUND = (27, 31, 39)
_HOVER = (49, 57, 68)
_LINE = (64, 73, 84)
_TEXT = (235, 243, 251)
_MUTED = (161, 181, 201)
_DISABLED = (112, 129, 145)
_ACCENT = (99, 184, 246)


class TrayPopup:
    """同线程拥有一个可复用的圆角顶层窗口；动作只投递给协调器。"""

    def __init__(
        self,
        *,
        on_command: Callable[[str], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        self._on_command = on_command
        self._on_error = on_error
        self._hinstance = win32api.GetModuleHandle(None)
        self._scale = max(1.0, min(2.0, win32api.GetSystemMetrics(win32con.SM_CXSMICON) / 16))
        self._width = self._px(190)
        self._header_height = self._px(43)
        self._row_height = self._px(29)
        self._exit_gap = self._px(7)
        self._height = self._row_top(3) + self._row_height + self._px(7)
        self._hwnd = 0
        self._window_class = None
        self._visible = False
        self._view: TrayView | None = None
        self._hover = -1
        self._fonts: list[int] = []

    def _px(self, value: int) -> int:
        return round(value * self._scale)

    def _row_top(self, index: int) -> int:
        return (
            self._header_height
            + self._row_height * index
            + (self._exit_gap if index == 3 else 0)
        )

    def show(
        self, owner: int, view: TrayView, x: int, y: int,
        icon_rect: tuple[int, int, int, int] | None = None,
    ) -> None:
        """贴着图标可用的一侧弹出；取不到图标矩形时才用点击位置。"""
        if self._visible:
            self.hide()
            return
        self._view = view
        self._hover = -1
        try:
            if not self._hwnd:
                self._create_window(owner)
            left, top = self._position(x, y, icon_rect)
            win32gui.SetWindowPos(
                self._hwnd,
                win32con.HWND_TOPMOST,
                left,
                top,
                self._width,
                self._height,
                win32con.SWP_SHOWWINDOW,
            )
            self._visible = True
            win32gui.InvalidateRect(self._hwnd, None, True)
            try:
                win32gui.SetForegroundWindow(self._hwnd)
            except pywintypes.error:
                # 无交互桌面的测试进程没有前台权限；窗口仍可见并能被点击。
                pass
        except Exception:
            self.close()
            raise

    def hide(self) -> None:
        """隐藏但保留窗口，下一次右键可直接重用。"""
        self._visible = False
        self._hover = -1
        if self._hwnd:
            try:
                win32gui.ShowWindow(self._hwnd, win32con.SW_HIDE)
            except pywintypes.error:
                pass

    def close(self) -> None:
        """仅在托盘拥有线程上调用，释放窗口类与字体句柄。"""
        self.hide()
        hwnd, self._hwnd = self._hwnd, 0
        if hwnd:
            try:
                win32gui.DestroyWindow(hwnd)
            except pywintypes.error:
                pass
        if self._window_class is not None:
            try:
                win32gui.UnregisterClass(POPUP_CLASS_NAME, self._hinstance)
            except pywintypes.error:
                pass
            self._window_class = None
        for font in self._fonts:
            try:
                win32gui.DeleteObject(font)
            except pywintypes.error:
                pass
        self._fonts.clear()

    def _create_window(self, owner: int) -> None:
        window_class = win32gui.WNDCLASS()
        window_class.hInstance = self._hinstance
        window_class.lpszClassName = POPUP_CLASS_NAME
        window_class.lpfnWndProc = self._wnd_proc
        window_class.style = getattr(win32con, "CS_DROPSHADOW", 0)
        win32gui.RegisterClass(window_class)
        self._window_class = window_class
        self._hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_TOPMOST,
            POPUP_CLASS_NAME,
            texts.APP_NAME,
            win32con.WS_POPUP,
            0,
            0,
            self._width,
            self._height,
            owner,
            0,
            self._hinstance,
            None,
        )
        if not self._hwnd:
            raise RuntimeError("tray_popup_window_missing")
        region = win32gui.CreateRoundRectRgn(
            0, 0, self._width + 1, self._height + 1, self._px(12), self._px(12)
        )
        try:
            # pywin32 成功时返回 None，区域所有权已交给窗口，不能再 DeleteObject。
            win32gui.SetWindowRgn(self._hwnd, region, True)
        except Exception:
            win32gui.DeleteObject(region)
            raise
        for size, weight in ((12, 600), (10, 400), (12, 400)):
            self._fonts.append(self._font(size, weight))

    def _font(self, size: int, weight: int) -> int:
        font = win32gui.LOGFONT()
        font.lfHeight = -self._px(size)
        font.lfWeight = weight
        font.lfFaceName = "Microsoft YaHei UI"
        return win32gui.CreateFontIndirect(font)

    def _position(
        self, x: int, y: int,
        icon_rect: tuple[int, int, int, int] | None = None,
    ) -> tuple[int, int]:
        if icon_rect is None or icon_rect[2] <= icon_rect[0] or icon_rect[3] <= icon_rect[1]:
            icon_rect = (x, y, x + 1, y + 1)
        icon_left, icon_top, icon_right, icon_bottom = icon_rect
        icon_x = (icon_left + icon_right) // 2
        icon_y = (icon_top + icon_bottom) // 2
        monitor = win32api.MonitorFromPoint(
            (icon_x, icon_y), win32con.MONITOR_DEFAULTTONEAREST
        )
        left, top, right, bottom = win32api.GetMonitorInfo(monitor)["Work"]
        gap = self._px(7)
        left_side = icon_left - self._width - gap
        right_side = icon_right + gap
        candidates = (
            (left_side, right_side) if icon_x >= (left + right) // 2
            else (right_side, left_side)
        )
        popup_left = next(
            (candidate for candidate in candidates
             if left <= candidate and candidate + self._width <= right),
            candidates[0],
        )
        popup_left = min(max(popup_left, left), max(left, right - self._width))
        popup_top = icon_y - self._height // 2
        popup_top = min(max(popup_top, top), max(top, bottom - self._height))
        return popup_left, popup_top

    def _row_at(self, x: int, y: int) -> int:
        if not self._px(6) <= x < self._width - self._px(6):
            return -1
        for index in range(4):
            top = self._row_top(index)
            if top <= y < top + self._row_height:
                return index
        return -1

    def _activate(self, index: int) -> None:
        view = self._view
        if view is None or not 0 <= index < len(view.menu) or not view.menu[index].enabled:
            return
        command = view.menu[index].command
        self.hide()
        self._on_command(command)

    def _move_selection(self, direction: int) -> None:
        view = self._view
        if view is None:
            return
        available = [index for index, item in enumerate(view.menu) if item.enabled]
        if not available:
            return
        if self._hover not in available:
            self._hover = available[0 if direction > 0 else -1]
        else:
            position = available.index(self._hover)
            self._hover = available[(position + direction) % len(available)]
        win32gui.InvalidateRect(self._hwnd, None, False)

    def _wnd_proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        try:
            return self._dispatch(hwnd, msg, wparam, lparam)
        except Exception as exc:
            self._on_error(exc)
            return 0

    def _dispatch(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == win32con.WM_PAINT:
            self._paint(hwnd)
            return 0
        if msg == win32con.WM_ERASEBKGND:
            return 1
        if msg == win32con.WM_MOUSEMOVE:
            index = self._row_at(lparam & 0xFFFF, (lparam >> 16) & 0xFFFF)
            view = self._view
            if view is not None and 0 <= index < len(view.menu) and not view.menu[index].enabled:
                index = -1
            if index != self._hover:
                self._hover = index
                win32gui.InvalidateRect(hwnd, None, False)
            return 0
        if msg == win32con.WM_LBUTTONUP:
            self._activate(self._row_at(lparam & 0xFFFF, (lparam >> 16) & 0xFFFF))
            return 0
        if msg == win32con.WM_KEYDOWN:
            if wparam == win32con.VK_ESCAPE:
                self.hide()
            elif wparam == win32con.VK_DOWN:
                self._move_selection(1)
            elif wparam == win32con.VK_UP:
                self._move_selection(-1)
            elif wparam in (win32con.VK_RETURN, win32con.VK_SPACE):
                self._activate(self._hover)
            return 0
        if msg == win32con.WM_ACTIVATE and (wparam & 0xFFFF) == win32con.WA_INACTIVE:
            self.hide()
            return 0
        if msg == win32con.WM_CLOSE:
            self.hide()
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _paint(self, hwnd: int) -> None:
        hdc, paint = win32gui.BeginPaint(hwnd)
        try:
            self._draw(hdc)
        finally:
            win32gui.EndPaint(hwnd, paint)

    def _draw(self, hdc: int) -> None:
        self._fill(hdc, (0, 0, self._width, self._height), _BACKGROUND)
        view = self._view
        if view is None or len(self._fonts) != 3:
            return
        self._draw_text(
            hdc, texts.APP_NAME, self._fonts[0], _TEXT,
            (self._px(15), self._px(5), self._width - self._px(12), self._px(25)),
        )
        self._draw_text(
            hdc, view.status_label, self._fonts[1], _MUTED,
            (self._px(15), self._px(23), self._width - self._px(12), self._px(41)),
        )
        self._fill(
            hdc,
            (
                self._px(12),
                self._header_height - 1,
                self._width - self._px(12),
                self._header_height,
            ),
            _LINE,
        )
        self._fill(
            hdc,
            (self._px(12), self._row_top(3) - self._px(4),
             self._width - self._px(12), self._row_top(3) - self._px(3)),
            _LINE,
        )
        for index, item in enumerate(view.menu):
            top = self._row_top(index)
            if index == self._hover and item.enabled:
                self._fill(
                    hdc,
                    (self._px(6), top, self._width - self._px(6), top + self._row_height),
                    _HOVER,
                )
                self._fill(
                    hdc,
                    (
                        self._px(6),
                        top + self._px(5),
                        self._px(8),
                        top + self._row_height - self._px(5),
                    ),
                    _ACCENT,
                )
            self._draw_text(
                hdc, item.label, self._fonts[2], _TEXT if item.enabled else _DISABLED,
                (self._px(15), top, self._width - self._px(12), top + self._row_height),
            )

    @staticmethod
    def _fill(hdc: int, rect: tuple[int, int, int, int], color: tuple[int, int, int]) -> None:
        brush = win32gui.CreateSolidBrush(win32api.RGB(*color))
        try:
            win32gui.FillRect(hdc, rect, brush)
        finally:
            win32gui.DeleteObject(brush)

    @staticmethod
    def _draw_text(
        hdc: int,
        value: str,
        font: int,
        color: tuple[int, int, int],
        rect: tuple[int, int, int, int],
    ) -> None:
        original = win32gui.SelectObject(hdc, font)
        try:
            win32gui.SetBkMode(hdc, win32con.TRANSPARENT)
            win32gui.SetTextColor(hdc, win32api.RGB(*color))
            win32gui.DrawText(
                hdc, value, -1, rect,
                win32con.DT_LEFT | win32con.DT_VCENTER | win32con.DT_SINGLELINE |
                win32con.DT_END_ELLIPSIS | win32con.DT_NOPREFIX,
            )
        finally:
            win32gui.SelectObject(hdc, original)
