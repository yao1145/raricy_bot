<script lang="ts">
  // Simple / Advanced 配置与凭据状态（设计 §5.3、§6、§7）。
  // 保存走同一个配置事务；凭据用 keep / replace / delete 三种操作表达。
  import * as api from "./api";
  import { FIELDS, LEVELS, fromInput, toInput, type FieldSpec } from "./fields";

  let { onchanged }: { onchanged: () => void } = $props();

  let view = $state<api.ConfigView | null>(null);
  let texts = $state<Record<string, string>>({});
  let simple = $state(true);
  let showAdvanced = $state(false);
  let busy = $state(false);
  let message = $state<string | null>(null);
  let failure = $state<string | null>(null);
  let password = $state("");
  let apiKey = $state("");
  let passwordAction = $state<"keep" | "replace" | "delete">("keep");
  let apiKeyAction = $state<"keep" | "replace" | "delete">("keep");
  let startOnLaunch = $state(false);
  let kbFile = $state("knowledge.md");
  let kbContent = $state("");
  let kbFiles = $state<number | null>(null);
  let testResult = $state<Record<string, string>>({});

  async function load(): Promise<void> {
    try {
      view = await api.getConfig();
      const next: Record<string, string> = {};
      for (const spec of FIELDS) {
        next[spec.key] = toInput(spec, view.values[spec.key]);
      }
      texts = next;
      startOnLaunch = view.start_bot_on_launch;
      try {
        kbFiles = (await api.kbStatus()).files;
      } catch {
        kbFiles = null;
      }
    } catch (error) {
      failure = error instanceof api.ApiError && error.status === 401
        ? "会话已失效，请重新打开管理页。"
        : "无法读取配置。";
    }
  }

  $effect(() => {
    void load();
  });

  function spec(key: string): FieldSpec | undefined {
    return FIELDS.find((item) => item.key === key);
  }

  function collect(): Record<string, unknown> {
    const values: Record<string, unknown> = {};
    for (const item of FIELDS) {
      const text = texts[item.key] ?? "";
      if (text.trim() === "") continue; // 空白字段保留服务端原值，不覆盖成空
      values[item.key] = fromInput(item, text);
    }
    return values;
  }

  async function save(): Promise<void> {
    if (!view) return;
    busy = true;
    message = null;
    failure = null;
    try {
      const credentials: Record<string, unknown> = {};
      if (passwordAction === "replace") credentials.password = { action: "replace", value: password };
      else if (passwordAction === "delete") credentials.password = { action: "delete" };
      else credentials.password = { action: "keep" };
      if (apiKeyAction === "replace") credentials.llm_api_key = { action: "replace", value: apiKey };
      else if (apiKeyAction === "delete") credentials.llm_api_key = { action: "delete" };
      else credentials.llm_api_key = { action: "keep" };

      const body: Record<string, unknown> = {
        expected_revision: view.revision ?? 0,
        values: collect(),
        credentials,
        start_bot_on_launch: startOnLaunch,
      };
      if (!view.account) body.account = "";
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

  function describe(error: unknown): string {
    if (error instanceof api.ApiError) {
      if (error.status === 409) return "配置已被另一个页面改过，请刷新后重试。";
      if (error.field) return `字段有问题：${error.field}（${error.code}）`;
      return `操作失败：${error.code}`;
    }
    return "操作失败；请查看近期事件。";
  }

  const simpleFields = $derived(FIELDS.filter((item) => item.group === "simple"));
  const advancedFields = $derived(FIELDS.filter((item) => item.group === "advanced"));
</script>

{#if view}
  <div class="panel">
    <h2>模型与提示词</h2>
    <div class="grid">
      {#each simpleFields.filter((item) => item.key.startsWith("model.")) as item (item.key)}
        <label>{item.label}
          {#if item.kind === "bool"}
            <input type="checkbox" bind:checked={texts[item.key] as any} />
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
      <label>密码操作
        <select bind:value={passwordAction}>
          <option value="keep">保持不变</option>
          <option value="replace">替换为新值</option>
          <option value="delete">删除</option>
        </select>
      </label>
      {#if passwordAction === "replace"}
        <label>新密码<input type="password" bind:value={password} autocomplete="new-password" /></label>
      {/if}
      <label>模型 Key 操作
        <select bind:value={apiKeyAction}>
          <option value="keep">保持不变</option>
          <option value="replace">替换为新值</option>
          <option value="delete">删除</option>
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
    <h2>能力</h2>
    <div class="grid">
      {#each simpleFields.filter((item) => !item.key.startsWith("model.") && item.key !== "system_prompt") as item (item.key)}
        <label class:checkbox={item.kind === "bool"}>
          {#if item.kind === "bool"}
            <input type="checkbox" bind:checked={texts[item.key] as any} />
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
    <p class="hint">当前资料 {kbFiles ?? "未知"} 份。只接受 Markdown 单文件，写入受管目录后由既有刷新机制加载。</p>
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
    <label class="checkbox" style="margin-top:12px">
      <input type="checkbox" bind:checked={startOnLaunch} />
      打开程序时自动启动机器人（只影响新的启动会话）
    </label>
  </div>

  {#if message}<div class="notice">{message}</div>{/if}
  {#if failure}<div class="error">{failure}</div>{/if}

  <div class="row">
    <button class="action" disabled={busy} onclick={save}>保存</button>
    <button class="ghost" disabled={busy} onclick={() => (simple = !simple)}>
      {simple ? "收起说明" : "展开说明"}
    </button>
  </div>
{/if}
