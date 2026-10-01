<script lang="ts">
  import { onDestroy, onMount, tick } from "svelte";
  import * as api from "./api";
  import Accounts from "./Accounts.svelte";
  import DesktopSettings from "./DesktopSettings.svelte";
  import Events from "./Events.svelte";
  import Recovery from "./Recovery.svelte";
  import Settings from "./Settings.svelte";
  import Intro from "./Intro.svelte";
  import Status from "./Status.svelte";
  import Wizard from "./Wizard.svelte";
  import { ACCOUNT_NOTICES } from "./texts";

  type Phase = "boot" | "expired" | "ready";
  type Tab = "status" | "accounts" | "settings" | "events";
  type SettingsView = "config" | "desktop";

  let phase = $state<Phase>("boot");
  let status = $state<api.StatusSnapshot | null>(null);
  let profiles = $state<api.ProfileCard[] | null>(null);
  let catalog = $state<api.ProfileCatalog | null>(null);
  let tab = $state<Tab>("status");
  let settingsView = $state<SettingsView>("config");
  // 用户主动点过页签之后，状态变化不再自动夺走视图。
  let tabTouched = $state(false);
  // 向导：首次设置（profileId 为 null）或绑定到某个账号（添加账号 / 没有配置的账号）。
  let wizard = $state<{ profileId: string | null } | null>(null);
  // 首次设置向导一旦出现就锁定到用户关掉为止：保存过程中档案被建立，
  // 「没有档案」的条件随即消失，但不能因此把向导从用户脚下撤走。
  let wizardDismissed = $state(false);
  let notice = $state<string | null>(null);
  let error = $state<string | null>(null);
  let intro = $state(true);
  let introLeaving = $state(false);
  let shell: HTMLDivElement;

  let refreshTimer: ReturnType<typeof setInterval> | null = null;
  let unsubscribe: (() => void) | null = null;

  async function refresh(): Promise<void> {
    try {
      const state = await api.getStatus();
      status = state.status;
      error = null;
    } catch (failure) {
      if (failure instanceof api.ApiError && failure.status === 401) {
        phase = "expired";
        return;
      }
      error = "无法读取状态；控制服务可能正在退出。";
      return;
    }
    try {
      const list = await api.getProfiles();
      profiles = list.profiles;
      catalog = list.catalog;
    } catch {
      // 档案列表读不出来（例如恢复态下的元数据故障）时保留上一次的结果，
      // 不假装「一个账号都没有」：原因由 `config.state == "recovery"` 与稳定码承载。
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

  function selectTab(next: Tab): void {
    tabTouched = true;
    tab = next;
  }

  async function finishIntro(): Promise<void> {
    intro = false;
    await tick();
    shell.focus({ preventScroll: true });
  }

  const pageCopy: Record<Tab, { title: string; detail: string }> = {
    status: { title: "运行状态", detail: "查看当前账号、机器人进程和子系统的实时状态。" },
    accounts: { title: "账号档案", detail: "管理账号身份、活动档案与凭据清理。" },
    settings: { title: "配置中心", detail: "调整模型、能力和桌面启动方式。" },
    events: { title: "近期事件", detail: "按时间查看当前账号与控制服务的事件。" },
  };

  const configState = $derived(status?.config.state ?? "unknown");
  // 恢复状态不是向导入口：坏元数据只给只读提示，绝不把用户带进空向导（F2）。
  const showRecovery = $derived(configState === "recovery");
  const recoveryCode = $derived(status?.config.error ?? null);
  // 首次设置向导只在「一个档案都没有、且服务端说还没设置」时自动出现；
  // 缺凭据 / 配置无效 / 身份未验证各有自己的提示面板（D-147）。
  const firstSetup = $derived(
    profiles !== null && profiles.length === 0 && configState === "needs_setup",
  );
  const activeCard = $derived(
    profiles?.find((card) => card.profile_id === status?.active_profile_id) ?? null,
  );
  const epoch = $derived(status?.profile_epoch ?? null);
  const noticeKey = $derived.by(() => {
    if (!status) return null;
    if (configState === "no_selection") return "no_selection";
    if (configState === "needs_credentials") return "needs_credentials";
    if (configState === "invalid") return "invalid";
    if (configState === "configured" && activeCard?.identity_state === "unverified") {
      return "identity_unverified";
    }
    return null;
  });
  const stateNotice = $derived(noticeKey ? (ACCOUNT_NOTICES[noticeKey] ?? null) : null);

  // 「有档案但一个都没选中」不是故障：自动进账号页让用户选，但不覆盖用户已点过的页签。
  $effect(() => {
    if (configState === "no_selection" && !tabTouched && !wizard) tab = "accounts";
  });

  // 首次设置只在这里转成向导：之后由 `wizard` 状态负责，直到 ondone / oncancel。
  $effect(() => {
    if (firstSetup && !wizard && !wizardDismissed) wizard = { profileId: null };
  });

  function finishWizard(): void {
    wizard = null;
    tabTouched = true;
    tab = "accounts";
    void refresh();
  }

  function cancelWizard(): void {
    wizard = null;
    wizardDismissed = true;
    tabTouched = true;
    tab = "accounts";
  }
</script>

{#if intro}
  <Intro onenter={() => (introLeaving = true)} ondone={finishIntro} />
{/if}

<div class="shell" class:intro-active={intro} class:intro-leaving={introLeaving} inert={intro} bind:this={shell} tabindex="-1">
  <header class="brand">
    <div class="brand-identity">
      <img src="/favicon.ico" width="42" height="42" alt="" />
      <div>
        <h1>Raricy <span>Light</span></h1>
        <p>本机控制台</p>
      </div>
    </div>
    <div class="brand-state">
      <span class="connection-dot" class:online={status?.process.state === "running"}></span>
      {status?.process.state === "running" ? "机器人运行中" : "本地控制服务"}
      <span class="brand-local">仅限本机</span>
    </div>
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
    <nav class="module-nav" aria-label="主要页面">
      <button class="module-link module-link--status" class:active={tab === "status"} aria-current={tab === "status" ? "page" : undefined} onclick={() => selectTab("status")}>
        <span class="module-shape shape-orbit" aria-hidden="true"></span>
        <span class="module-label">状态</span><span class="module-sub">运行与健康</span>
      </button>
      <button class="module-link module-link--accounts" class:active={tab === "accounts"} aria-current={tab === "accounts" ? "page" : undefined} onclick={() => selectTab("accounts")}>
        <span class="module-shape shape-satellite" aria-hidden="true"></span>
        <span class="module-label">账号</span><span class="module-sub">身份与档案</span>
      </button>
      <button class="module-link module-link--settings" class:active={tab === "settings"} aria-current={tab === "settings" ? "page" : undefined} onclick={() => selectTab("settings")}>
        <span class="module-shape shape-vector" aria-hidden="true"></span>
        <span class="module-label">设置</span><span class="module-sub">配置与桌面</span>
      </button>
      <button class="module-link module-link--events" class:active={tab === "events"} aria-current={tab === "events" ? "page" : undefined} onclick={() => selectTab("events")}>
        <span class="module-shape shape-signal" aria-hidden="true"></span>
        <span class="module-label">事件</span><span class="module-sub">最近记录</span>
      </button>
    </nav>

    {#if error}<div class="error">{error}</div>{/if}
    {#if notice}<div class="notice">{notice}</div>{/if}

    {#if !wizard}
      <div class="page-heading" data-page={tab}>
        <div><h2>{pageCopy[tab].title}</h2><p>{pageCopy[tab].detail}</p></div>
        {#if status?.active_profile_id && activeCard}
          <span class="active-profile">当前账号 <strong>{activeCard.display_name || activeCard.account || "未命名"}</strong></span>
        {/if}
      </div>
    {/if}

    <main id="main-content">
    {#if wizard}
      <Wizard profileId={wizard.profileId} ondone={finishWizard} oncancel={cancelWizard} />
    {:else if tab === "status"}
      {#if showRecovery}
        <Recovery code={recoveryCode} />
      {:else}
        {#if stateNotice}
          <div class="panel">
            <h2>{stateNotice.title}</h2>
            <p>{stateNotice.detail}</p>
            {#if stateNotice.action && stateNotice.target}
              {@const target = stateNotice.target}
              <div class="row">
                <button class="action" onclick={() => selectTab(target)}>{stateNotice.action}</button>
              </div>
            {/if}
          </div>
        {/if}
        {#if status}
          <!-- 切换档案即重建组件：旧表单、密码输入与测试结果随之作废（§9 末段）。 -->
          {#key status.active_profile_id}
            <Status {status} profile={activeCard} {epoch} onchanged={refresh} />
          {/key}
        {/if}
      {/if}
    {:else if tab === "settings"}
      <div class="subnav" aria-label="设置分区">
        <button class:active={settingsView === "config"} aria-current={settingsView === "config" ? "page" : undefined} onclick={() => (settingsView = "config")}>机器人配置</button>
        <button class:active={settingsView === "desktop"} aria-current={settingsView === "desktop" ? "page" : undefined} onclick={() => (settingsView = "desktop")}>桌面与启动</button>
      </div>
      {#if settingsView === "config"}
        {#key status?.active_profile_id}
          <Settings profile={activeCard} {epoch} onchanged={refresh} />
        {/key}
      {:else}
        <DesktopSettings onchanged={refresh} />
      {/if}
    {:else if tab === "accounts"}
      {#key status?.active_profile_id}
        <Accounts
          {status}
          profiles={profiles ?? []}
          {catalog}
          onchanged={refresh}
          onedit={() => { settingsView = "config"; selectTab("settings"); }}
          onwizard={(profileId) => (wizard = { profileId })}
        />
      {/key}
    {:else}
      <Events profileId={status?.active_profile_id ?? null} />
    {/if}
    </main>
  {/if}
</div>
