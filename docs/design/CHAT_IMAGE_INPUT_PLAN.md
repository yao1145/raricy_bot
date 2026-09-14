# 聊天图片输入实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让机器人在 `model.vision_enabled` 为真时，把用户消息里附带的那张图取回内存、编码为 base64 data URL，随当前轮交给模型。

**Architecture:** 取图与编码收在新的 `core/vision.py`；站点侧只加一个受同源与字节上限约束的 `SiteClient.fetch_image`。图片只进当前轮——`ContextManager` 一行不改，历史里留下 `[图片]` / `[图片未提供]` 文本标记。默认关闭，关闭时进程里没有任何新增请求。

**Tech Stack:** Python 3.12+ / asyncio / httpx（含 `httpx.MockTransport` 测试）/ pytest + pytest-asyncio（`asyncio_mode = "auto"`）/ 标准库 `base64`、`urllib.parse`。

**设计依据:** `docs/design/CHAT_IMAGE_INPUT_DESIGN.md`（本计划的唯一权威来源；冲突以它为准）。

## Global Constraints

- 运行期依赖仅有 `httpx`、`openai`、`PyYAML`、`aiohttp`。**本计划不新增任何依赖**（base64 与 urllib.parse 都是标准库）。
- 注释与 docstring 用中文，标识符用英文，**任何地方不得出现 emoji**。
- 用户内容只放在 `role="user"` 的消息里，绝不拼进 system prompt。
- HTTP 客户端绝不设置 `Origin` / `Referer`；成功判据只看信封 `code == 200`。
- 日志一律走 `logging_setup.log_event()`，只输出 `LOG_FIELDS` 白名单字段。**任何日志与 SQLite 都不得出现**：Cookie、密码、API Key、消息正文、模型请求体、**图片 URL、图片字节或 base64 片段**。
- 测试**不得发起真实连接**：站点层一律 `httpx.MockTransport(transport=...)`。
- `filterwarnings = ["error"]`：测试输出必须干净，依赖产生的任何 warning 都算失败。
- 测试命令从仓库根执行，`pyproject.toml` 已设 `pythonpath=["src"]`，不需要安装。
- **本仓库的 `.gitignore` 排除了 `tests/`、`docs/archive/`、`CLAUDE.md`。** 因此每个任务的提交步骤**只提交 `src/` 与 `docs/` 下的文件**，测试文件留在工作区不提交（这是既有仓库设置，不是遗漏）。

---

### Task 1: 配置项 `model.vision_enabled` 与 `model.max_image_bytes`

**Files:**
- Modify: `src/raricy_bot/config.py`（`ModelConfig` 定义在 42-50 行；`load_config` 的 model 段在 187-193 行；辅助函数在 289-330 行附近）
- Modify: `config.example.yaml`（`model:` 段）
- Test: `tests/test_config.py`（追加用例；该文件已有 `MINIMAL_YAML`、`full_yaml()`、`write_config`、`secrets_env` 可复用）

**Interfaces:**
- Consumes: 无（本任务是第一个）。
- Produces:
  - `ModelConfig.vision_enabled: bool`（默认 `False`）、`ModelConfig.max_image_bytes: int`（默认 `5242880`）
  - 模块常量 `config.MAX_IMAGE_BYTES: int = 10485760`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_config.py` 末尾。注意：`model:` 段里新增的键必须写在 `model:` 段内，
而既有 `MINIMAL_YAML` 以 `system_prompt:` 结尾、`FULL_YAML` 同样如此，两者都无法在
`model:` 段内插行——因此本任务自建一份带占位的模板：

```python
# --- 图片输入（chat image input）---

# extra 追加到 model 段末尾，每行需自带两空格缩进。
VISION_YAML = """\
site:
  base_url: "https://example.com"
model:
  base_url: "https://provider.example/v1"
  model: "configured-model-name"
{extra}system_prompt: "{prompt}"
"""


def vision_yaml(extra: str = "") -> str:
    return VISION_YAML.format(extra=extra, prompt=PROMPT)


def test_vision_defaults_to_disabled(write_config, secrets_env) -> None:
    """默认关闭：既有部署升级后行为不变。"""
    cfg = load_config(write_config(vision_yaml()), secrets_env)

    assert cfg.model.vision_enabled is False
    assert cfg.model.max_image_bytes == 5242880


def test_vision_fields_are_parsed(write_config, secrets_env) -> None:
    text = vision_yaml("  vision_enabled: true\n  max_image_bytes: 1048576\n")

    cfg = load_config(write_config(text), secrets_env)

    assert cfg.model.vision_enabled is True
    assert cfg.model.max_image_bytes == 1048576


def test_vision_enabled_must_be_a_bool(write_config, secrets_env) -> None:
    """YAML 里写成字符串 'true' 要被拒绝，不能悄悄当成真。"""
    with pytest.raises(ConfigError, match="vision_enabled"):
        load_config(write_config(vision_yaml('  vision_enabled: "true"\n')), secrets_env)


@pytest.mark.parametrize("value", [0, -1, 10485761])
def test_max_image_bytes_out_of_range(write_config, secrets_env, value: int) -> None:
    """必须 >= 1，且不得超过站点图床的 10 MiB 硬上限。"""
    text = vision_yaml(f"  max_image_bytes: {value}\n")

    with pytest.raises(ConfigError, match="max_image_bytes"):
        load_config(write_config(text), secrets_env)


def test_max_image_bytes_rejects_bool(write_config, secrets_env) -> None:
    with pytest.raises(ConfigError, match="max_image_bytes"):
        load_config(write_config(vision_yaml("  max_image_bytes: true\n")), secrets_env)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_config.py -q -k "vision or max_image"
```

Expected: FAIL —— `AttributeError: 'ModelConfig' object has no attribute 'vision_enabled'`，以及 `ConfigError` 未抛出的用例失败。

- [ ] **Step 3: 实现配置项**

`src/raricy_bot/config.py`：在 `ModelConfig` 上加两个字段（**必须带默认值**，`tests/test_app.py:make_config` 等现有调用不带它们）：

```python
@dataclass(frozen=True)
class ModelConfig:
    """模型服务配置。"""

    base_url: str
    model: str
    temperature: float = 0.4
    timeout_seconds: float = 45.0
    max_output_tokens: int = 600
    # 图片输入：默认关闭。配的模型未必支持视觉，开启而模型不支持时每一轮带图的消息
    # 都会以 400 失败并回一条失败提示；默认关闭让既有部署升级后行为逐字节不变。
    vision_enabled: bool = False
    # 单张图的下载期硬上限（字节）。默认 5 MiB：base64 后约 6.7 MiB，
    # 在 concurrency=3 时峰值可控。
    max_image_bytes: int = 5 * 1024 * 1024
```

文件顶部常量区（紧邻 `MAX_CONFIG_BYTES`）加：

```python
# 站点图床的单图硬上限（10 MiB）。来源：raricy.com src/lib/image-upload.ts 的
# MAX_IMAGE_SIZE。配得比它更大的话站点根本不会给出那么大的图，只会掩盖意图。
MAX_IMAGE_BYTES: int = 10 * 1024 * 1024
```

`load_config` 里 `ModelConfig(...)` 增加两个参数：

```python
        max_output_tokens=_positive_int(model_raw, "max_output_tokens", "model", 600),
        vision_enabled=_bool_flag(model_raw, "vision_enabled", "model", False),
        max_image_bytes=_max_image_bytes(model_raw),
```

新增两个辅助函数（放在 `_temperature` 旁边）：

```python
def _bool_flag(
    container: Mapping[str, Any], key: str, where: str, default: bool
) -> bool:
    """取布尔开关；缺失用默认值，非布尔（含 "true" 这类字符串）一律报错。"""
    value = container.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"配置 {where}.{key} 必须是 true 或 false")
    return value


def _max_image_bytes(container: Mapping[str, Any]) -> int:
    """单图字节上限：正整数且不超过站点图床的 10 MiB 硬上限。"""
    value = _positive_int(
        container, "max_image_bytes", "model", 5 * 1024 * 1024
    )
    if value > MAX_IMAGE_BYTES:
        raise ConfigError(
            f"配置 model.max_image_bytes 不得超过 {MAX_IMAGE_BYTES}"
        )
    return value
