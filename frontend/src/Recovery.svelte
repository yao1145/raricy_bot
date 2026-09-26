<script lang="ts">
  // 元数据故障的只读提示面板：只解释状态与下一步，不提供修复/重建/重新开始（F2）。
  import { RECOVERY_NEXT_STEP, recoveryNotice } from "./texts";

  let { code }: { code: string | null } = $props();
  const notice = $derived(recoveryNotice(code));
</script>

<div class="panel">
  <h2>{notice.title}</h2>
  <p>{notice.detail}</p>
  <p class="hint">{RECOVERY_NEXT_STEP}</p>
  {#if code}<p class="hint">机器码：{code}</p>{/if}
</div>
