<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import * as api from "./api";
  import Events from "./Events.svelte";
  import Settings from "./Settings.svelte";
  import Status from "./Status.svelte";
  import Wizard from "./Wizard.svelte";

  type Phase = "boot" | "expired" | "ready";

  let phase = $state<Phase>("boot");
  let status = $state<api.StatusSnapshot | null>(null);
  let tab = $state<"status" | "settings" | "events">("status");
  let notice = $state<string | null>(null);
  let error = $state<string | null>(null);

  let refreshTimer: ReturnType<typeof setInterval> | null = null;
  let unsubscribe: (() => void) | null = null;

  async function refresh(): Promise<void> {
    try {
      const body = await api.getStatus();
      status = body.status;
      error = null;
    } catch (failure) {
      if (failure instanceof api.ApiError && failure.status === 401) {
        phase = "expired";
      } else {
        error = "无法读取状态；控制服务可能正在退出。";
      }
    }
  }

  onMount(async () => {
    const token = api.bootstrapToken();
    // 令牌只在 fragment 里出现一次：无论成功与否都先清掉地址栏（§8.1）。
    api.clearFragment();
    try {
      if (token) await api.exchange(token);
      if (!api.haveSession()) {
        phase = "expired";
        return;
      }
      phase = "ready";
      await refresh();
      refreshTimer = setInterval(refresh, 3000);
      unsubscribe = api.subscribeEvents(
        () => {
          void refresh();
        },
        () => {
          notice = "事件游标已失效，下面的列表从当前时刻重新开始。";
        },
      );
    } catch {
      phase = "expired";
    }
  });

  onDestroy(() => {
    if (refreshTimer) clearInterval(refreshTimer);
    unsubscribe?.();
  });

  const configState = $derived(status?.config.state ?? "unknown");
  const showWizard = $derived(configState === "needs_setup" || configState === "needs_credentials");
</script>

<div class="shell">
  <header class="brand">
    <h1>Raricy Light</h1>
    <span class="tag">本机管理页 · 仅回环可访问</span>
  </header>

  {#if phase === "boot"}
    <div class="panel">正在获取本机会话…</div>
  {:else if phase === "expired"}
    <div class="panel">
      <h2>会话已失效</h2>
      <p>
        这个页面只在当前控制服务运行期间有效。请从桌面上的 <strong>Raricy Bot Light</strong>
        图标再次打开管理页（第二次启动只会激活已有实例，不会新建一个）。
      </p>
    </div>
  {:else}
    <nav>
      <button class:active={tab === "status"} onclick={() => (tab = "status")}>状态</button>
      <button class:active={tab === "settings"} onclick={() => (tab = "settings")}>设置</button>
      <button class:active={tab === "events"} onclick={() => (tab = "events")}>近期事件</button>
    </nav>

    {#if error}<div class="error">{error}</div>{/if}
    {#if notice}<div class="notice">{notice}</div>{/if}

    {#if tab === "status"}
      {#if showWizard}
        <Wizard ondone={refresh} />
      {:else if status}
        <Status {status} onchanged={refresh} />
      {/if}
    {:else if tab === "settings"}
      <Settings onchanged={refresh} />
    {:else}
      <Events />
    {/if}
  {/if}
</div>
