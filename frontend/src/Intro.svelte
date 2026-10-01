<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { createIntroMotion } from "./introMotion";

  let { onenter, ondone }: { onenter: () => void; ondone: () => void } = $props();
  let leaving = $state(false);
  let enterButton: HTMLButtonElement;
  let stage: HTMLDivElement;
  let emblem: HTMLDivElement;
  let canvas: HTMLCanvasElement;
  let animation: ReturnType<typeof createIntroMotion> | null = null;
  let exitTimer: ReturnType<typeof setTimeout> | null = null;

  onMount(() => {
    animation = createIntroMotion(stage, emblem, canvas);
    enterButton.focus({ preventScroll: true });
  });
  onDestroy(() => {
    if (exitTimer !== null) clearTimeout(exitTimer);
    animation?.destroy();
  });

  function enter(): void {
    if (leaving) return;
    leaving = true;
    animation?.scatter();
    onenter();
    // 动画被禁用或打断时，计时器仍会完成进入流程。
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    exitTimer = setTimeout(ondone, reducedMotion ? 0 : 1000);
  }
</script>

<div class="intro-screen" class:leaving bind:this={stage}>
  <canvas class="intro-particles" bind:this={canvas} aria-hidden="true"></canvas>
  <div class="intro-emblem" bind:this={emblem}>
    <button type="button" class="intro-shape intro-orbit" data-shape="blue" aria-label="蓝色圆形：可拖动，方向键移动，空格释放粒子"><span class="geometry"></span></button>
    <button type="button" class="intro-shape intro-satellite" data-shape="pink" aria-label="粉色圆形：可拖动，方向键移动，空格释放粒子"><span class="geometry"></span></button>
    <button type="button" class="intro-shape intro-vector" data-shape="yellow" aria-label="黄色三角：可拖动，方向键移动，空格释放粒子">
      <svg class="geometry" viewBox="-110 -110 220 220" aria-hidden="true">
        <defs>
          <linearGradient id="intro-yellow" x1="30%" y1="0%" x2="80%" y2="100%">
            <stop offset="0%" stop-color="#FFE9A0" />
            <stop offset="55%" stop-color="#F5D76E" />
            <stop offset="100%" stop-color="#E0B84A" />
          </linearGradient>
        </defs>
        <polygon points="0,-99 98,81 -98,81" fill="url(#intro-yellow)" stroke="#ffffff2e" stroke-width="1.5" />
      </svg>
    </button>
    <button type="button" class="intro-shape intro-signal" data-shape="teal" aria-label="青色方形：可拖动，方向键移动，空格释放粒子"><span class="geometry"></span></button>
  </div>
  <button class="enter-btn" type="button" bind:this={enterButton} onclick={enter} disabled={leaving}>
    <span class="enter-label">GO</span>
  </button>
</div>

