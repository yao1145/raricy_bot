<script lang="ts">
  // 近期事件（设计 §12）：只显示服务端白名单事件；本地最多保留 200 条。
  import { onDestroy, onMount } from "svelte";
  import * as api from "./api";

  const CAPACITY = 200;

  let events = $state<api.LogEvent[]>([]);
  let gap = $state(false);
  let unsubscribe: (() => void) | null = null;

  onMount(() => {
    unsubscribe = api.subscribeEvents(
      (event) => {
        const next = [...events, event];
        events = next.length > CAPACITY ? next.slice(next.length - CAPACITY) : next;
      },
      () => {
        gap = true;
      },
    );
  });

  onDestroy(() => unsubscribe?.());

  function time(at: number): string {
    const date = new Date(at * 1000);
    return date.toLocaleTimeString("zh-CN", { hour12: false });
  }

  function levelClass(level: string): string {
    const lowered = level.toLowerCase();
    if (lowered === "error" || lowered === "critical") return "error";
    if (lowered === "warning") return "warn";
    return "";
  }
</script>

<div class="panel">
  <h2>近期事件</h2>
  <p class="hint">
    只有固定事件码与白名单字段；正文、提示词、Cookie 与凭据从不进入这里。最多显示最近
    {CAPACITY} 条。
  </p>
  {#if gap}
    <div class="notice">事件游标已失效（可能是控制服务重启过），列表从当前时刻重新开始。</div>
  {/if}
  <div class="log">
    {#if events.length === 0}
      <div class="hint">还没有事件。</div>
    {/if}
    {#each events as event (event.id)}
      <div class={levelClass(event.level)}>
        <span class="time">{time(event.at)}</span>
        [{event.level.toLowerCase()}] {event.event}
        {#if Object.keys(event.fields).length > 0}
          <span class="time">
            {Object.entries(event.fields)
              .map(([key, value]) => `${key}=${String(value)}`)
              .join(" ")}
          </span>
        {/if}
      </div>
    {/each}
  </div>
</div>
