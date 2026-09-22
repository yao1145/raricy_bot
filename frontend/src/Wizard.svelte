<script lang="ts">
  // 首次向导（设计 §5.1）：填账号与模型、选能力、保存并启动。
  // 密码与 API Key 只存在于这一页的内存里，从不写进浏览器存储。
  import * as api from "./api";

  let { ondone }: { ondone: () => void } = $props();

  let account = $state("");
  let password = $state("");
  let apiKey = $state("");
  let step = $state(1);
  let busy = $state(false);
  let failure = $state<string | null>(null);
  let values = $state<Record<string, unknown>>({});
  let config = $state<api.ConfigView | null>(null);

  $effect(() => {
    void (async () => {
      try {
        config = await api.getConfig();
        values = { ...(config.defaults ?? {}), ...values };
      } catch {
        failure = "无法读取配置；请重新打开管理页。";
      }
    })();
  });

  function field(key: string, label: string, kind: "text" | "bool" = "text") {
    return { key, label, kind };
  }

  const capabilityFields = [
    field("model.vision_enabled", "图片理解（模型需支持图片）", "bool"),
    field("comments.enabled", "博客评论", "bool"),
    field("knowledge_base.enabled", "本地知识库", "bool"),
    field("memory.enabled", "长期记忆", "bool"),
  ];

  async function saveAndStart(): Promise<void> {
    busy = true;
    failure = null;
    try {
      const payload: Record<string, unknown> = {
        expected_revision: 0,
        values,
        account,
        start_bot_on_launch: true,
        credentials: {
          password: { action: "replace", value: password },
          llm_api_key: { action: "replace", value: apiKey },
        },
      };
      if (values["knowledge_base.enabled"]) {
        values["knowledge_base.allowed_channel_kinds"] = values["knowledge_base.allowed_channel_kinds"] ?? ["dm"];
        values["knowledge_base.allowed_user_ids"] = values["knowledge_base.allowed_user_ids"] ?? [account];
      }
      if (values["memory.enabled"]) {
        values["memory.access_mode"] = values["memory.access_mode"] ?? "allowlist";
        values["memory.allow_user_list"] = values["memory.allow_user_list"] ?? [account];
        values["memory.admin_user_list"] = values["memory.admin_user_list"] ?? [account];
      }
      await api.saveConfig(payload);
      await api.botAction("start");
      ondone();
    } catch (error) {
      failure = describe(error);
    } finally {
      busy = false;
    }
  }

  async function saveDraft(): Promise<void> {
    // 草稿只保存非敏感字段，允许还没填完（§6.2）。
    busy = true;
    failure = null;
    try {
      const draft = { ...values };
      delete draft["system_prompt"];
      const current = await api.getConfig();
      await api.saveDraft({ expected_revision: 0, values: draft });
      void current;
    } catch (error) {
      failure = describe(error);
    } finally {
      busy = false;
    }
  }

  function describe(error: unknown): string {
    if (error instanceof api.ApiError) {
      if (error.field) return `字段有问题：${error.field}（${error.code}）`;
      return `保存失败：${error.code}`;
    }
    return "保存失败；请稍后再试。";
  }

  const canSubmit = $derived(account.trim() !== "" && password.trim() !== "" && apiKey.trim() !== "");
</script>

<div class="panel">
  <h2>首次设置 · 第 {step} / 3 步</h2>

  {#if step === 1}
    <div class="grid">
      <label>站点账号
        <input bind:value={account} autocomplete="username" />
      </label>
      <label>站点密码
        <input type="password" bind:value={password} autocomplete="current-password" />
      </label>
    </div>
    <p class="hint">密码只会保存进系统凭据库；如果系统凭据库不可用，则只在本次运行内有效。</p>
  {:else if step === 2}
    <div class="grid">
      <label>模型 API 地址
        <input bind:value={values["model.base_url"] as string} placeholder="https://example.com/v1" />
      </label>
      <label>模型名称
        <input bind:value={values["model.model"] as string} />
      </label>
      <label>模型 API Key
        <input type="password" bind:value={apiKey} autocomplete="off" />
      </label>
    </div>
    <label>System Prompt
      <textarea bind:value={values["system_prompt"] as string}></textarea>
    </label>
    <p class="hint">图片与对话内容会送往这个模型服务；提示词正文可在设置里随时修改。</p>
  {:else}
    <div class="grid">
      {#each capabilityFields as item (item.key)}
        <label class="checkbox">
          <input type="checkbox" bind:checked={values[item.key] as boolean} />
          {item.label}
        </label>
      {/each}
    </div>
    <p class="hint">
      知识库与记忆还需要在「设置」里填写允许的使用者；不选也可以，稍后再开启。
    </p>
  {/if}

  {#if failure}<div class="error">{failure}</div>{/if}

  <div class="row" style="margin-top:16px">
    <button class="action" disabled={step === 1} onclick={() => (step -= 1)}>上一步</button>
    {#if step < 3}
      <button class="action" disabled={step === 1 && !canSubmit} onclick={() => (step += 1)}>下一步</button>
      <button class="ghost" disabled={busy} onclick={saveDraft}>保存草稿</button>
    {:else}
      <button class="action" disabled={busy || !canSubmit} onclick={saveAndStart}>保存并启动</button>
    {/if}
  </div>
</div>
