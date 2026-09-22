"""冻结产物的冒烟：启动 → 激活 → 会话 → 状态 → 退出（LIGHT_EDITION_DESIGN §17.2 可自动化部分）。

它不连站点、不建机器人：只证明发行目录里的程序能自己跑起来、控制面可用、退出干净。
干净 Windows 验收（无 Python/Node/Docker 的独立机器）仍需人工执行，见
[docs/usage/LIGHT.md](../docs/usage/LIGHT.md) §7。

注意：激活通道与单实例互斥体都按**当前用户**命名，因此冒烟运行时本机不能同时
有另一个 Light 实例（那会让冒烟拿到别的实例的端口并失败 —— 是安全失败，不是误报）。

用法::

    python tools/build_light.py --pyinstaller
    python tools/smoke_light.py --app-dir build/light-staging/dist/RaricyBotLight
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from raricy_launcher import activation  # noqa: E402
from raricy_launcher.platform import get_platform  # noqa: E402

# 冒烟预算：冻结程序冷启动 + 控制服务就绪 + 退出回收。
START_TIMEOUT_SECONDS = 60.0
EXIT_TIMEOUT_SECONDS = 30.0


class SmokeError(Exception):
    """冒烟失败；消息是可直接照做的说明。"""


def _child_env(data_root: Path) -> dict[str, str]:
    """给子进程一个干净环境：数据根指向临时目录，浏览器不动真格。

    刻意**不**传 PYTHONPATH：冻结程序必须自带运行时，不能借用开发环境。
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key in ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "COMSPEC")
    }
    env["LOCALAPPDATA"] = str(data_root.parent)
    # 首次启动会「打开管理页」：把浏览器换成一个只记录地址的批处理，冒烟不弹窗，
    # 也避免真实浏览器把 profile 写进临时数据根（那会让清理失败）。
    recorder = data_root.parent / "browser.cmd"
    recorder.parent.mkdir(parents=True, exist_ok=True)
    recorder.write_text(
        '@echo off\r\necho %1 >> "%~dp0opened.txt"\r\n', encoding="utf-8"
    )
    env["BROWSER"] = str(recorder)
    return env


def _wait_for_port(data_root: Path, timeout: float) -> int:
    metadata = data_root / "runtime" / "launcher-runtime.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            payload = json.loads(metadata.read_text(encoding="utf-8"))
            port = int(payload["port"])
            if port > 0:
                return port
        except (OSError, ValueError, KeyError):
            time.sleep(0.2)
    raise SmokeError("控制服务没有在预算内发布端口（看 diagnostics/launcher.log）")


def _activate(port: int) -> str:
    """走激活通道取一次带引导令牌的入口地址（等价于第二次双击）。"""
    platform = get_platform()
    data = platform.request_activation(
        activation.encode_request("open_admin"), timeout_ms=10_000
    )
    response = activation.decode_response(data)
    if not response.get("ok") or not isinstance(response.get("url"), str):
        raise SmokeError("激活被拒绝")
    if not response["url"].startswith(f"http://127.0.0.1:{port}/"):
        raise SmokeError(f"激活返回了意外地址：{response['url']}")
    return response["url"]


def _session_and_status(url: str):
    """用引导令牌换会话，再读一次状态与页面；返回 (CSRF 值, 会话 Cookie)。"""
    import httpx

    base = url.split("#", 1)[0].rstrip("/")
    token = url.split("#token=", 1)[1]
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        exchanged = client.post(
            f"{base}/api/session/exchange",
            json={"token": token},
            headers={"Origin": base, "Content-Type": "application/json"},
        )
        if exchanged.status_code != 200:
            raise SmokeError(f"会话兑换失败：{exchanged.status_code}")
        csrf = exchanged.json()["csrf"]
        client.cookies.update(exchanged.cookies)
        cookies = dict(client.cookies)

        status = client.get(f"{base}/api/status")
        if status.status_code != 200:
            raise SmokeError(f"状态接口不可用：{status.status_code}")
        state = status.json()["status"]["process"]["state"]
        if state not in {"stopped", "failed"}:
            raise SmokeError(f"未配置的实例不该在跑机器人：{state}")

        page = client.get(f"{base}/")
        if page.status_code != 200 or "RARICY_LIGHT" not in page.text:
            raise SmokeError("管理页没有正常返回")
        # 构建产物必须真的取得到：缺 bundle 的发行包在浏览器里只会白屏（复审 F10）。
        asset = re.search(r'src="(/assets/[^"]+\.js)"', page.text)
        if asset is None:
            raise SmokeError("管理页没有引用构建产物")
        if client.get(f"{base}{asset.group(1)}").status_code != 200:
            raise SmokeError("构建产物取不到")
        # 凭据后端必须真的可用：冻结包里缺 keyring 时保存凭据会永远 503（复审 F3）。
        config = client.get(f"{base}/api/config").json()
        backend = (config.get("credentials") or {}).get("backend") or {}
        if not backend:
            raise SmokeError(f"配置接口没有返回凭据状态：{config}")
        if backend.get("name") != "keyring" or not backend.get("available"):
            # 会话内存模式是运行的降级形态，但发行包必须带上系统凭据库：
            # 否则用户每次启动都要重填密码（复审 N-3）。
            raise SmokeError(f"发行包没有可用的系统凭据后端：{backend}")
    return csrf, cookies


def _quit(url: str, csrf: str, cookies: dict[str, str]) -> None:
    import httpx

    base = url.split("#", 1)[0].rstrip("/")
    with httpx.Client(timeout=10, cookies=cookies) as client:
        response = client.post(
            f"{base}/api/launcher/quit",
            json={},
            headers={
                "Origin": base,
                "Content-Type": "application/json",
                "X-Raricy-CSRF": csrf,
            },
        )
        if response.status_code != 200:
            raise SmokeError(f"退出请求被拒绝：{response.status_code}")


def run(app_dir: Path) -> int:
    exe = app_dir / "RaricyBotLight.exe"
    if not exe.is_file():
        raise SmokeError(f"没有找到 {exe}；先跑 tools/build_light.py --pyinstaller")

    with tempfile.TemporaryDirectory(prefix="light-smoke-", ignore_cleanup_errors=True) as tmp:
        data_root = Path(tmp) / "RaricyBotLight"
        process = subprocess.Popen(
            [str(exe)],
            env=_child_env(data_root),
            cwd=str(app_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            port = _wait_for_port(data_root, START_TIMEOUT_SECONDS)
            print(f"[ok] 控制服务已监听 127.0.0.1:{port}")
            url = _activate(port)
            print("[ok] 激活返回带一次性令牌的入口地址")
            csrf, cookies = _session_and_status(url)
            print("[ok] 会话兑换、状态接口与管理页都可用")
            _quit(url, csrf, cookies)
            deadline = time.monotonic() + EXIT_TIMEOUT_SECONDS
            while time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.2)
            if process.poll() is None:
                raise SmokeError("退出请求后进程仍未结束")
            if process.returncode != 0:
                raise SmokeError(f"退出码不是 0：{process.returncode}")
            print("[ok] 退出请求让进程干净结束（退出码 0）")
            if (data_root / "runtime" / "launcher-runtime.json").exists():
                raise SmokeError("退出后运行元数据没有清理")
            print("[ok] 运行元数据已清理，互斥体已释放")
            opened = data_root.parent / "opened.txt"
            if opened.exists():
                print(f"[ok] 首次启动按要求打开了管理页：{opened.read_text(encoding='utf-8').strip()[:60]}…")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        return run(args.app_dir)
    except SmokeError as exc:
        print(f"[fail] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
