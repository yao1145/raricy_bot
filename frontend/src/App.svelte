<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import * as api from "./api";
  import Accounts from "./Accounts.svelte";
  import DesktopSettings from "./DesktopSettings.svelte";
  import Events from "./Events.svelte";
  import Recovery from "./Recovery.svelte";
  import Settings from "./Settings.svelte";
  import Status from "./Status.svelte";
  import Wizard from "./Wizard.svelte";
  import { ACCOUNT_NOTICES } from "./texts";

  type Phase = "boot" | "expired" | "ready";
  type Tab = "status" | "settings" | "events" | "desktop" | "accounts";

  let phase = $state<Phase>("boot");
  let status = $state<api.StatusSnapshot | null>(null);
  let profiles = $state<api.ProfileCard[] | null>(null);
  let catalog = $state<api.ProfileCatalog | null>(null);
  let tab = $state<Tab>("status");
  // 用户主动点过页签之后，状态变化不再自动夺走视图。
  let tabTouched = $state(false);
  // 向导：首次设置（profileId 为 null）或绑定到某个账号（添加账号 / 没有配置的账号）。
  let wizard = $state<{ profileId: string | null } | null>(null);
  // 首次设置向导一旦出现就锁定到用户关掉为止：保存过程中档案被建立，
  // 「没有档案」的条件随即消失，但不能因此把向导从用户脚下撤走。
  let wizardDismissed = $state(false);
  let notice = $state<string | null>(null);
  let error = $state<string | null>(null);

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
      <button class:active={tab === "status"} onclick={() => selectTab("status")}>状态</button>
      <button class:active={tab === "settings"} onclick={() => selectTab("settings")}>设置</button>
      <button class:active={tab === "events"} onclick={() => selectTab("events")}>近期事件</button>
      <button class:active={tab === "desktop"} onclick={() => selectTab("desktop")}>桌面</button>
      <button class:active={tab === "accounts"} onclick={() => selectTab("accounts")}>账号</button>
    </nav>

    {#if error}<div class="error">{error}</div>{/if}
    {#if notice}<div class="notice">{notice}</div>{/if}

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
      {#key status?.active_profile_id}
        <Settings profile={activeCard} {epoch} onchanged={refresh} />
      {/key}
    {:else if tab === "accounts"}
      {#key status?.active_profile_id}
        <Accounts
          {status}
          profiles={profiles ?? []}
          {catalog}
          onchanged={refresh}
          onedit={() => selectTab("settings")}
          onwizard={(profileId) => (wizard = { profileId })}
        />
      {/key}
    {:else if tab === "desktop"}
      <DesktopSettings onchanged={refresh} />
    {:else}
      <Events profileId={status?.active_profile_id ?? null} />
    {/if}
  {/if}
</div>
