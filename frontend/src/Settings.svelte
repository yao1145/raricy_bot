<script lang="ts">
  // Simple / Advanced 配置与凭据状态（设计 §5.3、§6、§7）。
  // 保存走同一个配置事务；凭据只提供 keep / replace 两种操作（删除暂不可用，
  // 服务端对 delete 一律回 409 `credential_delete_unavailable`，见 D-131）。
  import * as api from "./api";
  import { FIELDS, LEVELS, fromInput, toInput, type FieldSpec } from "./fields";
  import {
    CREDENTIALS_CLEANUP_PENDING,
    CREDENTIALS_CLEAR_CONFIRM,
    CREDENTIALS_CLEAR_RESULTS,
    CREDENTIALS_UNKNOWN_OWNERSHIP,
    accountCodeNotice,
    conflictNotice,
  } from "./texts";

  type ClearScope = "password" | "llm_api_key" | "both";

  let {
    profile = null,
    epoch = null,
    onchanged,
  }: {
    /** 当前活动档案的卡片（来自 `GET /api/profiles`）：清理待办与重试入口从这里来。 */
    profile?: api.ProfileCard | null;
    /** 状态 DTO 的活动代次；过渡写入口要原样带上（§59、D-146）。 */
    epoch?: number | null;
    onchanged: () => void;
  } = $props();

  let view = $state<api.ConfigView | null>(null);
  let texts = $state<Record<string, string>>({}); // 非布尔字段的输入框文本
  let flags = $state<Record<string, boolean>>({}); // 布尔字段
  let showAdvanced = $state(false);
  let busy = $state(false);
  let message = $state<string | null>(null);
  let failure = $state<string | null>(null);

  let account = $state("");
  let password = $state("");
  let apiKey = $state("");
  let passwordAction = $state<"keep" | "replace">("keep");
  let apiKeyAction = $state<"keep" | "replace">("keep");

  let kbFile = $state("knowledge.md");
  let kbContent = $state("");
  let kbFiles = $state<number | null>(null);
  let testResult = $state<Record<string, string>>({});

  let clearScope = $state<ClearScope>("password");
  let clearConfirmed = $state(false);
  let clearFailure = $state<string | null>(null);
  let clearBusy = $state(false);

  async function load(): Promise<void> {
    try {
      const loaded = await api.getConfig();
      view = loaded;
      const nextTexts: Record<string, string> = {};
      const nextFlags: Record<string, boolean> = {};
      for (const spec of FIELDS) {
        const raw = loaded.values[spec.key];
        if (spec.kind === "bool") nextFlags[spec.key] = raw === true;
        else nextTexts[spec.key] = toInput(spec, raw);
      }
      texts = nextTexts;
      flags = nextFlags;
      account = loaded.account ?? "";
      try {
        kbFiles = (await api.kbStatus()).files;
      } catch {
        kbFiles = null;
      }
    } catch (error) {
      failure =
        error instanceof api.ApiError && error.status === 401
          ? "会话已失效，请从桌面图标重新打开管理页。"
          : "无法读取配置。";
    }
  }

  $effect(() => {
    void load();
  });

  function collect(): Record<string, unknown> {
    const values: Record<string, unknown> = {};
    for (const spec of FIELDS) {
      if (spec.kind === "bool") {
        values[spec.key] = flags[spec.key] === true;
        continue;
      }
      const text = (texts[spec.key] ?? "").trim();
      if (text === "") continue; // 空白字段保留服务端原值，不覆盖成空
      values[spec.key] = fromInput(spec, text);
    }
    return values;
  }

  async function save(): Promise<void> {
    if (!view) return;
    busy = true;
    message = null;
    failure = null;
    try {
      const credentials: Record<string, unknown> = {
        password:
          passwordAction === "replace"
            ? { action: "replace", value: password }
            : { action: passwordAction },
        llm_api_key:
          apiKeyAction === "replace"
            ? { action: "replace", value: apiKey }
            : { action: apiKeyAction },
      };
      const body: Record<string, unknown> = {
        expected_revision: view.revision ?? 0,
        values: collect(),
        credentials,
        // 过渡入口的活动代次门（§59、D-146）：档案用加载这份表单时的那一个，
        // 指针已经切走时服务端会回 409，而不是把改动写到刚切过去的账号上。
        profile_id: view.profile_id,
        expected_profile_epoch: epoch ?? 0,
      };
      if (!view.account && account.trim() !== "") body.account = account.trim();
      const result = await api.saveConfig(body);
      password = "";
      apiKey = "";
      passwordAction = "keep";
      apiKeyAction = "keep";
      message = `已保存为 revision ${result.revision}；正在运行的机器人要用「重启」才会应用新配置。`;
      await load();
      onchanged();
    } catch (error) {
      failure = describe(error);
    } finally {
      busy = false;
    }
  }

  async function runTest(kind: "site" | "model"): Promise<void> {
    busy = true;
    failure = null;
    try {
      const result = kind === "site" ? await api.testSite() : await api.testModel();
      testResult = {
        ...testResult,
        [kind]: result.ok ? `通过（${result.elapsed_ms} ms）` : `未通过：${result.detail}`,
      };
      onchanged();
    } catch (error) {
      testResult = { ...testResult, [kind]: describe(error) };
    } finally {
      busy = false;
    }
  }

  async function importKb(): Promise<void> {
    busy = true;
    failure = null;
    try {
      const result = await api.kbImport(kbFile, kbContent);
      message = `已导入 ${result.name}；机器人在下一次刷新知识库时会加载它。`;
      kbContent = "";
      await load();
    } catch (error) {
      failure = describe(error);
    } finally {
      busy = false;
    }
  }

  /** 清除保存的凭据：先停止机器人、撤销受管历史引用，再把档案置为待重填（§6.1）。 */
  async function clearSavedCredentials(): Promise<void> {
    if (!view?.profile_id || !clearConfirmed) return;
    clearBusy = true;
    clearFailure = null;
    message = null;
    failure = null;
    try {
      const selected =
        clearScope === "both" ? ["password", "llm_api_key"] : [clearScope];
      const body = await api.clearCredentials(view.profile_id, {
        kinds: selected,
        idempotency_key: api.newIdempotencyKey("clear"),
      });
      const operation = await api.waitForOperation(body.operation_id);
      message =
        CREDENTIALS_CLEAR_RESULTS[operation.result ?? ""] ??
        `清除已结束（${operation.result ?? operation.state}）。`;
      clearConfirmed = false;
      await load();
      onchanged();
    } catch (error) {
      clearFailure = describe(error);
    } finally {
      clearBusy = false;
    }
  }

  // 409 在不同接口上有不同含义，按稳定码分别说明（不把「重启中」说成「另一个页面改过」）。
  const CONFLICT_TEXT: Record<string, string> = {
    revision_conflict: "配置已被另一个页面改过，请刷新后重试。",
    bot_running: "机器人正在运行：先停止它，再测试站点。",
    test_in_progress: "上一次测试还没结束，请稍等。",
    config_not_ready: "还没有可用的正式配置，请先保存。",
    no_active_config: "还没有可用的正式配置，请先保存。",
    credentials_unresolved: "凭据不可用：请在下面重新填写密码与模型 Key。",
  };

  function describe(error: unknown): string {
    if (error instanceof api.ApiError) {
      if (error.status === 401) return "会话已失效，请从桌面图标重新打开管理页。";
      // 无本地条目的稳定码用服务端固定文案（例如删除凭据暂不可用），页面不自行翻译。
      if (error.status === 409)
        return (
          CONFLICT_TEXT[error.code] ??
          conflictNotice(error.code) ??
          accountCodeNotice(error.code) ??
          error.detail ??
          `操作冲突：${error.code}`
        );
      if (error.detail) return error.detail;
      if (error.field) return `字段有问题：${error.field}（${error.code}）`;
      return `操作失败：${error.code}`;
    }
    return "操作失败；请查看近期事件。";
  }

  const simpleFields = $derived(
    FIELDS.filter(
      (item) =>
        item.group === "simple" &&
        !item.key.startsWith("model.") &&
        item.key !== "system_prompt",
    ),
  );
  const modelFields = $derived(FIELDS.filter((item) => item.key.startsWith("model.")));
  const advancedFields = $derived(FIELDS.filter((item) => item.group === "advanced"));
