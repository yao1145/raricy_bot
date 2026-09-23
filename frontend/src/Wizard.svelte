<script lang="ts">
  // 首次向导（设计 §5.1）：填账号与模型、选能力、保存并启动。
  // 密码与 API Key 只存在于这一页的内存里，从不写进浏览器存储。
  import * as api from "./api";
  import { toInput } from "./fields";

  let { ondone }: { ondone: () => void } = $props();

  const STEPS = 3;

  let step = $state(1);
  let busy = $state(false);
  let failure = $state<string | null>(null);
  let message = $state<string | null>(null);

  let account = $state("");
  let originalAccount = "";
  let accountLocked = $state(false);
  let password = $state("");
  let apiKey = $state("");
  let modelUrl = $state("");
  let modelName = $state("");
  let systemPrompt = $state("");
  let flags = $state<Record<string, boolean>>({
    "model.vision_enabled": false,
    "comments.enabled": false,
    "knowledge_base.enabled": false,
    "memory.enabled": false,
  });
  let revision = $state(0); // 已保存配置的 revision（needs_credentials 时可能 > 0）
  let draftRevision = $state(0);
  let siteUserId = $state("");
  let credentialsChanged = $state(false);
  let usernameChanged = $state(false);
  let siteTestResult = $state<string | null>(null);
  let modelTestResult = $state<string | null>(null);
  let knownKbUserIds = $state<string[]>([]);
  let knownMemoryUserIds = $state<string[]>([]);
  let knownMemoryAdminUserIds = $state<string[]>([]);
  let knownKbChannelKinds = $state<string[]>(["dm"]);

  const capabilityFields = [
    { key: "model.vision_enabled", label: "图片理解（模型需支持图片）" },
    { key: "comments.enabled", label: "博客评论" },
    { key: "knowledge_base.enabled", label: "本地知识库" },
    { key: "memory.enabled", label: "长期记忆" },
  ];

  $effect(() => {
    void (async () => {
      try {
        const view = await api.getConfig();
        revision = view.revision ?? 0;
        account = view.account ?? "";
        originalAccount = account.trim();
        accountLocked = originalAccount !== "";
        const draft = await api.getDraft();
        const draftValues = draft.values ?? {};
        const value = (key: string): unknown =>
          draftValues[key] == null ? view.values[key] : draftValues[key];
        systemPrompt = toInput(
          { key: "system_prompt", label: "", kind: "prompt", group: "simple" },
          value("system_prompt") ?? view.defaults["system_prompt"],
        );
        modelUrl = toInput(
          { key: "model.base_url", label: "", kind: "text", group: "simple" },
          value("model.base_url"),
        );
        modelName = toInput(
          { key: "model.model", label: "", kind: "text", group: "simple" },
          value("model.model"),
        );
        for (const item of capabilityFields) {
          flags[item.key] = value(item.key) === true;
        }
        knownKbUserIds = stringList(value("knowledge_base.allowed_user_ids"));
        knownMemoryUserIds = stringList(value("memory.allow_user_list"));
        knownMemoryAdminUserIds = stringList(value("memory.admin_user_list"));
        knownKbChannelKinds = stringList(value("knowledge_base.allowed_channel_kinds"));
        draftRevision = draft.revision;
      } catch {
        failure = "无法读取配置；请从桌面图标重新打开管理页。";
      }
    })();
  });

  function stepReady(current: number): boolean {
    if (current === 1) return account.trim() !== "" && password.trim() !== "";
    if (current === 2)
      return modelUrl.trim() !== "" && modelName.trim() !== "" && apiKey.trim() !== "";
    return true;
  }

  function collectedValues(): Record<string, unknown> {
    const values: Record<string, unknown> = {
      "model.base_url": modelUrl.trim(),
      "model.model": modelName.trim(),
      "system_prompt": systemPrompt,
      ...flags,
    };
    const kbUserIds = credentialsChanged ? [] : knownKbUserIds;
    const memoryUserIds = credentialsChanged ? [] : knownMemoryUserIds;
    values["knowledge_base.allowed_channel_kinds"] = knownKbChannelKinds.length
      ? knownKbChannelKinds
      : ["dm"];
    if (kbUserIds.length) values["knowledge_base.allowed_user_ids"] = kbUserIds;
    if (flags["knowledge_base.enabled"] && !kbUserIds.length) {
      values["knowledge_base.enabled"] = false;
    }
    if (memoryUserIds.length) {
      values["memory.allow_user_list"] = memoryUserIds;
      values["memory.admin_user_list"] = knownMemoryAdminUserIds;
    }
    if (flags["memory.enabled"] && memoryUserIds.length) {
      values["memory.access_mode"] = "allowlist";
    } else if (flags["memory.enabled"]) {
      values["memory.enabled"] = false;
    }
    return values;
  }

  function stringList(value: unknown): string[] {
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
  }

  function identityAvailable(key: string): boolean {
    if (key !== "knowledge_base.enabled" && key !== "memory.enabled") return true;
    if (siteUserId) return true;
    if (credentialsChanged) return false;
    return key === "knowledge_base.enabled"
      ? knownKbUserIds.length > 0
      : knownMemoryUserIds.length > 0;
  }

  function invalidateSiteIdentity(): void {
    credentialsChanged = true;
    siteUserId = "";
    siteTestResult = null;
  }

  function invalidateUsernameIdentity(): void {
    usernameChanged = account.trim() !== originalAccount;
    invalidateSiteIdentity();
  }

  function requiresIdentityRetest(): boolean {
    return credentialsChanged && !siteUserId &&
      (flags["knowledge_base.enabled"] || flags["memory.enabled"]);
  }

  async function testSite(): Promise<void> {
    busy = true;
    failure = null;
    siteTestResult = null;
    try {
      const result = await api.testSite({ username: account.trim(), password });
      if (result.account_id) {
        if (usernameChanged) {
          // 登录名变化意味着机器人身份可能变化，移除旧账号的默认授权 ID。
          knownKbUserIds = [result.account_id];
          knownMemoryUserIds = [result.account_id];
          knownMemoryAdminUserIds = [result.account_id];
          knownKbChannelKinds = ["dm"];
        } else if (!knownKbUserIds.length) {
          knownKbUserIds = [result.account_id];
        }
        if (usernameChanged || !knownMemoryUserIds.length) {
          knownMemoryUserIds = [result.account_id];
        }
        if (usernameChanged || !knownMemoryAdminUserIds.length) {
          knownMemoryAdminUserIds = [result.account_id];
        }
        siteUserId = result.account_id;
        credentialsChanged = false;
        usernameChanged = false;
      }
      siteTestResult = result.ok
        ? `站点登录与聊天权限测试通过（账号 ID：${siteUserId}）。`
        : siteUserId
          ? `已登录（账号 ID：${siteUserId}），聊天权限测试未通过：${result.detail}`
          : `站点测试未通过：${result.detail}`;
    } catch (error) {
      failure = describe(error);
    } finally {
      busy = false;
    }
  }

  async function testModel(): Promise<void> {
    busy = true;
    failure = null;
    modelTestResult = null;
    try {
      const result = await api.testModel({
        base_url: modelUrl.trim(),
        model: modelName.trim(),
        api_key: apiKey,
      });
      modelTestResult = result.ok
        ? "模型连接测试通过。"
        : `模型测试未通过：${result.detail}`;
    } catch (error) {
      failure = describe(error);
    } finally {
      busy = false;
    }
  }

  async function saveDraft(): Promise<void> {
    // 草稿只保存非敏感字段，允许还没填完（§6.2）；revision 用草稿自己的。
    busy = true;
    failure = null;
    message = null;
    if (requiresIdentityRetest()) {
      failure = "账号或密码已修改；请先测试站点，或关闭知识库与记忆后保存草稿。";
      busy = false;
      return;
    }
    try {
      const values = collectedValues();
      delete values["system_prompt"]; // 提示词属正式配置；草稿不藏正文
      const saved = await api.saveDraft({ expected_revision: draftRevision, values });
      draftRevision = saved.revision;
      message = `草稿已保存（revision ${saved.revision}）。`;
    } catch (error) {
      failure = describe(error);
    } finally {
      busy = false;
    }
  }

  async function saveAndStart(): Promise<void> {
    busy = true;
    failure = null;
    message = null;
    if (requiresIdentityRetest()) {
      failure = "账号或密码已修改；请先测试站点，或关闭知识库与记忆后保存。";
      busy = false;
      return;
    }
    try {
      const result = await api.saveConfig({
        expected_revision: revision,
        values: collectedValues(),
        account: account.trim(),
        start_bot_on_launch: true,
        credentials: {
          password: { action: "replace", value: password },
          llm_api_key: { action: "replace", value: apiKey },
        },
      });
      revision = result.revision;
      await api.botAction("start", result.revision);
      message = "已保存并启动；机器人正在初始化。";
      ondone();
    } catch (error) {
      failure = describe(error);
    } finally {
      busy = false;
    }
  }

  function describe(error: unknown): string {
    if (error instanceof api.ApiError) {
      if (error.status === 409) return "配置已被另一个页面改过，请刷新后重试。";
      if (error.field) return `字段有问题：${error.field}（${error.code}）`;
      return `保存失败：${error.code}`;
    }
    return "保存失败；请稍后再试。";
  }
