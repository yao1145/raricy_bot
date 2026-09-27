<script lang="ts">
  import * as api from "./api";
  import {
    CONFIG_STATE_LABELS,
    IDENTITY_LABELS,
    PROFILE_STATE_LABELS,
    accountCodeNotice,
    conflictNotice,
  } from "./texts";

  let {
    status,
    profile = null,
    epoch = null,
    onchanged,
  }: {
    status: api.StatusSnapshot;
    /** 当前活动档案的卡片（来自 `GET /api/profiles`），没有选中时为 null。 */
    profile?: api.ProfileCard | null;
    /** 状态 DTO 的活动代次；过渡写入口要原样带上（§59、D-146）。 */
    epoch?: number | null;
    onchanged: () => void;
  } = $props();

  let busy = $state(false);
  let message = $state<string | null>(null);
  let failure = $state<string | null>(null);

  const processState = $derived(status.process.state);
  const running = $derived(processState === "running" || processState === "starting");

  const PROCESS_LABEL: Record<string, string> = {
    stopped: "已停止",
    starting: "启动中",
    running: "运行中",
    stopping: "停止中",
    failed: "故障",
  };

  const CONFIG_LABEL: Record<string, string> = {
    configured: "已配置",
    needs_setup: "尚未配置",
    needs_credentials: "需要重新填写凭据",
    invalid: "配置无效",
    no_selection: "没有选中账号",
  };

  async function act(action: "start" | "stop" | "restart"): Promise<void> {
    busy = true;
    failure = null;
    message = null;
    try {
      // 启停的过渡门：请求要带当前活动档案与代次，缺字段会被服务端回
      // 409 client_upgrade_required（§59、D-146）；停止不需要这两个字段。
      const operation = await api.botAction(action, {
        profileId: status.active_profile_id ?? null,
        epoch,
      });
      await poll(operation.operation_id);
    } catch (error) {
      failure = describe(error);
    } finally {
      busy = false;
      onchanged();
    }
  }

  async function poll(operationId: string): Promise<void> {
    // 轮询预算要盖住 Worker 的启动等待（60 秒），否则慢启动会被误报成「仍在进行」。
    for (let attempt = 0; attempt < 180; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      const body = await api.getOperation(operationId);
      if (body.operation.finished) {
        message = `${label(body.operation.kind)}：${body.operation.result ?? ""}`;
        return;
      }
    }
    message = "操作仍在进行中；可以稍后在状态里查看结果。";
  }

  function label(kind: string): string {
    return { start: "启动", stop: "停止", restart: "重启" }[kind] ?? kind;
  }

  function describe(error: unknown): string {
    if (error instanceof api.ApiError) {
      if (error.status === 401) return "会话已失效，请重新打开管理页。";
      if (error.code === "config_not_ready") return "还没有可用的正式配置，请先在设置里保存。";
      // 固定文案集中在 texts.ts；未命中的码才回退到原始码。
      return conflictNotice(error.code) ?? accountCodeNotice(error.code) ?? `操作被拒绝：${error.code}`;
    }
    return "操作失败；请查看近期事件里的固定事件码。";
  }

  async function quitAll(): Promise<void> {
    if (!confirm("退出后机器人会停止、控制服务会关闭；确定继续吗？")) return;
    try {
      const body = await api.quit();
      message = body.message;
    } catch (error) {
      failure = describe(error);
    }
  }

  function snapshot(key: string): Record<string, any> | null {
    const snapshot = status.worker.snapshot;
    if (!snapshot) return null;
    const section = snapshot[key];
    return section && typeof section === "object" ? section : null;
  }

  const site = $derived(snapshot("site"));
  const model = $derived(snapshot("model"));
  const comments = $derived(snapshot("comments"));
  const kb = $derived(snapshot("knowledge_base"));
  const memory = $derived(snapshot("memory"));
  const archive = $derived(snapshot("archive"));
</script>

