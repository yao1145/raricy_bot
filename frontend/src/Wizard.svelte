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
        systemPrompt = toInput(
          { key: "system_prompt", label: "", kind: "prompt", group: "simple" },
          view.values["system_prompt"] ?? view.defaults["system_prompt"],
        );
        modelUrl = toInput(
          { key: "model.base_url", label: "", kind: "text", group: "simple" },
          view.values["model.base_url"],
        );
        modelName = toInput(
          { key: "model.model", label: "", kind: "text", group: "simple" },
          view.values["model.model"],
        );
        for (const item of capabilityFields) {
          flags[item.key] = view.values[item.key] === true;
        }
        const draft = await api.getDraft();
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
    if (flags["knowledge_base.enabled"]) {
      // 启用知识库必须同时给出使用范围（§13.2），界面按最小可用范围预填。
      values["knowledge_base.allowed_channel_kinds"] = ["dm"];
      values["knowledge_base.allowed_user_ids"] = [account.trim()];
    }
    if (flags["memory.enabled"]) {
      values["memory.access_mode"] = "allowlist";
      values["memory.allow_user_list"] = [account.trim()];
      values["memory.admin_user_list"] = [account.trim()];
    }
    return values;
  }

  async function saveDraft(): Promise<void> {
    // 草稿只保存非敏感字段，允许还没填完（§6.2）；revision 用草稿自己的。
    busy = true;
    failure = null;
    message = null;
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
        <input bind:value={account} autocomplete="username" />
      </label>
      <label>站点密码
        <input type="password" bind:value={password} autocomplete="current-password" />
      </label>
    </div>
    <p class="hint">
      密码只保存进系统凭据库；如果系统凭据库不可用，则只在本次运行内有效（设置页会标明）。
    </p>
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
  {:else}
    <div class="grid">
      {#each capabilityFields as item (item.key)}
        <label class="checkbox">
          <input type="checkbox" bind:checked={flags[item.key]} />
          {item.label}
        </label>
      {/each}
    </div>
    <p class="hint">
      知识库与记忆会按「仅本人私聊」的默认范围启用，之后可在设置里调整允许的使用者。
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