</script>

<div class="panel">
  <h2>首次设置 · 第 {step} / {STEPS} 步</h2>

  {#if step === 1}
    <div class="grid">
      <label>站点账号
        <input
          bind:value={account}
          oninput={invalidateUsernameIdentity}
          disabled={accountLocked}
          autocomplete="username"
        />
      </label>
      <label>站点密码
        <input type="password" bind:value={password} oninput={invalidateSiteIdentity} autocomplete="current-password" />
      </label>
    </div>
    <p class="hint">
      密码只保存进系统凭据库；如果系统凭据库不可用，则只在本次运行内有效（设置页会标明）。
    </p>
    {#if accountLocked}
      <p class="hint">账号已绑定此档案；要更换站点账号，请重新设置并新建档案。</p>
    {/if}
    <div class="row">
      <button class="ghost" disabled={busy || !stepReady(1)} onclick={testSite}>测试站点</button>
      {#if siteTestResult}<span class="hint">{siteTestResult}</span>{/if}
    </div>
  {:else if step === 2}
    <div class="grid">
      <label>模型 API 地址
        <input bind:value={modelUrl} placeholder="https://example.com/v1" />
      </label>
      <label>模型名称
        <input bind:value={modelName} />
      </label>
      <label>模型 API Key
        <input type="password" bind:value={apiKey} autocomplete="off" />
      </label>
    </div>
    <label>System Prompt
      <textarea bind:value={systemPrompt}></textarea>
    </label>
    <p class="hint">图片与对话内容会送往这个模型服务；提示词正文可在设置里随时修改。</p>
    <div class="row">
      <button class="ghost" disabled={busy || !stepReady(2)} onclick={testModel}>测试模型</button>
      {#if modelTestResult}<span class="hint">{modelTestResult}</span>{/if}
    </div>
  {:else}
    <div class="grid">
      {#each capabilityFields as item (item.key)}
        <label class="checkbox">
          <input
            type="checkbox"
            bind:checked={flags[item.key]}
            disabled={busy || (!flags[item.key] && !identityAvailable(item.key))}
          />
          {item.label}
        </label>
      {/each}
    </div>
    <p class="hint">
      测试站点取得稳定账号 ID 后，可将知识库与记忆限制为本人私聊；登录用户名不会用作权限 ID。
    </p>
  {/if}

  {#if failure}<div class="error">{failure}</div>{/if}
  {#if message}<div class="notice">{message}</div>{/if}

  <div class="row" style="margin-top:16px">
    <button class="action" disabled={step === 1 || busy} onclick={() => (step -= 1)}>上一步</button>
    {#if step < STEPS}
      <button class="action" disabled={!stepReady(step) || busy} onclick={() => (step += 1)}>
        下一步
      </button>
      <button class="ghost" disabled={busy} onclick={saveDraft}>保存草稿</button>
    {:else}
      <button class="action" disabled={busy || !stepReady(1) || !stepReady(2)} onclick={saveAndStart}>
        保存并启动
      </button>
    {/if}
  </div>
</div>