</script>

{#if view}
  <div class="panel">
    <h2>模型与提示词</h2>
    <div class="grid">
      {#each modelFields as item (item.key)}
        <label>{item.label}
          {#if item.kind === "bool"}
            <input type="checkbox" bind:checked={flags[item.key]} />
          {:else}
            <input bind:value={texts[item.key]} />
          {/if}
          {#if item.help}<span class="hint">{item.help}</span>{/if}
        </label>
      {/each}
    </div>
    <label>System Prompt
      <textarea bind:value={texts["system_prompt"]}></textarea>
    </label>
    <div class="row">
      <button class="action" disabled={busy} onclick={() => runTest("model")}>测试模型</button>
      {#if testResult.model}<span class="hint">模型：{testResult.model}</span>{/if}
    </div>
  </div>

  <div class="panel">
    <h2>站点与凭据</h2>
    <dl class="kv">
      <dt>站点地址</dt>
      <dd>https://raricy.com（首版固定）</dd>
      <dt>账号</dt>
      <dd>{view.account ?? "（首次保存时填写）"}</dd>
      <dt>密码</dt>
      <dd>{view.credentials.password.configured ? "已保存" : "未配置"}</dd>
      <dt>模型 Key</dt>
      <dd>{view.credentials.llm_api_key.configured ? "已保存" : "未配置"}</dd>
      <dt>凭据后端</dt>
      <dd>
        {view.credentials.backend.name}
        {#if !view.credentials.backend.available}
          <span class="hint">不可用：凭据只在本次运行内有效，重启后需要重新填写</span>
        {/if}
      </dd>
    </dl>
    <div class="grid">
      {#if !view.account}
        <label>站点账号
          <input bind:value={account} autocomplete="username" />
        </label>
      {/if}
      <label>密码操作
        <select bind:value={passwordAction}>
          <option value="keep">保持不变</option>
          <option value="replace">替换为新值</option>
        </select>
      </label>
      {#if passwordAction === "replace"}
        <label>新密码<input type="password" bind:value={password} autocomplete="new-password" /></label>
      {/if}
      <label>模型 Key 操作
        <select bind:value={apiKeyAction}>
          <option value="keep">保持不变</option>
          <option value="replace">替换为新值</option>
        </select>
      </label>
      {#if apiKeyAction === "replace"}
        <label>新的模型 Key<input type="password" bind:value={apiKey} autocomplete="off" /></label>
      {/if}
    </div>
    <div class="row">
      <button class="action" disabled={busy} onclick={() => runTest("site")}>测试站点</button>
      {#if testResult.site}<span class="hint">站点：{testResult.site}</span>{/if}
    </div>
  </div>

  <div class="panel">
    <h2>清除保存的凭据</h2>
    {#if view.profile_id}
      <p class="hint">{CREDENTIALS_CLEAR_CONFIRM}</p>
      {#if profile?.credentials.cleanup_pending}
        <div class="error">
          {CREDENTIALS_CLEANUP_PENDING}
          <button class="ghost" disabled={clearBusy} onclick={clearSavedCredentials}>重试清除</button>
        </div>
      {/if}
      {#if profile?.credentials.unknown_ownership}
        <div class="error">{CREDENTIALS_UNKNOWN_OWNERSHIP}</div>
      {/if}
      <div class="grid">
        <label class="checkbox">
          <input type="radio" bind:group={clearScope} value="password" disabled={clearBusy} />
          只清站点密码
        </label>
        <label class="checkbox">
          <input type="radio" bind:group={clearScope} value="llm_api_key" disabled={clearBusy} />
          只清模型 Key
        </label>
        <label class="checkbox">
          <input type="radio" bind:group={clearScope} value="both" disabled={clearBusy} />
          两者都清
        </label>
      </div>
      <label class="checkbox">
        <input type="checkbox" bind:checked={clearConfirmed} disabled={clearBusy} />
        我确认清除所选的凭据，并明白没有撤销；清除后这个账号要重新填写凭据才能启动。
      </label>
      <div class="row" style="margin-top:12px">
        <button class="action danger" disabled={clearBusy || !clearConfirmed} onclick={clearSavedCredentials}>
          清除所选凭据
        </button>
        <span class="hint">清除与「停止机器人」「移除账号」是三件不同的事，各有自己的入口。</span>
      </div>
    {:else}
      <p class="hint">还没有可清除的账号档案。</p>
    {/if}
    {#if clearFailure}<div class="error">{clearFailure}</div>{/if}
  </div>

  <div class="panel">
    <h2>能力</h2>
    <div class="grid">
      {#each simpleFields as item (item.key)}
        <label class:checkbox={item.kind === "bool"}>
          {#if item.kind === "bool"}
            <input type="checkbox" bind:checked={flags[item.key]} />
            {item.label}
          {:else}
            {item.label}
            <input bind:value={texts[item.key]} />
          {/if}
          {#if item.help}<span class="hint">{item.help}</span>{/if}
        </label>
      {/each}
    </div>
  </div>

  <div class="panel">
    <h2>知识库导入</h2>
    <p class="hint">
      当前资料 {kbFiles ?? "未知"} 份。只接受单份 Markdown（上限 1 MiB），写入受管目录后
      由既有刷新机制加载。
    </p>
    <div class="grid">
      <label>文件名<input bind:value={kbFile} /></label>
    </div>
    <label>内容
      <textarea bind:value={kbContent} placeholder="# 标题&#10;正文…"></textarea>
    </label>
    <div class="row">
      <button class="action" disabled={busy || kbContent.trim() === ""} onclick={importKb}>导入</button>
    </div>
  </div>

  <div class="panel">
    <h2>高级</h2>
    <label class="checkbox">
      <input type="checkbox" bind:checked={showAdvanced} />
      显示高级字段（上下文、队列、并发、超时、日志级别）
    </label>
    {#if showAdvanced}
      <div class="grid">
        {#each advancedFields as item (item.key)}
          <label>{item.label}
            {#if item.kind === "level"}
              <select bind:value={texts[item.key]}>
                {#each LEVELS as level}
                  <option value={level}>{level}</option>
                {/each}
              </select>
            {:else}
              <input bind:value={texts[item.key]} />
            {/if}
          </label>
        {/each}
      </div>
    {/if}
    <p class="hint" style="margin-top:12px">
      「打开 Light 时启动机器人」与「登录 Windows 时启动 Light」都已移到「桌面」页的桌面设置。
    </p>
  </div>

  {#if message}<div class="notice">{message}</div>{/if}
  {#if failure}<div class="error">{failure}</div>{/if}

  <div class="row">
    <button class="action" disabled={busy} onclick={save}>保存</button>
  </div>
{/if}
