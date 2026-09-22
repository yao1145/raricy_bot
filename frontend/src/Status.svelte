<script lang="ts">
  import * as api from "./api";

  let { status, onchanged }: { status: api.StatusSnapshot; onchanged: () => void } = $props();

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
  };

  async function act(action: "start" | "stop" | "restart"): Promise<void> {
    busy = true;
    failure = null;
    message = null;
    try {
      const operation = await api.botAction(action, status.config.revision ?? undefined);
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
      return `操作被拒绝：${error.code}`;
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

<div class="panel">
  <h2>机器人</h2>
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
  <div class="panel">
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