```

`config.example.yaml` 的 `model:` 段追加：

```yaml
  # 图片输入默认关闭。仅当本模型确实支持视觉时才打开：否则每一条带图的消息
  # 都会以 400 失败并回一条失败提示。
  vision_enabled: false
  # 单张图的下载期硬上限（字节），不得超过站点图床的 10 MiB 上限。
  max_image_bytes: 5242880
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_config.py -q
```

Expected: PASS（全文件通过，含既有用例）。

- [ ] **Step 5: 提交**

```bash
git add src/raricy_bot/config.py config.example.yaml
git commit -m "feat(config): add opt-in model.vision_enabled and max_image_bytes"
```

---

### Task 2: `text_utils.has_image`

**Files:**
- Modify: `src/raricy_bot/text_utils.py`（文件末尾，`has_media` 之后，149-151 行）
- Test: `tests/test_text_utils.py`（`test_has_media_without_media` 附近，285-299 行）

**Interfaces:**
- Consumes: 无。
- Produces: `has_image(message: Any) -> bool` —— 与 `has_media` 的区别：它回答的是「这轮能不能把图交给模型」，因此 `image_missing` 为真时返回 False；`has_media` 回答的是「有没有我读不了的东西」，保持原义不动。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_text_utils.py`（`from raricy_bot.text_utils import (...)` 里补上 `has_image`）：

```python
# --- 可读图片判定 ---


def test_has_image_requires_present_and_not_missing() -> None:
    """只有图存在、且没有被标记为已失效时才算「有图可读」。"""
    assert has_image(SimpleNamespace(image=object(), image_missing=False)) is True
    assert has_image(SimpleNamespace(image=object(), image_missing=True)) is False
    assert has_image(SimpleNamespace(image=None, image_missing=False)) is False
    assert has_image(SimpleNamespace(image=None, image_missing=True)) is False
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_text_utils.py -q -k has_image
```

Expected: FAIL —— `ImportError: cannot import name 'has_image'`。

- [ ] **Step 3: 实现**

`src/raricy_bot/text_utils.py` 末尾追加：

```python
def has_image(message: Any) -> bool:
    """判断这条消息是否带了一张**可以读**的图。

    与 `has_media` 的区别是 `image_missing`：那个字段表示「引用了图但图已不存在」，
    这种消息没有东西可以交给模型，因此不算有图。`has_media` 回答的是另一个问题
    （「有没有我读不了的东西」），保持原义不动。
    """
    return message.image is not None and not message.image_missing
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_text_utils.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/raricy_bot/text_utils.py
git commit -m "feat(text_utils): add has_image predicate for vision eligibility"
```

---

### Task 3: `SiteClient.fetch_image` 与 `ImageFetchError`

**Files:**
- Modify: `src/raricy_bot/site/client.py`（`SiteError` 之后加异常类，约 84 行；`_same_origin` 等辅助放在 `_stream_headers` 之后；`fetch_image` 放在 `open_stream` 之前）
- Test: `tests/test_client.py`（复用 `SiteStub` / `Harness` / `failing_route` / `assert_no_cors_headers`）

**Interfaces:**
- Consumes: `SiteClient._base_url`、`_cookie_headers()`、`_require_client()`（均已存在）。
- Produces:
  - `class ImageFetchError(Exception)`，构造签名 `ImageFetchError(reason: str, status: int = 0)`，属性 `.reason: str`、`.status: int`；`reason ∈ {"host_not_allowed", "too_large", "http", "network"}`。
  - `async def fetch_image(self, url: str, *, max_bytes: int) -> bytes`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_client.py`：

```python
# --- 图片取回（chat image input）---

IMAGE_PATH = "/api/images/img_1/raw"
# 最小合法 PNG 头，够 sniff_image_mime 认出格式。
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def image_route(data: bytes = PNG_BYTES, *, status: int = 200) -> Responder:
    """直接返回原始图片字节的响应。"""

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=data, headers={"Content-Type": "image/png"})

    return respond


async def test_fetch_image_resolves_relative_url_and_sends_cookie() -> None:
    """站点给的是相对路径，取图要拼成同源绝对地址并带上会话 Cookie。"""
    stub = SiteStub()
    stub.route("GET", LOGIN_PATH, login_route())
    stub.route("GET", IMAGE_PATH, image_route())

    async with Harness(stub) as h:
        await h.client.login()
        data = await h.client.fetch_image(IMAGE_PATH, max_bytes=1024)

        assert data == PNG_BYTES
        request = stub.requests[-1]
        assert str(request.url) == f"{BASE_URL}{IMAGE_PATH}"
        assert request.headers["Cookie"] == f"raricy_session={SESSION_VALUE}"
        assert request.headers["Accept"] == "image/*"
        assert_no_cors_headers(stub.requests)


async def test_fetch_image_rejects_cross_origin_without_sending_anything() -> None:
    """跨源地址直接拒绝，且**一个请求都不发**（Cookie 不能外泄到别的 host）。"""
    stub = SiteStub()

    async with Harness(stub) as h:
        with pytest.raises(ImageFetchError) as excinfo:
            await h.client.fetch_image("https://evil.example/img/1.png", max_bytes=1024)

    assert excinfo.value.reason == "host_not_allowed"
    assert stub.requests == []


async def test_fetch_image_rejects_other_port() -> None:
    """同 host 不同端口不算同源。"""
    stub = SiteStub()

    async with Harness(stub) as h:
        with pytest.raises(ImageFetchError) as excinfo:
            await h.client.fetch_image(f"https://site.example:8443{IMAGE_PATH}", max_bytes=1024)

    assert excinfo.value.reason == "host_not_allowed"
    assert stub.requests == []


async def test_fetch_image_stops_reading_at_the_byte_limit() -> None:
    """超过上限立即放弃，不把整个响应读完。"""
    produced: list[int] = []

    async def chunks():
        for index in range(4):
            produced.append(index)
            yield b"x" * 512

    def responder(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunks())

    stub = SiteStub()
    stub.route("GET", IMAGE_PATH, responder)

    async with Harness(stub) as h:
        with pytest.raises(ImageFetchError) as excinfo:
            await h.client.fetch_image(IMAGE_PATH, max_bytes=700)

    assert excinfo.value.reason == "too_large"
    # 第 0、1 块之后已经超过 700 字节，第 2 块不应该被产出。
    assert 2 not in produced


async def test_fetch_image_missing_returns_http_reason() -> None:
    """私有图对非作者返回 404（站点伪装成不存在）：归为 http，调用方据此降级。"""
    stub = SiteStub()
    stub.route("GET", IMAGE_PATH, json_route(404, {"code": 404, "message": "Not Found"}))

    async with Harness(stub) as h:
        with pytest.raises(ImageFetchError) as excinfo:
            await h.client.fetch_image(IMAGE_PATH, max_bytes=1024)

    assert (excinfo.value.reason, excinfo.value.status) == ("http", 404)


async def test_fetch_image_network_error() -> None:
    stub = SiteStub()
    stub.route("GET", IMAGE_PATH, failing_route(httpx.ConnectError("boom")))

    async with Harness(stub) as h:
        with pytest.raises(ImageFetchError) as excinfo:
            await h.client.fetch_image(IMAGE_PATH, max_bytes=1024)

    assert excinfo.value.reason == "network"


async def test_fetch_image_empty_body_is_an_http_failure() -> None:
    stub = SiteStub()
    stub.route("GET", IMAGE_PATH, image_route(b""))

    async with Harness(stub) as h:
        with pytest.raises(ImageFetchError) as excinfo:
            await h.client.fetch_image(IMAGE_PATH, max_bytes=1024)

    assert (excinfo.value.reason, excinfo.value.status) == ("http", 200)
