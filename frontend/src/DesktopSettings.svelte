<script lang="ts">
  // 桌面设置（N4 §8、§58、§59、§60）：登录 Windows 时启动 Light、打开 Light 时启动
  // 机器人、启动目标档案，以及启动项的实际登记事实与修复入口。
  //
  // 差异显性化：登记事实由服务端观测得到，页面只如实展示，不据此承诺「下次登录必定
  // 启动」；系统侧的禁用（任务管理器、设置页、企业策略）本程序读不到，也不覆盖。
  import * as api from "./api";
  import { conflictNotice, startupResultNotice, startupStatusNotice } from "./texts";

  let { onchanged }: { onchanged: () => void } = $props();

  let settings = $state<api.DesktopSettingsView | null>(null);
  let facts = $state<api.StartupFacts | null>(null);
  let revision = $state(0);
  let launchAtSignIn = $state(false);
  let startBotOnLaunch = $state(false);
  let busy = $state(false);
  let message = $state<string | null>(null);
  let failure = $state<string | null>(null);

  async function load(): Promise<void> {
    try {
      const view = await api.getDesktopSettings();
      settings = view;
      revision = view.settings_revision;
      launchAtSignIn = view.launch_at_sign_in;
      startBotOnLaunch = view.start_bot_on_launch;
      try {
        const status = await api.getStartupStatus();
        facts = status;
        revision = status.settings_revision;
      } catch {
        facts = null;
      }
      failure = null;
    } catch (error) {
      failure = describe(error);
    }
  }

  $effect(() => {
    void load();
  });

  async function save(): Promise<void> {
    if (!settings) return;
    busy = true;
    message = null;
    failure = null;
    try {
      const result = await api.saveDesktopSettings({
        expected_settings_revision: revision,
        launch_at_sign_in: launchAtSignIn,
        start_bot_on_launch: startBotOnLaunch,
      });
      facts = result.startup;
      revision = result.settings_revision;
      message = result.applied
        ? "桌面设置已保存，启动项已按当前意图应用。"
        : "桌面设置已保存，但启动项没有应用成功；下方事实说明了系统侧的实际情况。";
      await load();
      onchanged();
    } catch (error) {
      failure = describe(error);
    } finally {
      busy = false;
    }
  }

  async function repair(): Promise<void> {
    busy = true;
    message = null;
    failure = null;
    try {
      const result = await api.repairStartup(revision);
      facts = result.startup;
      message = result.applied ? "启动项已按当前程序路径重新登记。" : "修复没有生效，请看下方事实。";
      await load();
      onchanged();
    } catch (error) {
      failure = describe(error);
      await load();
    } finally {
      busy = false;
    }
  }

  function describe(error: unknown): string {
    if (error instanceof api.ApiError) {
      if (error.status === 401) return "会话已失效，请从桌面图标重新打开管理页。";
      if (error.detail) return error.detail;
      if (error.status === 409) return conflictNotice(error.code) ?? `操作冲突：${error.code}`;
      return `操作失败：${error.code}`;
    }
    return "操作失败；请查看近期事件。";
  }

  const stateNotice = $derived(facts ? startupStatusNotice(facts.effective_state) : null);
  const resultNotice = $derived(facts ? startupResultNotice(facts.last_apply_result) : undefined);
  const stateClass = $derived(
    facts?.effective_state === "enabled"
      ? "ok"
      : facts?.effective_state === "needs_repair"
        ? "warn"
        : facts?.effective_state === "disabled"
          ? ""
          : "bad",
  );
</script>

{#if settings}
  <div class="panel">
    <h2>登录启动</h2>
    <label class="checkbox">
      <input type="checkbox" bind:checked={launchAtSignIn} disabled={busy} />
      登录 Windows 时启动 Light
    </label>
    <p class="hint">
      只登记当前用户的启动项，不请求管理员权限、不写系统级位置。启用前会先检查程序路径与命令长度；
      登记完成后也无法确认 Windows 是否真的会执行它——系统设置里的禁用决定本程序读不到，也不覆盖。
    </p>
  </div>

  <div class="panel">
    <h2>打开 Light 时启动机器人</h2>
    <label class="checkbox">
      <input type="checkbox" bind:checked={startBotOnLaunch} disabled={busy} />
      打开 Light 时启动机器人
    </label>
    <p class="hint">
      只决定 Light 启动后是否一并启动机器人，与「登录 Windows 时启动 Light」相互独立；
      该偏好保存在数据目录的 <code>desktop.json</code>，不再写进档案配置。
    </p>
  </div>

  <div class="panel">
    <h2>启动目标档案</h2>
    <dl class="kv">
      <dt>当前目标</dt>
      <dd>{settings.startup_profile_id ?? "（未指定；跟随当前档案）"}</dd>
    </dl>
    <p class="hint">
      启动目标只影响登录启动时用哪个档案。档案选择与切换由「账号」页管理；
      这里显示当前保存的目标。
    </p>
  </div>

  <div class="panel">
    <h2>启动项状态</h2>
    {#if facts}
      {#if stateNotice}
        <p>
          <span class="state {stateClass}">{stateNotice.title}</span>
        </p>
        <p class="hint">{stateNotice.detail}</p>
      {/if}
      <dl class="kv">
        <dt>意图</dt>
        <dd>{facts.requested_enabled ? "开启" : "关闭"}</dd>
        <dt>注册表登记</dt>
        <dd>{facts.registration_present ? "有本产品同名的值" : "没有"}</dd>
        <dt>命令与当前路径一致</dt>
        <dd>{facts.command_matches ? "一致" : "不一致"}</dd>
        <dt>程序文件存在</dt>
        <dd>{facts.executable_exists ? "是" : "否"}</dd>
        <dt>上次应用结果</dt>
        <dd>{resultNotice ?? facts.last_apply_result}</dd>
        <dt>登记命令</dt>
        <dd>{facts.expected_command || "（当前形态下无法生成）"}</dd>
      </dl>
      {#if facts.divergence}
        <div class="error">意图与登记事实不一致；按下「修复启动项」会按当前程序路径重新登记。</div>
      {/if}
      <div class="row" style="margin-top:12px">
        <button class="action" disabled={busy} onclick={repair}>修复启动项</button>
      </div>
    {:else}
      <p class="hint">暂时读不到启动项状态；可稍后刷新，或查看「近期事件」。</p>
    {/if}
  </div>

  {#if message}<div class="notice">{message}</div>{/if}
  {#if failure}<div class="error">{failure}</div>{/if}

  <div class="row">
    <button class="action" disabled={busy} onclick={save}>保存桌面设置</button>
  </div>
{/if}
