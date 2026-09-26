<script lang="ts">
  // 账号页（N2 §9、§59、§60）：账号卡片列表与三个互不相同的动作入口
  // —— 停止机器人（在状态页）、清除保存的凭据、移除账号。
  //
  // 页面不自己推断可行动作：按钮一律按服务端 `actions` 渲染；异步操作只认
  // 202 + operation_id，用 `GET /api/operations/{id}` 轮询，不在前端假装同步。
  import * as api from "./api";
  import {
    ACTIVATE_RESULTS,
    CONFIG_STATE_LABELS,
    CREDENTIALS_CLEANUP_PENDING,
    CREDENTIALS_CLEAR_CONFIRM,
    CREDENTIALS_CLEAR_RESULTS,
    CREDENTIALS_UNKNOWN_OWNERSHIP,
    CREDENTIAL_KIND_LABELS,
    IDENTITY_LABELS,
    PROFILE_ACTION_LABELS,
    PROFILE_STATE_LABELS,
    REMOVAL_CATEGORY_LABELS,
    REMOVAL_CLEANUP_PENDING,
    REMOVAL_CONFIRM_KEEP,
    REMOVAL_CONFIRM_PURGE,
    REMOVAL_IN_PROGRESS,
    REMOVAL_IRREVERSIBLE,
    REMOVAL_RESULTS,
    REMOVAL_RETRY_HINT,
    REMOVAL_SCOPE_DETAILS,
    REMOVAL_SCOPE_LABELS,
    REMOVAL_SIZE_INCOMPLETE,
    REMOVAL_STAGE_LABELS,
    REMOVAL_UNKNOWN_CREDENTIALS,
    REBIND_HINT,
    VERIFY_CHAT_NOTICE,
    VERIFY_HINT,
    VERIFY_SAVED,
    VERIFY_STOP_CONFIRM,
    VERIFY_STOP_FAILED,
    accountCodeNotice,
  } from "./texts";

  let {
    status,
    profiles,
    catalog,
    onchanged,
    onedit,
    onwizard,
  }: {
    status: api.StatusSnapshot | null;
    profiles: api.ProfileCard[];
    catalog: api.ProfileCatalog | null;
    onchanged: () => void;
    /** 切到「当前账号设置」页签（编辑按钮）。 */
    onedit: () => void;
    /** 打开绑定到该档案的向导（添加账号与「还没有配置」的验证入口）。 */
    onwizard: (profileId: string) => void;
  } = $props();

  let busy = $state(false);
  let message = $state<string | null>(null);
  let failure = $state<string | null>(null);
  // 创建请求的幂等键：网络失败后重试要复用同一个键，避免建出两个档案。
  let createKey: string | null = null;

  // 一次只开一个二级对话框；每个对话框自带待提交输入，切换档案时由 App 的
  // `{#key}` 重建组件，旧密码与待提交动作随之作废。
  let verify = $state<null | {
    profileId: string;
    label: string;
    mode: "verify" | "rebind";
    account: string;
    password: string;
    apiKey: string;
    busy: boolean;
    failure: string | null;
  }>(null);

  let clearing = $state<null | {
    profileId: string;
    label: string;
    kinds: string[];
    confirmed: boolean;
    busy: boolean;
    failure: string | null;
  }>(null);

  let removal = $state<null | {
    profileId: string;
    label: string;
    scope: string;
    preview: api.RemovalPreview | null;
    token: string | null;
    confirmed: boolean;
    busy: boolean;
    failure: string | null;
    operation: api.OperationView | null;
  }>(null);

  const processState = $derived(status?.process.state ?? "unknown");
  const botRunning = $derived(processState === "running" || processState === "starting");

  function describe(error: unknown): string {
    if (error instanceof api.ApiError) {
      if (error.status === 401) return "会话已失效，请从桌面图标重新打开管理页。";
      // 固定文案集中在 texts.ts；未命中的码才回退到服务端 message 或原始码。
      return accountCodeNotice(error.code) ?? error.detail ?? `操作被拒绝：${error.code}`;
    }
    return "操作失败；请查看近期事件里的固定事件码。";
  }

  function cardOf(profileId: string): api.ProfileCard | null {
    return profiles.find((card) => card.profile_id === profileId) ?? null;
  }

  function label(card: { display_name: string; account: string | null }): string {
    return card.display_name || card.account || "未命名账号";
  }

  function formatSize(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KiB", "MiB", "GiB", "TiB"];
    let value = bytes / 1024;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${value.toFixed(1)} ${units[unit]}`;
  }

  async function addAccount(): Promise<void> {
    if (!catalog) return;
    busy = true;
    message = null;
    failure = null;
    createKey ??= api.newIdempotencyKey("create");
    try {
      const body = await api.createProfile({
        expected_catalog_revision: catalog.catalog_revision,
        idempotency_key: createKey,
      });
      createKey = null;
      onchanged();
      onwizard(body.profile_id);
    } catch (error) {
      failure = describe(error);
      if (error instanceof api.ApiError && error.code === "revision_conflict") {
        // 目录已经被别处改过：下一次点击要用新修订与新幂等键，不能复用旧请求摘要。
        createKey = null;
      }
    } finally {
      busy = false;
    }
  }

  async function activate(card: api.ProfileCard, start: boolean): Promise<void> {
    if (!catalog) return;
    busy = true;
    message = null;
    failure = null;
    try {
      const body = await api.activateProfile(card.profile_id, {
        expected_catalog_revision: catalog.catalog_revision,
        expected_epoch: catalog.active_epoch,
        start,
        idempotency_key: api.newIdempotencyKey(start ? "start" : "select"),
      });
      const operation = await api.waitForOperation(body.operation_id);
      message = ACTIVATE_RESULTS[operation.result ?? ""] ?? `操作已结束（${operation.result ?? operation.state}）。`;
      onchanged();
    } catch (error) {
      failure = describe(error);
    } finally {
      busy = false;
    }
  }

  function beginEdit(card: api.ProfileCard): void {
    message = null;
    failure = null;
    if (!card.is_active) {
      failure = "这个账号不是当前选中的档案；请先「选中」它，再到「设置」页编辑，避免把改动写到别的账号上。";
      return;
    }
    onedit();
  }

  function openVerify(card: api.ProfileCard, mode: "verify" | "rebind"): void {
    message = null;
    failure = null;
    if (card.config.revision === null) {
      // 还没有保存过配置：先到向导里填写模型与服务字段，再在向导第 1 步验证身份。
      onwizard(card.profile_id);
      return;
    }
    verify = {
      profileId: card.profile_id,
      label: label(card),
      mode,
      account: card.account ?? "",
      password: "",
      apiKey: "",
      busy: false,
      failure: null,
    };
  }

  /** 身份验证要求机器人停止：运行中先征得同意，再停止并等到 stopped / failed。 */
  async function ensureStopped(): Promise<"ready" | "cancelled" | "timeout"> {
    if (!botRunning) return "ready";
    if (!confirm(VERIFY_STOP_CONFIRM)) return "cancelled";
    await api.botAction("stop");
    for (let attempt = 0; attempt < 60; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      const snapshot = await api.getStatus();
      const state = snapshot.status.process.state;
      if (state === "stopped" || state === "failed") return "ready";
    }
    return "timeout";
  }

  async function submitVerify(): Promise<void> {
    const dialog = verify;
    if (!dialog) return;
    const card = cardOf(dialog.profileId);
    if (!card) {
      dialog.failure = "账号列表已经变化；请关闭对话框后刷新重试。";
      return;
    }
    dialog.busy = true;
    dialog.failure = null;
    message = null;
    failure = null;
    try {
      const stopped = await ensureStopped();
      if (stopped !== "ready") {
        dialog.failure =
          stopped === "cancelled" ? "已取消：身份验证没有开始。" : VERIFY_STOP_FAILED;
        dialog.busy = false;
        return;
      }
      const result = await api.verifyProfile(
        dialog.profileId,
        dialog.account.trim(),
        dialog.password,
      );
      if (result.ok !== true || !result.verification_id) {
        dialog.failure = `站点登录没有通过：${result.detail ?? "服务端没有给出可用的验证票据"}`;
        dialog.busy = false;
        return;
      }
      const credentials: Record<string, unknown> = {
        password: { action: "replace", value: dialog.password },
      };
      // 留空表示「保持不变」：省略该键（服务端默认 keep），不把已有 Key 覆盖成空。
      if (dialog.apiKey) credentials.llm_api_key = { action: "replace", value: dialog.apiKey };
      await api.saveProfileConfig(dialog.profileId, {
        expected_revision: card.config.revision ?? 0,
        expected_profile_revision: card.profile_revision,
        verification_id: result.verification_id,
        values: {},
        credentials,
        account: dialog.account.trim(),
      });
      message = result.chat_ready === false ? `${VERIFY_SAVED} ${VERIFY_CHAT_NOTICE}` : VERIFY_SAVED;
      verify = null;
      onchanged();
    } catch (error) {
      dialog.failure = describe(error);
    } finally {
      dialog.busy = false;
    }
  }

  function openClear(card: api.ProfileCard): void {
    message = null;
    failure = null;
    clearing = {
      profileId: card.profile_id,
      label: label(card),
      kinds: [],
      confirmed: false,
      busy: false,
      failure: null,
    };
  }

  function toggleKind(kind: string, checked: boolean): void {
    const dialog = clearing;
    if (!dialog) return;
    const kinds = new Set(dialog.kinds);
    if (checked) kinds.add(kind);
    else kinds.delete(kind);
    dialog.kinds = [...kinds];
  }

  async function submitClear(): Promise<void> {
    const dialog = clearing;
    if (!dialog || dialog.kinds.length === 0) return;
    dialog.busy = true;
    dialog.failure = null;
    message = null;
    try {
      // 密码与模型 Key 是两个类别：kinds 是它们的非空子集（§59）。
      const body = await api.clearCredentials(dialog.profileId, {
        kinds: dialog.kinds,
        idempotency_key: api.newIdempotencyKey("clear"),
      });
      const operation = await api.waitForOperation(body.operation_id);
      message =
        CREDENTIALS_CLEAR_RESULTS[operation.result ?? ""] ??
        `清除已结束（${operation.result ?? operation.state}）。`;
      clearing = null;
      onchanged();
    } catch (error) {
      dialog.failure = describe(error);
    } finally {
      dialog.busy = false;
    }
  }

  async function openRemoval(card: api.ProfileCard, scope: string): Promise<void> {
    message = null;
    failure = null;
    removal = {
      profileId: card.profile_id,
      label: label(card),
      scope,
      preview: null,
      token: null,
      confirmed: false,
      busy: true,
      failure: null,
      operation: null,
    };
    await loadPreview();
  }

  /** 生成（或重新生成）预览：令牌一次性、绑定 scope 与两个 revision（§59）。 */
  async function loadPreview(): Promise<void> {
    const dialog = removal;
    if (!dialog) return;
    dialog.busy = true;
    dialog.failure = null;
    dialog.confirmed = false;
    dialog.token = null;
    dialog.operation = null;
    try {
      const body = await api.removalPreview(dialog.profileId, dialog.scope);
      dialog.preview = body.preview;
      dialog.token = body.confirmation_token;
    } catch (error) {
      dialog.preview = null;
      dialog.failure = describe(error);
    } finally {
      dialog.busy = false;
    }
  }

  async function changeScope(scope: string): Promise<void> {
    const dialog = removal;
    if (!dialog || scope === dialog.scope) return;
    // 参数（scope）变了就重新预览：旧令牌只对旧参数有效。
    dialog.scope = scope;
    await loadPreview();
  }

  async function submitRemoval(): Promise<void> {
    const dialog = removal;
    if (!dialog || !dialog.token || !dialog.confirmed) return;
    dialog.busy = true;
    dialog.failure = null;
    message = null;
    try {
      const body = await api.removeProfile(dialog.profileId, {
        scope: dialog.scope,
        confirmation_token: dialog.token,
        idempotency_key: api.newIdempotencyKey("remove"),
      });
      // 令牌在服务端已经消耗：无论结果如何都不能再次提交同一份确认。
      dialog.token = null;
      dialog.operation = await api.waitForOperation(body.operation_id);
      onchanged();
    } catch (error) {
      dialog.failure = describe(error);
      if (
        error instanceof api.ApiError &&
        ["removal_preview_stale", "removal_token_invalid", "profile_revision_conflict"].includes(
          error.code,
        )
      ) {
        dialog.token = null;
        dialog.confirmed = false;
      }
    } finally {
      dialog.busy = false;
    }
  }

  const removalResult = $derived(
    removal?.operation
      ? (REMOVAL_RESULTS[removal.operation.result ?? ""] ??
        `删除已结束（${removal.operation.result ?? removal.operation.state}）。`)
      : null,
  );
  const removalStage = $derived(
    removal?.operation?.stage ? (REMOVAL_STAGE_LABELS[removal.operation.stage] ?? removal.operation.stage) : null,
  );
</script>

<div class="panel">
  <h2>账号</h2>
  <p class="hint">
    每个账号一个本地档案。停止机器人、清除保存的凭据、移除账号是三件不同的事情，各有自己的按钮。
  </p>
  <div class="row">
    <button class="action" disabled={busy || !catalog} onclick={addAccount}>添加账号</button>
    <span class="hint">
      添加会先建立一个空档案，再打开向导填写这个账号的站点身份与模型设置。
    </span>
  </div>
</div>

{#if message}<div class="notice">{message}</div>{/if}
{#if failure}<div class="error">{failure}</div>{/if}

{#each profiles as card (card.profile_id)}
  <div class="panel" class:current={card.is_active}>
    <h2>{label(card)}</h2>
    <dl class="kv">
      <dt>标签</dt>
      <dd>{card.display_name || "（未设置）"}</dd>
      <dt>账号</dt>
      <dd>{card.account ?? "（未填写）"}</dd>
      <dt>稳定 ID</dt>
      <dd>{card.site_user_id ?? "（未验证）"}</dd>
      <dt>状态</dt>
      <dd>
        <span class="state" class:ok={card.state === "active"} class:warn={card.state === "deleting"} class:bad={card.state === "detached"}>
          {PROFILE_STATE_LABELS[card.state] ?? card.state}
        </span>
        <span class="state" class:ok={card.identity_state === "verified"} class:warn={card.identity_state !== "verified"}>
          {IDENTITY_LABELS[card.identity_state] ?? card.identity_state}
        </span>
        <span class="state" class:ok={card.config.state === "configured"} class:warn={card.config.state !== "configured"}>
          {CONFIG_STATE_LABELS[card.config.state] ?? card.config.state}
        </span>
      </dd>
      <dt>标记</dt>
      <dd>
        {#if card.is_active}<span class="state ok">当前选中</span>{/if}
        {#if card.is_running}<span class="state ok">运行中</span>{/if}
        {#if card.is_startup_target}<span class="state warn">启动目标</span>{/if}
        {#if !card.is_active && !card.is_running && !card.is_startup_target}
          <span class="hint">没有选中、运行或启动目标标记</span>
        {/if}
      </dd>
      <dt>凭据</dt>
      <dd>
        <span class="hint">
          后端 {card.credentials.backend.name}{card.credentials.backend.available ? "（可用）" : "（不可用：只在本次运行内有效）"}
          · 受管历史引用 {card.credentials.historical_managed} 条
        </span>
      </dd>
    </dl>

    {#if card.credentials.cleanup_pending}
      <div class="error">
        {CREDENTIALS_CLEANUP_PENDING}
        {#if card.actions.includes("clear_credentials")}
          <button class="ghost" onclick={() => openClear(card)}>重试清除</button>
        {/if}
      </div>
    {/if}
    {#if card.credentials.unknown_ownership}
      <div class="error">{CREDENTIALS_UNKNOWN_OWNERSHIP}</div>
    {/if}
    {#if card.state === "deleting"}
      <div class="error">{REMOVAL_RETRY_HINT}</div>
    {/if}

    <div class="row" style="margin-top:12px">
      {#each card.actions as action (action)}
        {#if action === "edit"}
          <button class="action" disabled={busy} onclick={() => beginEdit(card)}>
            {PROFILE_ACTION_LABELS[action] ?? action}
          </button>
        {:else if action === "verify"}
          <button class="action" disabled={busy} onclick={() => openVerify(card, "verify")}>
            {PROFILE_ACTION_LABELS[action] ?? action}
          </button>
        {:else if action === "rebind"}
          <button class="action" disabled={busy} onclick={() => openVerify(card, "rebind")}>
            {PROFILE_ACTION_LABELS[action] ?? action}
          </button>
        {:else if action === "clear_credentials"}
          <button class="action danger" disabled={busy} onclick={() => openClear(card)}>
            {PROFILE_ACTION_LABELS[action] ?? action}
          </button>
        {:else if action === "activate"}
          <button class="action" disabled={busy} onclick={() => activate(card, false)}>
            {PROFILE_ACTION_LABELS[action] ?? action}
          </button>
        {:else if action === "activate_and_start"}
          <button class="action" disabled={busy} onclick={() => activate(card, true)}>
            {PROFILE_ACTION_LABELS[action] ?? action}
          </button>
        {:else if action === "remove"}
          <button class="action danger" disabled={busy} onclick={() => openRemoval(card, "keep_data")}>
            {PROFILE_ACTION_LABELS[action] ?? action}
          </button>
        {:else if action === "purge"}
          <button class="action danger" disabled={busy} onclick={() => openRemoval(card, "purge_data")}>
            {PROFILE_ACTION_LABELS[action] ?? action}
          </button>
        {/if}
      {/each}
    </div>
  </div>
{/each}

{#if profiles.length === 0}
  <div class="panel">
    <p class="hint">还没有账号档案。用上面的「添加账号」建立一个，或让首次设置向导创建第一个账号。</p>
  </div>
{/if}

{#if verify}
  <div class="panel dialog">
    <h2>{verify.mode === "rebind" ? "重新绑定账号" : "验证身份"} · {verify.label}</h2>
    <p class="hint">{verify.mode === "rebind" ? REBIND_HINT : VERIFY_HINT}</p>
    {#if botRunning}
      <div class="error">机器人正在运行；提交后需要先停止它，验证完成不会自动重启。</div>
    {/if}
    <div class="grid">
      <label>站点账号
        <input
          bind:value={verify.account}
          disabled={verify.mode === "rebind"}
          autocomplete="username"
        />
      </label>
      <label>站点密码
        <input type="password" bind:value={verify.password} autocomplete="current-password" />
      </label>
      <label>模型 Key（留空表示保持不变；被清除或要替换时填写）
        <input type="password" bind:value={verify.apiKey} autocomplete="off" />
      </label>
    </div>
    {#if verify.failure}<div class="error">{verify.failure}</div>{/if}
    <div class="row" style="margin-top:12px">
      <button
        class="action"
        disabled={verify.busy || verify.account.trim() === "" || verify.password === ""}
        onclick={submitVerify}
      >
        验证并保存
      </button>
      <button class="ghost" disabled={verify.busy} onclick={() => (verify = null)}>取消</button>
    </div>
  </div>
{/if}

{#if clearing}
  <div class="panel dialog">
    <h2>清除保存的凭据 · {clearing.label}</h2>
    <p class="hint">{CREDENTIALS_CLEAR_CONFIRM}</p>
    <div class="grid">
      {#each Object.keys(CREDENTIAL_KIND_LABELS) as kind (kind)}
        <label class="checkbox">
          <input
            type="checkbox"
            checked={clearing.kinds.includes(kind)}
            disabled={clearing.busy}
            onchange={(event) => toggleKind(kind, event.currentTarget.checked)}
          />
          {CREDENTIAL_KIND_LABELS[kind]}
        </label>
      {/each}
    </div>
    <label class="checkbox">
      <input type="checkbox" bind:checked={clearing.confirmed} />
      我确认清除所选凭据，并明白没有撤销；清除后这个账号需要重新填写凭据才能启动。
    </label>
    {#if clearing.failure}<div class="error">{clearing.failure}</div>{/if}
    <div class="row" style="margin-top:12px">
      <button
        class="action danger"
        disabled={clearing.busy || !clearing.confirmed || clearing.kinds.length === 0}
        onclick={submitClear}
      >
        清除凭据
      </button>
      <button class="ghost" disabled={clearing.busy} onclick={() => (clearing = null)}>取消</button>
    </div>
  </div>
{/if}

{#if removal}
  <div class="panel dialog">
    <h2>{removal.scope === "purge_data" ? "彻底删除" : "移除账号"} · {removal.label}</h2>

    {#if removal.busy && !removal.preview}
      <p class="hint">正在生成预览（只读，不会删除任何东西）…</p>
    {:else if removal.failure && !removal.preview}
      <div class="error">{removal.failure}</div>
    {:else if removal.preview}
      <p>{REMOVAL_IRREVERSIBLE}</p>

      <div class="grid">
        {#each removal.preview.allowed_scopes as scope (scope)}
          <label class="checkbox">
            <input
              type="radio"
              name="removal-scope"
              checked={removal.scope === scope}
              disabled={removal.busy}
              onchange={() => changeScope(scope)}
            />
            {REMOVAL_SCOPE_LABELS[scope] ?? scope}
          </label>
        {/each}
      </div>
      {#if removal.scope === "purge_data"}
        <div class="error">{REMOVAL_SCOPE_DETAILS[removal.scope] ?? ""}</div>
      {:else}
        <p class="hint">{REMOVAL_SCOPE_DETAILS[removal.scope] ?? ""}</p>
      {/if}

      {#if removal.preview.running}
        <div class="error">这个账号的机器人正在运行；确认后会先停止它再删除。</div>
      {/if}
      {#if removal.preview.is_startup_target}
        <div class="error">它是「打开 Light 时启动机器人」的启动目标；删除会一并清空该启动目标。</div>
      {/if}
      {#if removal.preview.is_active}
        <div class="error">它是当前选中的账号；删除后不会自动选中别的账号，需要到账号页重新选择。</div>
      {/if}
      {#if removal.preview.credentials.cleanup_pending}
        <div class="error">{REMOVAL_CLEANUP_PENDING}</div>
      {/if}
      {#if removal.preview.credentials.unknown_ownership}
        <div class="error">{REMOVAL_UNKNOWN_CREDENTIALS}</div>
      {/if}

      <dl class="kv">
        {#each removal.preview.categories as item (item.key)}
          <dt>{REMOVAL_CATEGORY_LABELS[item.key] ?? item.key}</dt>
          <dd>
            {formatSize(item.size_bytes)}
            {#if !item.removable}<span class="hint">（受保护，不会删除）</span>{/if}
          </dd>
        {/each}
      </dl>
      <p class="hint">
        合计 {formatSize(removal.preview.size_bytes)}{#if removal.preview.size_complete === false}
          ；{REMOVAL_SIZE_INCOMPLETE}{/if}
      </p>

      {#if removal.operation}
        {#if !removal.operation.finished}
          <div class="notice">{REMOVAL_IN_PROGRESS}</div>
        {:else}
          <div
            class:error={removal.operation.result !== "removed_purged" && removal.operation.result !== "removed_detached"}
            class:notice={removal.operation.result === "removed_purged" || removal.operation.result === "removed_detached"}
          >
            {removalResult}
            {#if removalStage}<span class="hint">停在：{removalStage}</span>{/if}
          </div>
          {#if removal.operation.result !== "removed_purged" && removal.operation.result !== "removed_detached"}
            <p class="hint">{REMOVAL_RETRY_HINT}</p>
          {/if}
        {/if}
      {:else}
        <label class="checkbox">
          <input type="checkbox" bind:checked={removal.confirmed} disabled={removal.busy} />
          {removal.scope === "purge_data" ? REMOVAL_CONFIRM_PURGE : REMOVAL_CONFIRM_KEEP}
        </label>
        {#if removal.failure}<div class="error">{removal.failure}</div>{/if}
      {/if}
      {#if removal.operation && removal.failure}<div class="error">{removal.failure}</div>{/if}
    {/if}

    <div class="row" style="margin-top:12px">
      {#if removal.preview && !removal.operation}
        <button
          class="action danger"
          disabled={removal.busy || !removal.confirmed || removal.token === null}
          onclick={submitRemoval}
        >
          {removal.scope === "purge_data" ? "彻底删除这个账号" : "移除这个账号"}
        </button>
      {/if}
      {#if removal.preview && removal.operation?.finished}
        <button class="action danger" disabled={removal.busy} onclick={loadPreview}>重新预览</button>
      {/if}
      <button class="ghost" disabled={removal.busy} onclick={() => (removal = null)}>关闭</button>
    </div>
  </div>
{/if}