```

同时把文件顶部导入改成：

```python
from raricy_bot.site.client import (
    STREAM_READ_TIMEOUT_SECONDS,
    ImageFetchError,
    SiteClient,
    SiteError,
)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_client.py -q -k fetch_image
```

Expected: FAIL —— `ImportError: cannot import name 'ImageFetchError'`。

- [ ] **Step 3: 实现**

`src/raricy_bot/site/client.py`：在 `class SiteError` 之后加：

```python
class ImageFetchError(Exception):
    """图片取回失败。

    `reason` 是稳定短标识，供调用方判定降级路径与写日志用：
    - `host_not_allowed`：目标与站点不同源（**未发出任何请求**）
    - `too_large`：字节数超过调用方给定的上限
    - `http`：站点返回非 200（`status` 为 HTTP 状态码；空响应体也算）
    - `network`：传输层错误或超时
    """

    def __init__(self, reason: str, status: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
```

在 `open_stream` 之前加：

```python
    async def fetch_image(self, url: str, *, max_bytes: int) -> bytes:
        """取回一条消息附带的图片原始字节（INTERFACES §7）。

        只允许与 `base_url` **完全同源**（scheme + host + 有效端口）的地址。站点给的是
        相对路径 `/api/images/<id>/raw`，用 `urljoin` 解析；一旦允许「照站点给的 url
        带着 Cookie 去 GET」，任何能让站点返回任意 url 的路径都会变成凭据外泄通道。
        跨源直接拒绝，**不发出任何请求**。

        字节上限在**流式累计过程中**执行：超限立即放弃，不读完整个响应。
        本方法**不写任何日志**（URL 不得进日志），失败原因由调用方以稳定字段记录。
        401 不重新登录、不重试：取图失败只是一次降级，不值得为它多一次登录。
        """
        base = urllib.parse.urlsplit(self._base_url)
        target = urllib.parse.urljoin(self._base_url + "/", url)
        parsed = urllib.parse.urlsplit(target)
        if not self._same_origin(base, parsed):
            raise ImageFetchError("host_not_allowed")

        headers: dict[str, str] = {"Accept": "image/*"}
        headers.update(self._cookie_headers())
        chunks: list[bytes] = []
        total = 0
        try:
            async with self._require_client().stream("GET", target, headers=headers) as response:
                if response.status_code != 200:
                    raise ImageFetchError("http", int(response.status_code))
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ImageFetchError("too_large")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise ImageFetchError("network") from exc

        data = b"".join(chunks)
        if not data:
            raise ImageFetchError("http", 200)
        return data

    @staticmethod
    def _same_origin(
        base: urllib.parse.SplitResult, target: urllib.parse.SplitResult
    ) -> bool:
        """两者是否同源。非 http/https 或端口非法一律判为不同源。"""
        if target.scheme not in ("http", "https"):
            return False
        if target.scheme != base.scheme or target.hostname != base.hostname:
            return False
        return _effective_port(target) == _effective_port(base)
```

文件末尾加模块级辅助：

```python
def _effective_port(parsed: urllib.parse.SplitResult) -> int | None:
    """解析有效端口；非法端口（`:abc`、越界值）返回 None，从而判为不同源。"""
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is not None:
        return port
    if parsed.scheme == "https":
        return 443
    if parsed.scheme == "http":
        return 80
    return None
```

文件顶部导入加 `import urllib.parse`。

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_client.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/raricy_bot/site/client.py
git commit -m "feat(client): add same-origin bounded image fetch"
```

---

### Task 4: `core/vision.py` 的格式嗅探、data URL 与消息改写

**Files:**
- Create: `src/raricy_bot/core/vision.py`
- Test: `tests/test_vision.py`（新建）

**Interfaces:**
- Consumes: 无。
- Produces:
  - `IMAGE_MIME_ALLOWLIST: tuple[str, ...]`
  - `sniff_image_mime(data: bytes) -> str | None`
  - `build_data_url(data: bytes, mime: str) -> str`
  - `build_image_part(data_url: str) -> dict[str, Any]`
  - `attach_image(messages: list[dict[str, Any]], part: dict[str, Any]) -> None`

- [ ] **Step 1: 写失败的测试**

新建 `tests/test_vision.py`：

```python
"""图片输入的最低层：格式嗅探、data URL 编码与消息改写。

这些函数必须是纯函数：不发请求、不写日志、不落库。
"""

from __future__ import annotations

import base64

import pytest

from raricy_bot.core.vision import (
    IMAGE_MIME_ALLOWLIST,
    attach_image,
    build_data_url,
    build_image_part,
    sniff_image_mime,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 8
GIF = b"GIF89a" + b"\x00" * 8
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (GIF, "image/gif"),
        (WEBP, "image/webp"),
    ],
)
def test_sniff_recognizes_allowed_formats(data: bytes, expected: str) -> None:
    assert sniff_image_mime(data) == expected


@pytest.mark.parametrize(
    "data",
    [
        SVG,
        b"",
        b"\x89PNG",  # 截断的 PNG 头
        b"RIFF\x00\x00\x00\x00AVI ",  # RIFF 但不是 WEBP
        b"not an image at all",
    ],
)
def test_sniff_rejects_everything_else(data: bytes) -> None:
    assert sniff_image_mime(data) is None


def test_svg_is_not_in_the_allowlist() -> None:
    """图床上传白名单含 SVG，我们**不转发**它：唯一带脚本能力的格式。"""
    assert "image/svg+xml" not in IMAGE_MIME_ALLOWLIST


def test_build_data_url_encodes_base64() -> None:
    data = b"\x00\x01\x02"
    url = build_data_url(data, "image/png")

    assert url == f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"


def test_build_image_part_shape() -> None:
    assert build_image_part("data:image/png;base64,AAAA") == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAAA"},
    }


def test_attach_image_turns_text_content_into_parts() -> None:
    messages = [
        {"role": "system", "content": "系统"},
        {"role": "user", "content": "这是什么"},
    ]
    part = build_image_part("data:image/png;base64,AAAA")

    attach_image(messages, part)

    assert messages[1] == {
        "role": "user",
        "content": [{"type": "text", "text": "这是什么"}, part],
    }
    # system 消息绝不被改动。
    assert messages[0] == {"role": "system", "content": "系统"}


def test_attach_image_appends_when_content_is_already_parts() -> None:
    existing = {"type": "text", "text": "已有"}
    messages = [{"role": "user", "content": [existing]}]
    part = build_image_part("data:image/png;base64,AAAA")

    attach_image(messages, part)

    assert messages[0]["content"] == [existing, part]


def test_attach_image_targets_the_last_user_message() -> None:
    """历史里可能有多条 user 消息，只改最后一条（当前轮）。"""
    messages = [
        {"role": "user", "content": "旧"},
        {"role": "assistant", "content": "答"},
        {"role": "user", "content": "新"},
    ]
    attach_image(messages, build_image_part("data:image/png;base64,AAAA"))

    assert messages[0] == {"role": "user", "content": "旧"}
    assert isinstance(messages[2]["content"], list)


def test_attach_image_without_user_message_is_a_noop() -> None:
    messages = [{"role": "system", "content": "系统"}]

    attach_image(messages, build_image_part("data:image/png;base64,AAAA"))

    assert messages == [{"role": "system", "content": "系统"}]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_vision.py -q
```

Expected: FAIL —— `ModuleNotFoundError: No module named 'raricy_bot.core.vision'`。

- [ ] **Step 3: 实现**

新建 `src/raricy_bot/core/vision.py`：

```python
"""图片输入：取回、格式判定与编码（INTERFACES §20）。

三件事：从站点取图（`ImageLoader`）、按**字节**判定格式（不信任 DTO 里的 mime_type）、
编码成 data URL。图片字节只存在于内存：不落 SQLite、不写日志、不写文件、不进历史。

不转发 SVG：站点图床的上传白名单里有 image/svg+xml，但它是白名单里唯一带脚本能力的
格式，而模型对 SVG 的 data URL 也没有有效理解。
"""

from __future__ import annotations

import base64
from typing import Any

# 允许转交给模型的图片格式。判定一律以字节嗅探为准，DTO 的 mime_type 只作参考。
IMAGE_MIME_ALLOWLIST: tuple[str, ...] = (
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
)

_PNG_SIGNATURE: bytes = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE: bytes = b"\xff\xd8\xff"
_GIF_SIGNATURE: bytes = b"GIF8"


def sniff_image_mime(data: bytes) -> str | None:
    """按 magic bytes 判定图片格式；不在白名单里的返回 None。

    只认这四种：PNG / JPEG / GIF / WEBP。SVG（XML 文本）与其它一律 None。
    """
    if data.startswith(_PNG_SIGNATURE):
        return "image/png"
    if data.startswith(_JPEG_SIGNATURE):
        return "image/jpeg"
    if data.startswith(_GIF_SIGNATURE):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def build_data_url(data: bytes, mime: str) -> str:
    """把图片字节编码成 base64 data URL。"""
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def build_image_part(data_url: str) -> dict[str, Any]:
    """构造 OpenAI 兼容的 image_url 内容块。"""
    return {"type": "image_url", "image_url": {"url": data_url}}


def attach_image(messages: list[dict[str, Any]], part: dict[str, Any]) -> None:
    """就地把图片块挂到**最后一条** user 消息上。

    文本内容（str）就地升级为内容块列表，已有列表则追加。找不到 user 消息时不做任何事
    —— 调用方（app 的 worker）保证最后一轮一定存在 user 消息。

    调用时机：必须在 `_apply_reply_prefix` **之后**，因为后者按 str 拼接 content。
    """
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") != "user":
            continue
        content = messages[index].get("content")
        if isinstance(content, list):
            content.append(part)
        else:
            messages[index] = {
                "role": "user",
                "content": [{"type": "text", "text": content if isinstance(content, str) else ""}, part],
            }
        return
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_vision.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/raricy_bot/core/vision.py
git commit -m "feat(vision): add mime sniffing, data URL encoding and message part attach"
```

---

### Task 5: `ImageLoader`

**Files:**
- Modify: `src/raricy_bot/core/vision.py`（追加）
- Test: `tests/test_vision.py`（追加）

**Interfaces:**
- Consumes: Task 3 的 `SiteClient.fetch_image` / `ImageFetchError`；Task 4 的 `sniff_image_mime` / `build_data_url` / `build_image_part`；`site.models.ChatMessage`。
- Produces: `class ImageLoader`，构造 `ImageLoader(client: SiteClient, *, max_bytes: int, logger: logging.Logger | None = None)`，方法 `async def load(self, message: ChatMessage) -> tuple[dict[str, Any] | None, str]`，第二个返回值 ∈ `{"ok", "none", "host_not_allowed", "too_large", "http", "network", "unsupported_type"}`。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_vision.py`：

```python
# --- ImageLoader ---


class FakeImageClient:
    """假站点客户端：按预设结果返回字节或抛 ImageFetchError。"""

    def __init__(self, *, data: bytes = PNG, error: ImageFetchError | None = None) -> None:
        self.data = data
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def fetch_image(self, url: str, *, max_bytes: int) -> bytes:
        self.calls.append((url, max_bytes))
        if self.error is not None:
            raise self.error
        return self.data


IMAGE_URL = "/api/images/img_1/raw"


def _image_ref():
    """ImageRef 的替身；只有 url 会被 fetch_image 用到。"""
    return SimpleNamespace(id="img_1", url=IMAGE_URL, mime_type="image/png")


def _message(image=..., image_missing: bool = False):
    """最小 ChatMessage；只用到 image 与 image_missing 两个字段。

    `image` 默认是**一张真图引用**；要构造「没有图」的消息必须显式传 `image=None`，
    否则 `load()` 会去访问 `image.url` 而炸掉。
    """
    if image is ...:
        image = _image_ref()
    return SimpleNamespace(image=image, image_missing=image_missing)


async def test_loader_returns_part_for_allowed_image() -> None:
    client = FakeImageClient(data=PNG)
    loader = ImageLoader(client, max_bytes=1024)

    part, reason = await loader.load(_message())

    assert reason == "ok"
    assert part is not None
    assert part["image_url"]["url"].startswith("data:image/png;base64,")
    assert client.calls == [(IMAGE_URL, 1024)]


async def test_loader_skips_messages_without_a_usable_image() -> None:
    client = FakeImageClient()

    for message in (_message(image=None), _message(image_missing=True)):
        part, reason = await ImageLoader(client, max_bytes=1024).load(message)
        assert (part, reason) == (None, "none")

    assert client.calls == []  # 没有可读的图就不该发请求


async def test_loader_reports_unsupported_type_without_leaking_bytes(caplog) -> None:
    """SVG 的字节不是白名单格式：降级并记日志，日志里不得出现图片内容。"""
    client = FakeImageClient(data=SVG)
    logger = get_logger("test.vision")

    with caplog.at_level(logging.INFO, logger="raricy.test.vision"):
        part, reason = await ImageLoader(client, max_bytes=1024, logger=logger).load(_message())

    assert (part, reason) == (None, "unsupported_type")
    assert SVG.decode() not in caplog.text


@pytest.mark.parametrize(
    "reason",
    ["host_not_allowed", "too_large", "http", "network"],
)
async def test_loader_downgrades_on_fetch_failure(reason: str, caplog) -> None:
    client = FakeImageClient(error=ImageFetchError(reason, 404 if reason == "http" else 0))
    logger = get_logger("test.vision")

    with caplog.at_level(logging.INFO, logger="raricy.test.vision"):
        part, got = await ImageLoader(client, max_bytes=1024, logger=logger).load(_message())

    assert (part, got) == (None, reason)
    assert f"reason={reason}" in caplog.text
    assert "url" not in caplog.text.lower()


async def test_loader_logs_limit_bytes_on_oversize(caplog) -> None:
    client = FakeImageClient(error=ImageFetchError("too_large"))
    logger = get_logger("test.vision")

    with caplog.at_level(logging.INFO, logger="raricy.test.vision"):
        await ImageLoader(client, max_bytes=2048, logger=logger).load(_message())

    assert "limit_bytes=2048" in caplog.text
```

导入区补上：

```python
import logging
from types import SimpleNamespace

from raricy_bot.core.vision import ImageLoader
from raricy_bot.logging_setup import get_logger
from raricy_bot.site.client import ImageFetchError
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_vision.py -q -k loader
```

Expected: FAIL —— `ImportError: cannot import name 'ImageLoader'`。

- [ ] **Step 3: 实现**

追加到 `src/raricy_bot/core/vision.py`（导入区补 `import logging`、`from ..logging_setup import get_logger, log_event`、`from ..site.client import ImageFetchError, SiteClient`、`from ..site.models import ChatMessage`，并在模块级加 `_logger = get_logger("vision")`）：

```python
class ImageLoader:
    """把一条消息里的图片取回并编码成模型可用的内容块。

    图片字节只在这里短暂存在：不进历史、不落库、不写日志。失败一律降级为
    `(None, reason)`，由调用方决定是转纯文本轮还是回一条本地提示。
    """

    def __init__(
        self,
        client: SiteClient,
        *,
        max_bytes: int,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._max_bytes = max_bytes
        self._logger = logger if logger is not None else _logger

    async def load(self, message: ChatMessage) -> tuple[dict[str, Any] | None, str]:
        """返回 `(image_part | None, reason)`；reason 见 INTERFACES §20。"""
        image = message.image
        if image is None or message.image_missing:
            return None, "none"

        try:
            data = await self._client.fetch_image(image.url, max_bytes=self._max_bytes)
        except ImageFetchError as exc:
            self._log_failure(exc.reason)
            return None, exc.reason

        mime = sniff_image_mime(data)
        if mime is None or mime not in IMAGE_MIME_ALLOWLIST:
            # 嗅探目前只会返回白名单里的四种，这个判断是策略的单一来源：
            # 将来嗅探支持更多格式时，仍由白名单决定「发给模型」这一侧放行什么。
            self._log_failure("unsupported_type", size_bytes=len(data))
            return None, "unsupported_type"

        return build_image_part(build_data_url(data, mime)), "ok"

    def _log_failure(self, reason: str, *, size_bytes: int | None = None) -> None:
        """记一条降级日志；字段只有 reason / size_bytes / limit_bytes，**不记 URL**。"""
        fields: dict[str, object] = {"reason": reason}
        if size_bytes is not None:
            fields["size_bytes"] = size_bytes
        if reason == "too_large":
            fields["limit_bytes"] = self._max_bytes
        log_event(self._logger, logging.INFO, "vision.image_unavailable", **fields)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_vision.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/raricy_bot/core/vision.py
git commit -m "feat(vision): add ImageLoader with bounded fetch and downgrade reasons"
```

---

### Task 6: 三个用户可见文案

**Files:**
- Modify: `src/raricy_bot/texts.py`
- Test: `tests/test_text_utils.py`（文案断言集中在 302-362 行）

**Interfaces:**
- Consumes: 无。
- Produces:
  - `HELP_TEXT`（视觉关闭）与 `HELP_TEXT_WITH_VISION`（视觉开启），二者共用同一份首尾文字
  - `IMAGE_UNAVAILABLE_TEXT`（新）
  - `UNSUPPORTED_MEDIA_TEXT`（改写：只用于「只有博客、没有可读图片」）

**文案分工（改动后必须成立）：**

| 常量 | 用在什么情形 |
|------|-------------|
| `IMAGE_UNAVAILABLE_TEXT` | 消息里**有图但读不到**：视觉未开启、`image_missing`、或取图失败 |
| `UNSUPPORTED_MEDIA_TEXT` | 消息里**只有博客**、没有可读的图 |
| `USAGE_HINT` | 既没有正文也没有任何媒体 |

- [ ] **Step 1: 写失败的测试**

改写 `tests/test_text_utils.py` 的文案断言部分（导入区补 `HELP_TEXT_WITH_VISION`、`IMAGE_UNAVAILABLE_TEXT`）：

```python
@pytest.mark.parametrize(
    ("text", "keyword"),
    [
        (HELP_TEXT, "机器人"),
        ...
        (HELP_TEXT, "重启"),
    ],
)
def test_help_text_discloses_required_points(text: str, keyword: str) -> None:
    assert keyword in text


@pytest.mark.parametrize("text", [HELP_TEXT, HELP_TEXT_WITH_VISION])
def test_vision_help_variant_discloses_the_same_points(text: str) -> None:
    """两份帮助文案必须逐项披露同一组事实，只有「能不能看图」这一句不同。"""
    for keyword in (
        "机器人",
        "不是真人",
        "不代表站方",
        "第三方模型",
        "联网",
        "图片",
        "博客",
        "长期",
        "/help",
        "/reset",
        "公开",
        "精确 @",
        "引用",
        "7 天",
        "重启",
    ):
        assert keyword in text, f"缺少 {keyword}"


@pytest.mark.parametrize("text", [HELP_TEXT, HELP_TEXT_WITH_VISION])
def test_help_text_fits_the_site_message_limit(text: str) -> None:
    """站点单条消息上限 1000 字：/help 必须整条塞得下，否则会被截断。"""
    assert len(text) <= 1000


def test_vision_help_text_says_images_are_sent_to_the_model() -> None:
    """开启视觉时，帮助文案必须点明图片同样会转交第三方模型。"""
    index = HELP_TEXT_WITH_VISION.index("图片")
    assert "第三方模型" in HELP_TEXT_WITH_VISION[index : index + 60]
    assert "不能查看图片" not in HELP_TEXT_WITH_VISION


def test_default_help_text_still_says_images_are_not_readable() -> None:
    assert "不能查看图片" in HELP_TEXT
```

并把非空单字符串的 `parametrize` 列表补上两个新常量：

```python
        UNSUPPORTED_MEDIA_TEXT,
        IMAGE_UNAVAILABLE_TEXT,
        HELP_TEXT_WITH_VISION,
```

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_text_utils.py -q -k "help or vision or text"
```

Expected: FAIL —— `ImportError: cannot import name 'HELP_TEXT_WITH_VISION'`。

- [ ] **Step 3: 实现**

`src/raricy_bot/texts.py`：把 `HELP_TEXT` 拆成三段并生成两个常量（**首尾只写一份**），
新增 `IMAGE_UNAVAILABLE_TEXT`，改写 `UNSUPPORTED_MEDIA_TEXT`：

```python
# /help 的三个组成部分：首尾是两份文案共用的，中间那句按是否开启图片输入二选一。
_HELP_HEAD: str = (
    "我是本站的聊天与博客评论机器人，不是真人，发言不代表站方立场。\n"
    "我能做的事：在大区里精确 @ 我、在博客评论里首次 @ 我，或直接回复我的评论；"
    "私聊里直接给我发消息，我也会回复你。\n"
    "请注意：你发送的消息可能会被转交给第三方模型服务处理。\n"
)

_HELP_CAPABILITY_TEXT_ONLY: str = (
    "我无法联网，不能查看图片、附件或被引用的博客内容；博客正文不超过 1000 字时，"
    "可能随当前轮次一并发送给第三方模型，超过 1000 字时不会提供正文。\n"
)

_HELP_CAPABILITY_TEXT_VISION: str = (
    "我无法联网，但可以查看你发来的图片：图片同样会转交给第三方模型处理。"
    "不能读附件或被引用的博客正文；博客正文不超过 1000 字时，"
    "可能随当前轮次一并发送给第三方模型，超过 1000 字时不会提供正文。\n"
)

_HELP_TAIL: str = (
    "关于大区：那里是公开的多人对话，只有精确 @ 我的消息会进来，别人的发言我看不见。"
    "想接着聊就回复（引用）我的消息，这样会留在同一段对话里；"
    "别人加入后，这段对话里最近的内容会再次发送给模型。"
    "新开的对话与旧的不相干，对话归属保留 7 天，之后回复旧消息等于开一段新的。\n"
    "我重启之后可能会忘记先前聊过什么，没有长期记忆。\n"
    "发送 /help 可以再次查看这份说明，发送 /reset 可以开始一段新对话（不删除旧的那段）。"
    "重启后短期上下文会丢失；每条评论回复都会真实通知被回复的人。"
)

# 视觉关闭时（默认）的完整说明。
HELP_TEXT: str = _HELP_HEAD + _HELP_CAPABILITY_TEXT_ONLY + _HELP_TAIL

# 视觉开启时的完整说明；只替换中间那句能力描述，其余逐字相同。
HELP_TEXT_WITH_VISION: str = _HELP_HEAD + _HELP_CAPABILITY_TEXT_VISION + _HELP_TAIL
```

```python
# 消息里有图但读不到时的提示：视觉未开启、图片已失效、或取图失败都走这一条。
# 三种原因的措辞合并在一句里，因此不能声称具体是哪一个原因。
IMAGE_UNAVAILABLE_TEXT: str = (
    "这张图片我没能读取：可能是图片已失效、格式不支持或体积过大，"
    "也可能是当前没有启用图片理解。你可以用文字描述一下想问的内容。"
)

# 消息里只有博客、没有可读图片时的提示。
UNSUPPORTED_MEDIA_TEXT: str = "我暂时不能查看博客内容。请把想问的内容用文字发给我。"
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_text_utils.py -q && python -c "
import sys; sys.path.insert(0,'src')
from raricy_bot import texts
print(len(texts.HELP_TEXT), len(texts.HELP_TEXT_WITH_VISION))"
```

Expected: PASS；两个长度都 <= 1000。

- [ ] **Step 5: 提交**

```bash
git add src/raricy_bot/texts.py
git commit -m "feat(texts): add vision-aware help and image-unavailable copy"
```

---

### Task 7: 路由器按视觉开关分流纯图消息

**Files:**
- Modify: `src/raricy_bot/core/router.py`（构造 91-111 行；`_ACTIONABLE_REASONS` 42-55 行；第 9.1 步 282-306 行；`HELP_TEXT` 用在 309-320 行）
- Test: `tests/test_router.py`（既有 `test_media_only_reply_now` 在 363-369 行）

**Interfaces:**
- Consumes: Task 2 的 `text_utils.has_image`；Task 6 的 `texts.IMAGE_UNAVAILABLE_TEXT` / `HELP_TEXT_WITH_VISION`。
- Produces: `MessageRouter(..., vision_enabled: bool = False)`；纯图消息在开启视觉时入队，`reason == "image_only"`。

- [ ] **Step 1: 写失败的测试**

先给文件里既有的两个辅助加参数（它们的真实签名见 `tests/test_router.py:56` 与 `:95`）：

```python
def make_message(
    message_id: int = 1,
    *,
    channel_id: str = LOBBY,
    content: str = "",
    author_id: str = "u_a",
    author_name: str = "甲",
    image: ImageRef | None = None,
    image_missing: bool = False,          # 新增
    blog: BlogRef | None = None,          # 新增（既有实现里是写死的 None）
    pat: PatRef | None = None,
    reply: ReplyRef | None = None,
    is_deleted: bool = False,
) -> ChatMessage:
    return ChatMessage(
        ...
        image_missing=image_missing,
        blog=blog,
        ...
    )


def build_router(
    store: Store,
    *,
    ctx: ContextManager | None = None,
    cfg: BehaviorConfig | None = None,
    storage: StorageConfig | None = None,
    now: Callable[[], float] = _default_now,
    queue_size: int = 50,
    vision_enabled: bool = False,         # 新增，透传给 MessageRouter
) -> tuple[MessageRouter, ContextManager, asyncio.Queue[Request]]:
```

注意 `build_router` 返回三元组，既有用例都写成 `router, _, queue = build_router(store)`。

再把 `test_media_only_reply_now`（363 行）改成下面第一个用例，并追加四个新用例：

```python
async def test_media_only_reply_now(store: Store) -> None:
    """有图无文字、视觉未开启 → media_only + IMAGE_UNAVAILABLE_TEXT，不入队。"""
    router, _, queue = build_router(store)
    message = make_message(40, channel_id=DM_CHANNEL, content="", image=image_ref())

    result = await router.handle_message(DM_CHANNEL, message, 40)

    assert (result.action, result.reason) == ("reply_now", "media_only")
    assert result.text == texts.IMAGE_UNAVAILABLE_TEXT
    assert queue.qsize() == 0


async def test_blog_only_reports_unsupported_media(store: Store) -> None:
    """纯博客仍然是「读不了」：文案改为只针对博客。"""
    router, _, queue = build_router(store)
    message = make_message(
        41,
        channel_id=DM_CHANNEL,
        content="",
        blog=BlogRef(id="b1", title="标题", description="", author=None, updated_at=""),
    )

    result = await router.handle_message(DM_CHANNEL, message, 41)

    assert (result.action, result.reason) == ("reply_now", "media_only")
    assert result.text == texts.UNSUPPORTED_MEDIA_TEXT
    assert queue.qsize() == 0


async def test_image_only_is_queued_when_vision_is_enabled(store: Store) -> None:
    """开启视觉后，纯图消息进模型（reason=image_only）。"""
    router, _, queue = build_router(store, vision_enabled=True)
    message = make_message(42, channel_id=DM_CHANNEL, content="", image=image_ref())

    result = await router.handle_message(DM_CHANNEL, message, 42)

    assert (result.action, result.reason) == ("queued", "image_only")
    assert result.request is not None
    assert result.request.user_text == ""
    assert queue.qsize() == 1


async def test_missing_image_is_not_queued_even_with_vision(store: Store) -> None:
    """image_missing 表示图已不存在：没有东西可以交给模型，仍走本地提示。"""
    router, _, queue = build_router(store, vision_enabled=True)
    message = make_message(
        43, channel_id=DM_CHANNEL, content="", image=image_ref(), image_missing=True
    )

    result = await router.handle_message(DM_CHANNEL, message, 43)

    assert (result.action, result.reason) == ("reply_now", "media_only")
    assert result.text == texts.IMAGE_UNAVAILABLE_TEXT
    assert queue.qsize() == 0


async def test_help_text_follows_the_vision_flag(store: Store) -> None:
    plain, _, _ = build_router(store)
    vision, _, _ = build_router(store, vision_enabled=True)

    plain_result = await plain.handle_message(
        DM_CHANNEL, make_message(44, channel_id=DM_CHANNEL, content="/help"), 44
    )
    vision_result = await vision.handle_message(
        DM_CHANNEL, make_message(45, channel_id=DM_CHANNEL, content="/help"), 45
    )

    assert plain_result.text == texts.HELP_TEXT
    assert vision_result.text == texts.HELP_TEXT_WITH_VISION
```

（`BlogRef` 需要补进本文件的导入区，它来自 `raricy_bot.site.models`。）

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_router.py -q -k "media_only or image_only or help_text_follows"
```

Expected: FAIL —— `TypeError: __init__() got an unexpected keyword argument 'vision_enabled'`。

- [ ] **Step 3: 实现**

`src/raricy_bot/core/router.py`：

1. 导入 `has_image`（加进 `from ..text_utils import (...)` 的字母序位置）。
2. `_ACTIONABLE_REASONS` 加 `"image_only",`（它会入队、会消耗一次回复配额）。
3. `__init__` 增加参数与字段：

```python
    def __init__(
        self,
        ...
        now: Callable[[], float] = time.time,
        vision_enabled: bool = False,
    ) -> None:
        ...
        self._vision_enabled = vision_enabled
```

4. 第 9.1 步改为：

```python
        # 9.1 空正文：能看图就交给模型，否则给本地提示。
        if not user_text:
            if self._vision_enabled and has_image(message):
                # 纯图消息入队（reason=image_only），继续走到第 10 步；
                # 单图时的取图与降级由 app 的 worker 负责（设计 §3.5）。
                pass
            elif message.image is not None:
                # 有图但读不到：视觉未开启，或 image_missing。
                return self._emit(
                    "reply_now",
                    "media_only",
                    channel_id=channel_id,
                    message_id=message.id,
                    reply_to=message.id,
                    text=texts.IMAGE_UNAVAILABLE_TEXT,
                    channel_kind=channel_kind,
                    thread_root_id=thread_root_id,
                    event_id=event_id,
                )
            elif message.blog is not None:
                return self._emit(
                    "reply_now",
                    "media_only",
                    channel_id=channel_id,
                    message_id=message.id,
                    reply_to=message.id,
                    text=texts.UNSUPPORTED_MEDIA_TEXT,
                    channel_kind=channel_kind,
                    thread_root_id=thread_root_id,
                    event_id=event_id,
                )
            else:
                return self._emit(
                    "reply_now",
                    "empty",
                    channel_id=channel_id,
                    message_id=message.id,
                    reply_to=message.id,
                    text=texts.USAGE_HINT,
                    channel_kind=channel_kind,
                    thread_root_id=thread_root_id,
                    event_id=event_id,
                )
```

注意：`pass` 之后会继续执行 9.2-9.6。当 `user_text == ""` 时
`is_help_command` / `is_reset_command` / `is_secret_probe` 全为 False、
`len(user_text) > max_input_chars` 也为 False，因此不会误命中；
把这三步的守卫条件**不要**改动，只在这里加一条注释说明「空正文不会命中下面几步」。

5. `/help` 的文案按开关选择：

```python
        if is_help_command(user_text):
            return self._emit(
                "reply_now",
                "help",
                channel_id=channel_id,
                message_id=message.id,
                reply_to=message.id,
                text=(
                    texts.HELP_TEXT_WITH_VISION
                    if self._vision_enabled
                    else texts.HELP_TEXT
                ),
                channel_kind=channel_kind,
                thread_root_id=thread_root_id,
                event_id=event_id,
            )
```

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_router.py -q && python -m pytest tests -q
```

Expected: PASS。第二次全量运行里若 `tests/test_comment_router.py::...media_only` 失败，
说明误改了 `comments/router.py` —— 那是评论侧，**本次不改**，请回退对它的任何改动。

- [ ] **Step 5: 提交**

```bash
git add src/raricy_bot/core/router.py
git commit -m "feat(router): queue image-only messages when vision is enabled"
```

---

### Task 8: app 里装配 `ImageLoader` 并在 worker 拼装图片

**Files:**
- Modify: `src/raricy_bot/app.py`（`__init__` 的 MessageRouter 构造 189-197 行；`_handle_request` 454-532 行；`_pending_turn` 534-543 行；导入区）
- Test: `tests/test_app.py`（`make_config` 在 75 行；`FakeModel` 在 166 行；`AppStub` 在 188 行）

**Interfaces:**
- Consumes: Task 3 `SiteClient.fetch_image`；Task 4 `attach_image`；Task 5 `ImageLoader`；Task 6 文案；Task 7 的 `image_only`。
- Produces: `BotApp._load_image(request) -> tuple[dict | None, str]`、`BotApp._pending_turn(request, image_state)`、`BotApp._send_image_unavailable(request)`。

- [ ] **Step 1: 写失败的测试**

先扩展本文件既有的四个辅助，**全部带默认值**，不动既有调用：

1. `make_config`（75 行）加两个关键字参数并透传给 `ModelConfig`：

```python
def make_config(
    tmp_path,
    behavior: BehaviorConfig | None = None,
    *,
    db_path: str | None = None,
    vision_enabled: bool = False,
    max_image_bytes: int = 4096,
) -> Config:
    ...
        model=ModelConfig(
            base_url="https://model.example/v1",
            model="test-model",
            vision_enabled=vision_enabled,
            max_image_bytes=max_image_bytes,
        ),
```

2. `make_app` fixture（311 行）的 `_make` 加参数：

```python
    async def _make(
        *,
        behavior: BehaviorConfig | None = None,
        model: FakeModel | None = None,
        vision_enabled: bool = False,
    ) -> AppEnv:
        config = make_config(
            tmp_path / f"run-{next(counter)}", behavior, vision_enabled=vision_enabled
        )
```

3. `message_dto`（97 行）与 `message_frame`（133 行）各加两个参数：

```python
def message_dto(
    message_id: int,
    *,
    channel_id: str,
    content: str,
    author_id: str = ALICE_ID,
    username: str = "alice",
    reply_id: int | None = None,
    reply_content: str = "被引用的消息",
    image_id: str | None = None,          # 新增
    image_missing: bool = False,          # 新增
) -> dict[str, Any]:
    ...
    return {
        ...
        "image": (
            {"id": image_id, "url": f"/api/images/{image_id}/raw", "mime_type": "image/png"}
            if image_id is not None
            else None
        ),
        "image_missing": image_missing,
        ...
    }


def message_frame(
    event_id: int | None,
    channel_id: str,
    message_id: int,
    content: str,
    *,
    author_id: str = ALICE_ID,
    username: str = "alice",
    reply_id: int | None = None,
    reply_content: str = "被引用的消息",
    image_id: str | None = None,          # 新增，透传给 message_dto
    image_missing: bool = False,          # 新增，透传给 message_dto
) -> str:
```

4. `AppStub.__init__`（182 行）加两个属性，并在 `_handle` 的最前面加图片分支：

```python
        self.image_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        self.image_status = 200
```

```python
        if request.url.path.startswith("/api/images/"):
            return httpx.Response(
                self.image_status,
                content=self.image_bytes,
                headers={"Content-Type": "image/png"},
            )
```

然后追加用例（沿用本文件既有的 `await make_app()`、`wait_until`、`message_frame`、
`lobby_thread_session_key`；`lobby_thread_session_key` 已在导入区）：

```python
# --- 图片输入（chat image input）---


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'


async def test_image_with_text_is_attached_as_content_parts(make_app) -> None:
    """有图有正文：最后一条 user 消息的 content 变成 [text, image_url] 内容块。"""
    env = await make_app(vision_enabled=True)
    await wait_until(lambda: env.app.ready)

    env.stub.feed(message_frame(11, LOBBY, 101, "@mybot 这是什么", image_id="img_1"))
    await wait_until(lambda: env.model.calls >= 1)

    last = env.model.inputs[0][-1]
    assert last["role"] == "user"
    assert last["content"][0]["text"].endswith("[图片]\n---\n这是什么")
    assert last["content"][1]["type"] == "image_url"
    assert last["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_image_only_message_reaches_the_model(make_app) -> None:
    """纯图消息入队，正文部分只有 [图片]。"""
    env = await make_app(vision_enabled=True)
    await wait_until(lambda: env.app.ready)

    env.stub.feed(message_frame(12, LOBBY, 102, "", image_id="img_1"))
    await wait_until(lambda: env.model.calls >= 1)

    last = env.model.inputs[0][-1]
    assert last["content"][0] == {"type": "text", "text": "[图片]"}
    assert last["content"][1]["type"] == "image_url"


async def test_image_history_keeps_the_marker_not_the_bytes(make_app) -> None:
    """送达后提交进历史的是带标记的文本；图片本身绝不进历史。"""
    env = await make_app(vision_enabled=True)
    await wait_until(lambda: env.app.ready)

    env.stub.feed(message_frame(13, LOBBY, 103, "@mybot 这是什么", image_id="img_1"))
    await wait_for_status(env.app, 103)

    session = lobby_thread_session_key(103)
    history = env.app._ctx.build_messages(session, env.config.system_prompt)
    user_turns = [message["content"] for message in history if message["role"] == "user"]

    assert "[图片]" in user_turns[-1]
    assert "base64" not in user_turns[-1], "图片字节不得进历史"


async def test_unavailable_image_downgrades_to_a_text_only_turn(make_app) -> None:
    """取图失败但有正文：降级为纯文本轮，prompt 里标明图没拿到。"""
    env = await make_app(vision_enabled=True)
    await wait_until(lambda: env.app.ready)
    env.stub.image_status = 404

    env.stub.feed(message_frame(14, LOBBY, 104, "@mybot 这是什么", image_id="img_1"))
    await wait_until(lambda: env.model.calls >= 1)

    last = env.model.inputs[0][-1]
    assert isinstance(last["content"], str)
    assert last["content"].endswith("[图片未提供]\n---\n这是什么")


async def test_image_only_with_fetch_failure_gets_a_local_notice(make_app) -> None:
    """纯图且取不到：不调模型，发一条本地文案应答。"""
    env = await make_app(vision_enabled=True)
    await wait_until(lambda: env.app.ready)
    env.stub.image_status = 404

    env.stub.feed(message_frame(15, LOBBY, 105, "", image_id="img_1"))
    await wait_until(lambda: env.stub.call_count("POST", LOBBY_MESSAGES) >= 1)

    assert env.model.calls == 0
    body = env.stub.bodies("POST", LOBBY_MESSAGES)[-1]
    assert body["content"] == IMAGE_UNAVAILABLE_TEXT
    assert body["reply_to"] == 105


async def test_vision_disabled_never_requests_the_image(make_app) -> None:
    """关闭视觉时行为与改动前一致：不取图、不调模型、走本地提示。"""
    env = await make_app()  # vision_enabled 默认 False
    await wait_until(lambda: env.app.ready)

    env.stub.feed(message_frame(16, LOBBY, 106, "", image_id="img_1"))
    await wait_until(lambda: env.stub.call_count("POST", LOBBY_MESSAGES) >= 1)

    assert env.model.calls == 0
    assert env.stub.call_count("GET", "/api/images/img_1/raw") == 0
    assert env.stub.bodies("POST", LOBBY_MESSAGES)[-1]["content"] == IMAGE_UNAVAILABLE_TEXT


async def test_svg_image_is_not_sent_to_the_model(make_app) -> None:
    """图床白名单含 SVG，但我们不转发：按读不到处理，降级为纯文本轮。"""
    env = await make_app(vision_enabled=True)
    await wait_until(lambda: env.app.ready)
    env.stub.image_bytes = SVG_BYTES

    env.stub.feed(message_frame(17, LOBBY, 107, "@mybot 这是什么", image_id="img_1"))
    await wait_until(lambda: env.model.calls >= 1)

    last = env.model.inputs[0][-1]
    assert isinstance(last["content"], str)
    assert last["content"].endswith("[图片未提供]\n---\n这是什么")
```

导入区补 `IMAGE_UNAVAILABLE_TEXT`（`from raricy_bot.texts import (...)` 里）。
`PNG_BYTES` 若与 `AppStub.image_bytes` 的字面量重复，就只保留一处并在另一处引用它。

- [ ] **Step 2: 跑测试确认失败**

```bash
python -m pytest tests/test_app.py -q -k "image"
```

Expected: FAIL —— 图片被忽略，`model.inputs[0][-1]["content"]` 是字符串而非内容块列表。

- [ ] **Step 3: 实现**

`src/raricy_bot/app.py`：

1. 导入区补：

```python
from .core.vision import ImageLoader, attach_image
```

2. `__init__` 里构造 loader，并把开关透传给路由器：

```python
        self._vision_enabled = config.model.vision_enabled
        self._image_loader = ImageLoader(
            self._client, max_bytes=config.model.max_image_bytes
        )
```

```python
        self._router = MessageRouter(
            ...
            storage=self._config.storage,
            vision_enabled=self._vision_enabled,
        )
```

3. `_handle_request` 里，在代次检查之后、`_pending_turn` 之前插入：

```python
            # 取图在模型门之外：三个 worker 各自下载互不阻塞，超时由站点请求超时兜住。
            part, image_state = await self._load_image(request)
            if part is None and image_state != "none" and not request.user_text:
                # 纯图且读不到：用户明确发来一张图，必须给个交代，但不值得占用一次模型调用。
                await self._send_image_unavailable(request)
                return
            pending = self._pending_turn(request, image_state)
```

并在 `self._apply_reply_prefix(messages, request)` **之后**加：

```python
            # 必须在 _apply_reply_prefix 之后：那一步按字符串拼接 content。
            if part is not None:
                attach_image(messages, part)
```

4. 新增三个方法（`_pending_turn` 就地改造，其余插在它旁边）：

```python
    async def _load_image(self, request: Request) -> tuple[dict[str, Any] | None, str]:
        """取回本轮图片；关闭视觉时**完全不碰图床**，进程里没有新增请求。"""
        if not self._vision_enabled:
            return None, "none"
        return await self._image_loader.load(request.message)

    async def _send_image_unavailable(self, request: Request) -> None:
        """纯图但读不到：本地提示，不调模型。

        kind 用 `notice_local` 而不是 `notice`：它与第 9.1 步的纯媒体提示同类，
        都是应答明确用户动作的本地回复（D-1）。用 notice 会占掉该用户 24 小时的
        主动通知名额，把一次「图没读到」变成「今天别再提醒他」（D-30）。
        """
        if self._unavailable:
            return
        outcome = await self._sender.send(
            request.channel_id,
            texts.IMAGE_UNAVAILABLE_TEXT,
            request.message.id,
            kind="notice_local",
            thread_root_id=request.thread_root_id,
        )
        self._note_forbidden(outcome)

    @staticmethod
    def _with_image_marker(user_text: str, image_state: str) -> str:
        """给本轮正文加上图片标记（设计 §4）。

        `[图片]` 表示这一轮确实带了图；`[图片未提供]` 表示本来有图但没取到 ——
        让模型知道自己没看到图，而不是以为用户什么都没发。
        """
        if image_state == "ok" and user_text:
            return f"[图片]\n---\n{user_text}"
        if image_state == "ok":
            return "[图片]"
        if image_state != "none" and user_text:
            return f"[图片未提供]\n---\n{user_text}"
        return user_text

    @staticmethod
    def _pending_turn(request: Request, image_state: str) -> str:
        """构造本轮待提交的用户内容（不含直接引用，见 D-7）。

        - 大区：带上站点发言者标签，图片标记在包装**内部**（图属于发言人这条消息）；
        - 私聊：就是正文本身。
        """
        text = BotApp._with_image_marker(request.user_text, image_state)
        if request.channel_kind != "lobby":
            return text
        return speaker_wrapper(request.message.author.username, text)
```

导入区补 `from typing import Any`（若尚无）。

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_app.py -q && python -m pytest tests -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/raricy_bot/app.py src/raricy_bot/core/vision.py
git commit -m "feat(app): attach chat images to the current turn when vision is on"
```

---

### Task 9: 日志与隐私回归

**Files:**
- Modify: `tests/test_logging_safety.py`（复用其中的 `captured_logs` fixture，它把根 logger 设为 DEBUG 并捕获全部输出）

**Interfaces:**
- Consumes: Task 5 的 `ImageLoader`；`logging_setup` 的 `captured_logs` 模式。
- Produces: 一条断言「图片 URL、图片字节、base64 片段都不进日志」的回归用例。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_logging_safety.py`：

```python
IMAGE_URL = "/api/images/ZZTOP-SECRET-IMAGE-ID/raw"
IMAGE_BODY = b"\x89PNG\r\n\x1a\n" + b"ZZTOP-SECRET-IMAGE-BODY"


class _ImageStub:
    """只回一张图的假客户端；抛错路径由 error 控制。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def fetch_image(self, url: str, *, max_bytes: int) -> bytes:
        if self.error is not None:
            raise self.error
        return IMAGE_BODY


def _image_message() -> SimpleNamespace:
    return SimpleNamespace(
        image=SimpleNamespace(id="img_1", url=IMAGE_URL, mime_type="image/png"),
        image_missing=False,
    )


async def test_image_bytes_and_url_never_reach_the_log(captured_logs) -> None:
    """取图成功与失败两条路径都不得把 URL、字节或 base64 写进日志。"""
    message = _image_message()

    part, reason = await ImageLoader(_ImageStub(), max_bytes=4096).load(message)
    assert reason == "ok" and part is not None

    failed, failed_reason = await ImageLoader(
        _ImageStub(error=ImageFetchError("too_large")), max_bytes=4096
    ).load(message)
    assert failed is None and failed_reason == "too_large"

    output = captured_logs.getvalue()
    assert "ZZTOP-SECRET-IMAGE-ID" not in output, "图片 URL 泄漏进日志"
    assert "ZZTOP-SECRET-IMAGE-BODY" not in output, "图片字节泄漏进日志"
    assert "base64" not in output, "data URL 片段泄漏进日志"
    assert "reason=too_large" in output, "降级日志本身要写得出来，否则这条用例可能是空跑"
```

导入区补 `from types import SimpleNamespace`、`from raricy_bot.core.vision import ImageLoader`、`from raricy_bot.site.client import ImageFetchError`。

- [ ] **Step 2: 跑测试确认通过，并确认它不是空跑**

```bash
python -m pytest tests/test_logging_safety.py -q -k image
```

Expected: PASS。用例里那句 `assert "reason=too_large" in output` 是防空跑断言：
它保证降级日志确实写出来了，否则「日志里没有 URL」可能是因为压根没有日志。

再做一次反向验证，确认这条用例抓得住问题：临时把 `ImageLoader._log_failure` 改成
往**白名单内**的字段里塞 URL（例如 `fields["kind"] = image_url`），跑一次应当 FAIL；
把临时改动删掉后应当恢复 PASS。

- [ ] **Step 3: 无需提交**

本任务只新增 `tests/test_logging_safety.py` 的用例，而 `.gitignore` 排除了 `tests/`，
因此没有需要提交的文件。仓库级配置的原因见本计划的 Global Constraints。
（如果你希望这条测试也进版本控制，那是另一件事：得先改 `.gitignore`。）


---

### Task 10: 文档同步与全量回归

**Files:**
- Modify: `docs/design/INTERFACES.md`
- Modify: `docs/design/DESIGN_DECISIONS.md`
- Modify: `docs/design/SYSTEM_PROMPTS.md`
- Modify: `docs/usage/USAGE.md`、`docs/usage/DEPLOYMENT.md`

**Interfaces:**
- Consumes: 前面九个任务的全部产出。
- Produces: 与代码一致的契约文本。

- [ ] **Step 1: `INTERFACES.md`**

逐处改：

1. §1 `ModelConfig` 加 `vision_enabled: bool = False` 与 `max_image_bytes: int = 5242880`；
   规则段加「`max_image_bytes` 为正整数且 `<= 10485760`」、`vision_enabled` 非布尔即 `ConfigError`。
2. §4 加 `def has_image(message: Any) -> bool  # image is not None and not image_missing`，
   并说明它与 `has_media` 的分工。
3. §5 常量清单：把 `UNSUPPORTED_MEDIA_TEXT` 的注释改成「纯博客」；新增
   `IMAGE_UNAVAILABLE_TEXT` 与 `HELP_TEXT_WITH_VISION`；说明两份 HELP 文案共用首尾、
   只有能力句不同。
4. §7 加 `ImageFetchError` 与 `fetch_image` 的完整签名与四条 reason，写明：
   同源（scheme+host+有效端口）、超限即断、401 不重登、**不写日志**。
5. §12 构造签名加 `vision_enabled: bool = False`；第 9.1 步改写成三分支表，
   `image_only` 加入可行动 reason 表。
6. §14 `ModelClient.complete` 的注解放宽为 `list[dict[str, Any]]`，并说明只有当前轮的
   user 消息可能是内容块列表。
7. §16 worker 流程插入取图、纯图降级、标记与 `attach_image` 的顺序约束。
8. §19 第 5 条改写为：不实现博客理解、工具调用、联网、长期记忆；图片理解仅在
   `model.vision_enabled` 为真时提供，且只把当前轮那一张图取回内存转交模型。
9. 新增 §20（或按现有编号续）`core/vision.py`：`IMAGE_MIME_ALLOWLIST`、四个纯函数、
   `ImageLoader` 的七个 reason 与两条硬约束（不信任 DTO 的 mime、不转发 SVG）。

- [ ] **Step 2: `DESIGN_DECISIONS.md`**

追加三条（沿用现有条目的编号与格式：问题 / 决定 / 理由 / 影响）：

- **D-28 图片只进当前轮，历史记标记**：与 D-7 同源；图片体积大且只对当下有意义，
  进历史会让每一轮都重复外送一份 base64。历史里留 `[图片]` / `[图片未提供]`，
  让后续轮次知道当时发生了什么。
- **D-29 自己下载转 base64，不把站点 URL 交给模型商**：图床是相对路径且公开图无需登录；
  自己取可以控制体积、不依赖对方回源、不把内网可达地址变成第三方可探测的目标。
  同源是硬约束（Cookie 只发往站点），不转发 SVG。
- **D-30 取图失败降级为纯文本轮，纯图失败转本地提示且用 `notice_local`**：一条读不到的图
  不该把同一条消息里的正常提问一起丢掉；纯图时用户确实发了一张图，必须给个交代。
  用 `notice_local` 是因为它和纯媒体提示同类（D-1），用 `notice` 会占掉该用户 24 小时的
  主动通知名额。

- [ ] **Step 3: `SYSTEM_PROMPTS.md`、`USAGE.md`、`DEPLOYMENT.md`**

- `SYSTEM_PROMPTS.md`：§1 的分工与 §2 的提示词正文里，凡出现「不能看图」的表述按
  开关口径改写；§3.5「机器人自身的硬性事实」加一条「配置了视觉模型时可以查看用户发来的
  图片，图片只来自当前轮」。
- `USAGE.md`：说明 `model.vision_enabled` 的作用、开启前提（模型必须支持视觉）、
  默认关闭、以及纯图消息会计一次回复配额。
- `DEPLOYMENT.md`：在配置项清单里加两个新键与取值范围，并写明「关闭时进程不会对图床
  发出任何请求」。

- [ ] **Step 4: 全量回归**

```bash
python -m pytest tests -q
```

Expected: 全部 PASS，输出干净无 warning（`filterwarnings = ["error"]`）。

另跑一次「关闭视觉」的既有行为回归：

```bash
python -m pytest tests/test_app.py tests/test_router.py -q
```

- [ ] **Step 5: 提交**

```bash
git add docs/design/INTERFACES.md docs/design/DESIGN_DECISIONS.md docs/design/SYSTEM_PROMPTS.md docs/usage/USAGE.md docs/usage/DEPLOYMENT.md
git commit -m "docs: record chat image input contract, decisions and ops notes"
```

---

## 完成标准

- `python -m pytest tests -q` 全绿且无 warning。
- `model.vision_enabled` 为 `false` 时，整条链路与改动前逐字一致（Task 8 的
  `test_vision_disabled_never_requests_the_image` 证明它连图床都不碰）。
- 图片 URL、字节、base64 都不出现在日志与 SQLite 中（Task 9）。
- `INTERFACES.md` / `DESIGN_DECISIONS.md` / `SYSTEM_PROMPTS.md` 与代码一致（Task 10）。
