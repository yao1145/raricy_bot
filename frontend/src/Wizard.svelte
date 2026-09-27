<script lang="ts">
  // 设置向导（设计 §5.1、§9）：首次设置与「添加账号 / 重新绑定」共用同一套表单。
  //
  // - `profileId` 为 null：首次设置。保存时先建立首个档案，再用一次性票据绑定身份
  //   —— N2 起身份只能经 `PUT /api/profiles/{id}/config` 携带票据写入（§59）。
  // - `profileId` 非 null：编辑指定档案（添加账号、重新绑定、还没配置的账号）。
  //   站点测试走 `POST .../verify`，保存走 `PUT .../config`，只提交改动过的字段，
  //   未读到的字段（例如已移除档案的旧配置）不会被空表单覆盖。
  //
  // 密码与 API Key 只存在于这一页的内存里，从不写进浏览器存储。
  import * as api from "./api";
  import { accountCodeNotice, VERIFY_STOP_CONFIRM, VERIFY_STOP_FAILED } from "./texts";
  import { toInput } from "./fields";

  let {
    profileId = null,
    ondone,
    oncancel = null,
  }: {
    profileId?: string | null;
    ondone: () => void;
    oncancel?: (() => void) | null;
  } = $props();

  const STEPS = 3;
  const bound = $derived(profileId !== null);

  let step = $state(1);
  let busy = $state(false);
  let failure = $state<string | null>(null);
  let message = $state<string | null>(null);
  let loading = $state(true);

  // 目标档案：绑定模式下就是传入的档案，首次设置在保存时建立；
  // 两条路径之后共用同一套「验证 → 写配置 → 切换」提交。
  let createdProfileId = $state<string | null>(null);
  const target = $derived(profileId ?? createdProfileId);
  let displayName = $state("");
  let profileRevision = $state(1);

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
  let verificationId = $state("");
  let credentialsChanged = $state(false);
  let usernameChanged = $state(false);
  let siteTestResult = $state<string | null>(null);
  let modelTestResult = $state<string | null>(null);
  let knownKbUserIds = $state<string[]>([]);
  let knownMemoryUserIds = $state<string[]>([]);
  let knownMemoryAdminUserIds = $state<string[]>([]);
  let knownKbChannelKinds = $state<string[]>(["dm"]);
  // 装载时的表单快照：绑定模式下只提交与它不同的字段（空表单不覆盖旧配置）。
  let baseline = $state<Record<string, unknown> | null>(null);
  let baselineAccount = "";
  let baselineDisplay = "";
  // 首次设置建立档案用的幂等键：失败重试复用同一个，避免建出两个档案。
  let createKey: string | null = null;

  const capabilityFields = [
    { key: "model.vision_enabled", label: "图片理解（模型需支持图片）" },
    { key: "comments.enabled", label: "博客评论" },
    { key: "knowledge_base.enabled", label: "本地知识库" },
    { key: "memory.enabled", label: "长期记忆" },
  ];

  function stringList(value: unknown): string[] {
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
  }

  function applyValues(value: (key: string) => unknown, defaultPrompt: string): void {
    systemPrompt = toInput(
      { key: "system_prompt", label: "", kind: "prompt", group: "simple" },
      value("system_prompt") ?? defaultPrompt,
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
  }

  $effect(() => {
    void (async () => {
      try {
        // 默认 System Prompt 模板与站点基线来自只读视图；绑定模式下不读活动档案的
        // 取值，避免把另一个账号的设置带进这个表单。
        const view = await api.getConfig();
        const defaultPrompt = String(view.defaults["system_prompt"] ?? "");
        if (!bound) {
          revision = view.revision ?? 0;
          account = view.account ?? "";
          originalAccount = account.trim();
          accountLocked = originalAccount !== "";
          const draft = await api.getDraft();
          const draftValues = draft.values ?? {};
          draftRevision = draft.revision;
          applyValues(
            (key) => (draftValues[key] == null ? view.values[key] : draftValues[key]),
            defaultPrompt,
          );
        } else {
          const [draft, list] = await Promise.all([
            api.getProfileDraft(profileId as string),
            api.getProfiles(),
          ]);
          const card = list.profiles.find((item) => item.profile_id === profileId) ?? null;
          if (!card) {
            failure = "找不到这个账号档案；请返回账号页刷新后重试。";
            loading = false;
            return;
          }
          displayName = card.display_name;
          profileRevision = card.profile_revision;
          revision = card.config.revision ?? 0;
          account = card.account ?? "";
          originalAccount = account.trim();
          accountLocked = originalAccount !== "";
          const draftValues = draft.values ?? {};
          draftRevision = draft.revision;
          // 草稿里没有的字段保持空白：提交时按「与装载快照不同」筛选，空值不会
          // 覆盖档案里已有的配置（页面读不到非活动档案的正式配置）。
          applyValues((key) => draftValues[key], defaultPrompt);
          baseline = collectedValues();
          baselineAccount = account.trim();
          baselineDisplay = displayName;
        }
        loading = false;
      } catch (error) {
        loading = false;
        failure =
          error instanceof api.ApiError && error.status === 401
            ? "会话已失效，请从桌面图标重新打开管理页。"
            : "无法读取配置；请从桌面图标重新打开管理页。";
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
    const values: Record<string, unknown> = { ...flags };
    // 空白字段等于「这次没填」：不提交，避免把档案里已有的值覆盖成空。
    if (modelUrl.trim() !== "") values["model.base_url"] = modelUrl.trim();
    if (modelName.trim() !== "") values["model.model"] = modelName.trim();
    if (systemPrompt.trim() !== "") values["system_prompt"] = systemPrompt;
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

  function changedValues(): Record<string, unknown> {
    const current = collectedValues();
    const before = baseline ?? {};
    const values: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(current)) {
      if (JSON.stringify(value) !== JSON.stringify(before[key])) values[key] = value;
    }
    return values;
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
    // 票据绑定精确的账号与密码：输入一变就作废，保存时会重新验证。
    verificationId = "";
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

  function seedIdentity(id: string): void {
    if (usernameChanged || !knownKbUserIds.length) knownKbUserIds = [id];
    if (usernameChanged || !knownMemoryUserIds.length) knownMemoryUserIds = [id];
    if (usernameChanged || !knownMemoryAdminUserIds.length) knownMemoryAdminUserIds = [id];
    if (usernameChanged) knownKbChannelKinds = ["dm"];
  }

  /** 验证要求机器人停止：运行中先征得同意，再停止并等到 stopped / failed。 */
  async function ensureBotStopped(): Promise<boolean> {
    const snapshot = await api.getStatus();
    const state = snapshot.status.process.state;
    if (state !== "running" && state !== "starting") return true;
    if (!confirm(VERIFY_STOP_CONFIRM)) return false;
    await api.botAction("stop");
    for (let attempt = 0; attempt < 60; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      const next = await api.getStatus();
      const now = next.status.process.state;
      if (now === "stopped" || now === "failed") return true;
    }
    failure = VERIFY_STOP_FAILED;
    return false;
  }

  async function testSite(): Promise<void> {
    busy = true;
    failure = null;
    siteTestResult = null;
    try {
      if (target) {
        // 绑定档案的验证：登录站点、确认稳定 ID 并签发票据（600 秒、一次性）。
        if (!(await ensureBotStopped())) {
          if (!failure) failure = "已取消：身份验证没有开始。";
          return;
        }
        const result = await api.verifyProfile(target, account.trim(), password);
        if (result.ok !== true || !result.verification_id) {
          verificationId = "";
          siteTestResult = `站点登录没有通过：${result.detail ?? "服务端没有给出可用的验证票据"}`;
          return;
        }
        verificationId = result.verification_id;
        siteUserId = result.site_user_id ?? "";
        credentialsChanged = false;
        usernameChanged = false;
        if (siteUserId) seedIdentity(siteUserId);
        siteTestResult =
          result.chat_ready === false
            ? `身份已验证（账号 ID：${siteUserId}），但聊天权限探测未通过；票据仍然有效。`
            : `身份验证通过（账号 ID：${siteUserId}）。`;
        return;
      }
      const result = await api.testSite({ username: account.trim(), password });
      if (result.account_id) {
        siteUserId = result.account_id;
        seedIdentity(siteUserId);
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
      const payload: Record<string, unknown> = { expected_revision: draftRevision, values };
      const saved = target
        ? await api.saveProfileDraft(target, { ...payload, expected_profile_revision: profileRevision })
        : await api.saveDraft(payload);
      draftRevision = saved.revision;
      message = `草稿已保存（revision ${saved.revision}）。`;
      if (target) baseline = collectedValues();
    } catch (error) {
      failure = describe(error);
    } finally {
      busy = false;
    }
  }

  async function saveLaunchPreference(): Promise<boolean> {
    // 桌面偏好（§58、§60）：启动偏好归 desktop.json，不再随配置提交。
    // 配置保存成功后档案已经存在：从 `/api/config` 读回 `profile_id`，连同
    // `start_bot_on_launch` 一起把启动目标写进 desktop.json（N1 起档案 id 可查）。
    // 读不到档案 id（仍是 null）时不写该字段，不猜。
    try {
      const current = await api.getDesktopSettings();
      const payload: Record<string, unknown> = {
        expected_settings_revision: current.settings_revision,
        start_bot_on_launch: true,
      };
      const view = await api.getConfig();
      if (view.profile_id) payload.startup_profile_id = view.profile_id;
      await api.saveDesktopSettings(payload);
      return true;
    } catch {
      // 配置已经保存：偏好没写进去只影响下次自动启动，不能把保存说成失败。
      return false;
    }
  }

  /** 建立首个档案（空数据根上它同时成为活动档案）；幂等键保证只建一次。 */
  async function ensureFirstProfile(): Promise<string> {
    if (target) return target;
    const list = await api.getProfiles();
    createKey ??= api.newIdempotencyKey("create");
    try {
      const created = await api.createProfile({
        display_name: displayName.trim(),
        expected_catalog_revision: list.catalog.catalog_revision,
        idempotency_key: createKey,
      });
      createKey = null;
      createdProfileId = created.profile_id;
      return created.profile_id;
    } catch (error) {
      if (error instanceof api.ApiError && error.code === "revision_conflict") createKey = null;
      throw error;
    }
  }

  /** 绑定档案的提交：验证身份（票据）→ 写配置 → 切换并启动（§59）。 */
  async function submitBound(): Promise<boolean> {
    const list = await api.getProfiles();
    const card = list.profiles.find((item) => item.profile_id === target) ?? null;
    if (!card) {
      failure = "找不到这个账号档案；请返回账号页刷新后重试。";
      return false;
    }
    let ticket = verificationId;
    if (!ticket) {
      if (!(await ensureBotStopped())) {
        failure = failure ?? "已取消：身份验证没有开始，配置没有保存。";
        return false;
      }
      const verified = await api.verifyProfile(target as string, account.trim(), password);
      if (verified.ok !== true || !verified.verification_id) {
        failure = `站点登录没有通过：${verified.detail ?? "服务端没有给出可用的验证票据"}`;
        return false;
      }
      ticket = verified.verification_id;
      verificationId = ticket;
      if (verified.site_user_id) {
        siteUserId = verified.site_user_id;
        seedIdentity(siteUserId);
      }
    }
    const credentials: Record<string, unknown> = {
      password: { action: "replace", value: password },
    };
    // 留空表示「保持不变」：省略该键（服务端默认 keep），不把已有 Key 覆盖成空。
    if (apiKey) credentials.llm_api_key = { action: "replace", value: apiKey };
    const payload: Record<string, unknown> = {
      expected_revision: card.config.revision ?? 0,
      expected_profile_revision: card.profile_revision,
      verification_id: ticket,
      values: baseline ? changedValues() : collectedValues(),
      credentials,
      account: account.trim(),
    };
    if (displayName.trim() !== baselineDisplay) payload.display_name = displayName.trim();
    let saved;
    try {
      saved = await api.saveProfileConfig(target as string, payload);
    } catch (error) {
      // 票据在 `commit()` 之前消费：这次提交失败（含版本冲突）也会让它作废，
      // 重试必须重新验证身份（§59 的已知取舍）。
      verificationId = "";
      throw error;
    }
    profileRevision = saved.profile_revision;
    revision = saved.revision;
    baseline = collectedValues();
    password = "";
    apiKey = "";
    verificationId = ""; // 票据已一次性消费
    const after = await api.getProfiles();
    const operation = await api.activateProfile(target as string, {
      expected_catalog_revision: after.catalog.catalog_revision,
      expected_epoch: after.catalog.active_epoch,
      start: true,
      idempotency_key: api.newIdempotencyKey("start"),
    });
    // 202 + operation_id 是唯一依据：短暂轮询，没结束就如实说「进行中」。
    const view = await api.waitForOperation(operation.operation_id, 20, 500);
    message = view.finished
      ? `配置已保存；切换${view.result === "started" ? "并启动完成" : `结束（${view.result ?? view.state}）`}。`
      : "配置已保存；切换仍在进行，请到账号页或状态页查看进度。";
    return true;
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
      if (!bound) await ensureFirstProfile();
      const saved = await submitBound();
      if (!saved) return;
      if (!bound && (await saveLaunchPreference()) === false) {
        message = "已保存并启动；但「打开 Light 时启动机器人」偏好没有写入，请在「桌面」页重试。";
      }
      ondone();
    } catch (error) {
      failure = describe(error);
    } finally {
      busy = false;
    }
  }

  function describe(error: unknown): string {
    if (error instanceof api.ApiError) {
      return (
        accountCodeNotice(error.code) ??
        (error.field ? `字段有问题：${error.field}（${error.code}）` : `保存失败：${error.code}`)
      );
    }
    return "保存失败；请稍后再试。";
  }
</script>

<div class="panel">
  <h2>
    {target ? `填写账号设置 · ${displayName || account || "未命名账号"}` : `首次设置 · 第 ${step} / ${STEPS} 步`}
  </h2>
  <div class="wizard-progress" aria-label={`设置进度：第 ${step} 步，共 ${STEPS} 步`}>
    <span class:current={step === 1} class:complete={step > 1}>站点身份</span>
    <span class:current={step === 2} class:complete={step > 2}>模型连接</span>
    <span class:current={step === 3}>可选能力</span>
  </div>
  {#if loading}
    <p class="hint">正在读取配置…</p>
  {/if}

  {#if step === 1}
    <div class="grid">
      <label>本地标签（只用于区分这台机器上的账号）
        <input bind:value={displayName} disabled={busy} />
      </label>
      <label>站点账号
        <input
          bind:value={account}
          oninput={invalidateUsernameIdentity}
          disabled={accountLocked || busy}
          autocomplete="username"
        />
      </label>
      <label>站点密码
        <input type="password" bind:value={password} oninput={invalidateSiteIdentity} disabled={busy} autocomplete="current-password" />
      </label>
    </div>
    <p class="hint">
      密码只保存进系统凭据库；如果系统凭据库不可用，则只在本次运行内有效（设置页会标明）。
    </p>
    {#if accountLocked}
      <p class="hint">
        账号已绑定此档案；要更换站点账号，请回到账号页添加一个新账号。
      </p>
    {/if}
    <div class="row">
      <button class="ghost" disabled={busy || !stepReady(1)} onclick={testSite}>
        {target ? "验证身份" : "测试站点"}
      </button>
      {#if siteTestResult}<span class="hint">{siteTestResult}</span>{/if}
      {#if verificationId}<span class="hint">（已有一次性验证票据，保存时会消费）</span>{/if}
    </div>
  {:else if step === 2}
    <div class="grid">
      <label>模型 API 地址
        <input bind:value={modelUrl} placeholder="https://example.com/v1" disabled={busy} />
      </label>
      <label>模型名称
        <input bind:value={modelName} disabled={busy} />
      </label>
      <label>模型 API Key
        <input type="password" bind:value={apiKey} disabled={busy} autocomplete="off" />
      </label>
    </div>
    <label>System Prompt
      <textarea bind:value={systemPrompt} disabled={busy}></textarea>
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
      {#if target && flags["knowledge_base.enabled"] && knownKbUserIds.length === 0}
        <br />这个档案的可访问名单读不出来（还没有草稿或配置）；先完成验证身份，名单会按本次登录的稳定 ID 写入。
      {/if}
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
    {#if oncancel}
      <button class="ghost" disabled={busy} onclick={oncancel}>取消</button>
    {/if}
  </div>
</div>