<div class="status-layout">
<div class="panel account-panel">
  <h2>当前账号</h2>
  {#if profile}
    <dl class="kv">
      <dt>标签</dt>
      <dd>{profile.display_name || "（未设置）"}</dd>
      <dt>账号</dt>
      <dd>{profile.account ?? "（未填写）"}</dd>
      <dt>稳定 ID</dt>
      <dd>{profile.site_user_id ?? "（未验证）"}</dd>
      <dt>档案</dt>
      <dd>
        <span class="state" class:ok={profile.state === "active"} class:warn={profile.state !== "active"}>
          {PROFILE_STATE_LABELS[profile.state] ?? profile.state}
        </span>
        <span class="state" class:ok={profile.identity_state === "verified"} class:warn={profile.identity_state !== "verified"}>
          {IDENTITY_LABELS[profile.identity_state] ?? profile.identity_state}
        </span>
        {#if profile.is_active}<span class="state ok">当前选中</span>{/if}
        {#if profile.is_running}<span class="state ok">运行中</span>{/if}
        {#if profile.is_startup_target}<span class="state warn">启动目标</span>{/if}
      </dd>
      <dt>配置状态</dt>
      <dd>
        <span class="state" class:ok={profile.config.state === "configured"} class:warn={profile.config.state !== "configured"}>
          {CONFIG_STATE_LABELS[profile.config.state] ?? profile.config.state}
        </span>
      </dd>
    </dl>
    <p class="hint">账号的添加、切换与移除都在「账号」页；这里只显示当前选中档案的事实。</p>
  {:else}
    <p class="hint">当前没有选中的账号档案；到「账号」页选择或添加一个。</p>
  {/if}
</div>

<div class="panel status-hero">
  <h2>机器人</h2>
  <div class="process-display" class:running={processState === "running"} class:failed={processState === "failed"}>
    <span class="process-light" aria-hidden="true"></span>
    <div><strong>{PROCESS_LABEL[processState] ?? processState}</strong><span>当前进程状态</span></div>
    {#if status.process.pid}<code>PID {status.process.pid}</code>{/if}
  </div>
  <dl class="kv">
    <dt>进程</dt>
    <dd>
      <span class="state" class:ok={processState === "running"} class:warn={processState === "starting" || processState === "stopping"} class:bad={processState === "failed"}>
        {PROCESS_LABEL[processState] ?? processState}
      </span>
      {#if status.process.pid}<span class="hint">pid {status.process.pid}</span>{/if}
      {#if status.process.forced_stop}<span class="hint">（上一次是强制停止）</span>{/if}
      {#if status.process.exit_reason}<span class="hint">（{status.process.exit_reason}）</span>{/if}
    </dd>
    <dt>配置</dt>
    <dd>
      <span class="state" class:ok={status.config.state === "configured"} class:bad={status.config.state === "invalid"}>
        {CONFIG_LABEL[status.config.state] ?? status.config.state}
      </span>
      {#if status.config.revision !== null}<span class="hint">revision {status.config.revision}</span>{/if}
      {#if status.config.account}<span class="hint">账号 {status.config.account}</span>{/if}
      {#if status.restart_required}
        <span class="state warn">有改动待重启生效</span>
        <button class="ghost" onclick={() => act("restart")}>现在重启</button>
      {/if}
    </dd>
    <dt>状态快照</dt>
    <dd>
      {#if status.worker.freshness === "fresh"}
        <span class="state ok">实时</span>
      {:else if status.worker.freshness === "stale"}
        <span class="state warn">已过期</span>
      {:else}
        <span class="state warn">未知</span>
      {/if}
      <span class="hint">缺少证据时如实报未知，不从日志猜测</span>
    </dd>
  </dl>

  <div class="row" style="margin-top:16px">
    <button class="action" disabled={busy || running} onclick={() => act("start")}>启动</button>
    <button class="action" disabled={busy || !running} onclick={() => act("stop")}>停止</button>
    <button class="action" disabled={busy || !running} onclick={() => act("restart")}>重启</button>
    <button class="action danger" disabled={busy} onclick={quitAll}>退出 Light</button>
  </div>
  {#if message}<div class="notice">{message}</div>{/if}
  {#if failure}<div class="error">{failure}</div>{/if}
</div>

{#if status.worker.freshness === "fresh"}
  <div class="panel systems-panel">
    <h2>子系统</h2>
    <dl class="kv">
      <dt>站点</dt>
      <dd>
        {#if site}
          登录 {site.authenticated ? "正常" : "未登录"} ·
          SSE {site.sse_connected ? "已连接" : "未连接"} ·
          可聊天 {site.chat_ready ? "是" : "否"}
        {:else}未知{/if}
      </dd>
      <dt>模型</dt>
      <dd>
        {#if model}
          已配置 {model.configured ? "是" : "否"} · 图片 {model.vision_enabled ? "已开" : "未开"}
        {:else}未知{/if}
      </dd>
      <dt>评论</dt>
      <dd>{#if comments}{comments.enabled ? (comments.running ? "运行中" : "已开启但未运行") : "未开启"}{:else}未知{/if}</dd>
      <dt>知识库</dt>
      <dd>
        {#if kb}
          {kb.enabled ? "已开启" : "未开启"} · 资料 {kb.documents ?? 0} 份 · 片段 {kb.chunks ?? 0}
        {:else}未知{/if}
      </dd>
      <dt>记忆</dt>
      <dd>{#if memory}{memory.enabled ? (memory.armed ? "运行中" : "已开启、未就绪") : "未开启"}{:else}未知{/if}</dd>
      <dt>永久归档</dt>
      <dd>{#if archive}{archive.enabled ? "已开启" : "未开启"}{:else}未知{/if}</dd>
      <dt>最近显式测试</dt>
      <dd>
        {#each Object.entries(status.tests) as [kind, result] (kind)}
          <span class="hint">
            {kind === "site" ? "站点" : "模型"}：{result.state === "fresh"
              ? (result.ok ? "通过" : `未通过（${result.detail}）`)
              : result.state === "stale"
                ? "已过期（配置变更后需重测）"
                : "未测试"}
          </span>
        {/each}
      </dd>
    </dl>
  </div>
{/if}
</div>