<style>
  .intro-screen {
    position: fixed;
    inset: 0;
    z-index: 20;
    display: grid;
    place-items: center;
    overflow: hidden;
    background: radial-gradient(ellipse 80% 60% at 50% 45%, #0c1220 0%, #05060a 70%);
  }
  .intro-screen::before {
    content: "";
    position: absolute;
    inset: 0;
    pointer-events: none;
    background-image: var(--grid-image);
    background-size: 64px 64px;
    mask-image: var(--grid-mask);
    animation: grid-drift 28s linear infinite;
  }
  .intro-emblem {
    position: relative;
    width: min(480px, 84vw, 68svh);
    aspect-ratio: 480 / 430;
    transform: translateY(-5svh);
    z-index: 1;
  }
  .intro-particles { position: absolute; inset: 0; width: 100%; height: 100%; z-index: 0; pointer-events: none; }
  .intro-shape {
    position: absolute;
    display: block;
    aspect-ratio: 1;
    left: 0;
    top: 0;
    padding: 0;
    border: 0;
    border-radius: 12px;
    background: transparent;
    cursor: grab;
    touch-action: none;
    will-change: transform;
    transform: scale(0);
    -webkit-user-select: none;
    user-select: none;
  }
  .intro-shape:global(.dragging) { cursor: grabbing; z-index: 2; }
  .intro-shape:focus-visible { outline: 1px solid #ffffffaa; outline-offset: 10px; }
  .geometry {
    display: block;
    width: 100%;
    height: 100%;
    pointer-events: none;
  }
  .intro-orbit {
    width: 49.17%;
    border-radius: 50%;
  }
  .intro-orbit .geometry {
    position: relative;
    border-radius: 50%;
    background: radial-gradient(circle at 38% 32%, #8ed4ff, #4ba3e3 55%, #2b7bb8);
    border: 1.5px solid #ffffff2e;
    box-shadow: 0 0 calc(24px + var(--impact, 0) * 30px) #4ba3e388, 0 0 80px #4ba3e325;
  }
  .intro-orbit .geometry::after {
    content: "";
    position: absolute;
    inset: 32.2%;
    border: 1px solid #ffffff1f;
    border-radius: 50%;
  }
  .intro-satellite {
    width: 24.17%;
    border-radius: 50%;
  }
  .intro-satellite .geometry {
    border-radius: 50%;
    background: radial-gradient(circle at 40% 30%, #ffb3b0, #f07a7a 60%, #d45a5a);
    border: 1.4px solid #ffffff38;
    box-shadow: 0 0 calc(22px + var(--impact, 0) * 30px) #f07a7a88, 0 0 70px #f07a7a25;
  }
  .intro-vector {
    width: 40%;
  }
  .intro-vector .geometry {
    overflow: visible;
    filter: drop-shadow(0 0 14px #f5d76e99) drop-shadow(0 0 32px #f5d76e22);
  }
  .intro-signal {
    width: 24.17%;
  }
  .intro-signal .geometry {
    border-radius: 8.62%;
    background: linear-gradient(135deg, #7eede0, #3ecfbf 50%, #1fa89a);
    border: 1.5px solid #ffffff33;
    box-shadow: 0 0 calc(22px + var(--impact, 0) * 30px) #3ecfbf88, 0 0 70px #3ecfbf25;
  }
  .enter-btn {
    position: absolute;
    bottom: max(3rem, env(safe-area-inset-bottom) + 1.5rem);
    z-index: 3;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 188px;
    min-height: 58px;
    padding: 12px 30px;
    border: 1px solid transparent;
    border-radius: 999px;
    background: linear-gradient(130deg, #182534ed, #10161eed) padding-box,
      linear-gradient(115deg, #9acdec80, #ffffff26 45%, #79decb60) border-box;
    backdrop-filter: blur(18px);
    box-shadow: inset 0 1px 0 #ffffff0d, 0 10px 32px #0005, 0 0 35px #4ba3e312;
    color: #edf6fa;
    font-family: var(--sans);
    font-size: 22px;
    font-weight: 600;
    cursor: pointer;
    touch-action: manipulation;
    transition: box-shadow .25s ease, transform .25s ease, filter .25s ease;
  }
  .enter-label { letter-spacing: .24em; text-indent: .24em; }
  .enter-btn:hover { filter: brightness(1.16); transform: translateY(-3px); box-shadow: inset 0 1px 0 #ffffff12, 0 12px 36px #0005, 0 0 40px #4ba3e32a; }
  .enter-btn:active { transform: scale(.97); }
  .enter-btn:focus-visible { outline: 2px solid #8ed4ff; outline-offset: 5px; }
  .leaving { pointer-events: none; animation: curtain-away 1s ease-in both; }
  .leaving .enter-btn { animation: button-away .18s ease-out both; }
  @keyframes curtain-away { 0%, 25% { opacity: 1; } 100% { opacity: 0; } }
  @keyframes button-away { to { opacity: 0; transform: translateY(8px); } }
  @media (max-height: 450px) {
    .intro-emblem { width: min(480px, 84vw, 60svh); transform: translateY(-8svh); }
    .enter-btn { bottom: max(1.5rem, env(safe-area-inset-bottom) + 1rem); }
  }
  @media (prefers-reduced-motion: reduce) {
    .intro-screen::before, .leaving, .leaving .enter-btn { animation: none; }
  }
</style>
