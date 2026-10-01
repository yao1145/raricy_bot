type ShapeKey = "blue" | "pink" | "yellow" | "teal";
type Point = { x: number; y: number };
type Body = Point & {
  key: ShapeKey; el: HTMLElement; vx: number; vy: number; radius: number;
  mass: number; color: string; rotation: number; spin: number; scale: number; pulse: number;
};
type Particle = Point & {
  vx: number; vy: number; life: number; decay: number; radius: number;
  color: string; rotation: number; kind: number;
};

const REST: Record<ShapeKey, Point> = {
  // 对齐 favicon 的紧凑构图：方块位于大圆正下方。
  blue: { x: 160, y: 151 }, pink: { x: 357, y: 90 },
  yellow: { x: 323, y: 260 }, teal: { x: 160, y: 357 },
};
const SHAPES: { key: ShapeKey; radius: number; mass: number; color: string }[] = [
  { key: "blue", radius: 118, mass: 3.2, color: "#4BA3E3" },
  { key: "pink", radius: 58, mass: 1.1, color: "#F07A7A" },
  { key: "yellow", radius: 86, mass: 2.1, color: "#F5D76E" },
  { key: "teal", radius: 78, mass: 1.8, color: "#3ECFBF" },
];

/** 管理开场的物理运动、画布和监听器，进入控制台前统一释放。 */
export function createIntroMotion(stage: HTMLElement, emblem: HTMLElement, canvas: HTMLCanvasElement) {
  const ctx = canvas.getContext("2d");
  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const bodies: Body[] = SHAPES.map(shape => ({
    ...shape, el: emblem.querySelector<HTMLElement>(`[data-shape="${shape.key}"]`)!,
    x: 240, y: 215, vx: 0, vy: 0, rotation: 0, spin: 0, scale: 0, pulse: 0,
  }));
  const particles: Particle[] = [];
  let layout = { left: 0, top: 0, width: 0, height: 0, scale: 1 };
  let frame = 0;
  let last = 0;
  const started = performance.now();
  let disposed = false;
  let dragging: Body | null = null;
  let pointerId: number | null = null;
  let offset: Point = { x: 0, y: 0 };
  let pointer: Point | null = null;
  let lastMove = 0;
  let scatter: { start: number; from: (Point & { rotation: number; scale: number })[] } | null = null;
  let sparkle = 0;
  const launched = new Set<ShapeKey>();

  function render(body: Body) {
    const scale = body.scale * (1 + body.pulse * .1);
    body.el.style.transform = `translate(${body.x * layout.scale}px, ${body.y * layout.scale}px) translate(-50%, -50%) rotate(${body.rotation}deg) scale(${scale})`;
    body.el.style.setProperty("--impact", String(body.pulse));
  }

  function resize() {
    const box = emblem.getBoundingClientRect();
    const screen = stage.getBoundingClientRect();
    layout = { left: box.left - screen.left, top: box.top - screen.top, width: screen.width, height: screen.height, scale: box.width / 480 };
    // 限制高分屏画布开销，粒子坐标仍以 CSS 像素计算。
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(screen.width * dpr);
    canvas.height = Math.round(screen.height * dpr);
    ctx?.setTransform(dpr, 0, 0, dpr, 0, 0);
    bodies.forEach(render);
  }

  function point(event: PointerEvent): Point {
    return { x: (event.clientX - layout.left) / layout.scale, y: (event.clientY - layout.top) / layout.scale };
  }

  function emit(body: Body, count: number, speed: number, trail = false, origin: Point = body) {
    if (motion.matches || !ctx) return;
    for (let i = 0; i < count && particles.length < 420; i++) {
      const angle = Math.random() * Math.PI * 2;
      const velocity = (.4 + Math.random()) * speed;
      particles.push({
        x: layout.left + origin.x * layout.scale, y: layout.top + origin.y * layout.scale,
        vx: Math.cos(angle) * velocity, vy: Math.sin(angle) * velocity,
        life: trail ? .6 : 1, decay: trail ? .025 : .012 + Math.random() * .018,
        radius: trail ? 1.5 + Math.random() * 2 : 1.5 + Math.random() * 3,
        color: body.color, rotation: Math.random() * Math.PI, kind: Math.floor(Math.random() * 3),
      });
    }
  }

  function drawParticles(step: number) {
    if (!ctx) return;
    ctx.clearRect(0, 0, layout.width, layout.height);
    for (let i = particles.length - 1; i >= 0; i--) {
      const p = particles[i];
      p.life -= p.decay * step;
      if (p.life <= 0) { particles.splice(i, 1); continue; }
      p.x += p.vx * step; p.y += p.vy * step;
      p.vy += .025 * step;
      p.vx *= .99 ** step;
      ctx.save();
      ctx.globalAlpha = p.life * .85;
      ctx.fillStyle = p.color;
      ctx.translate(p.x, p.y);
      ctx.rotate(p.rotation + (1 - p.life) * 2);
      const r = p.radius * (.4 + p.life);
      if (p.kind === 0) ctx.fillRect(-r, -r, r * 2, r * 2);
      else {
        ctx.beginPath();
        if (p.kind === 1) {
          ctx.moveTo(0, -r * 1.3); ctx.lineTo(r, r * .8); ctx.lineTo(-r, r * .8); ctx.closePath();
        } else ctx.arc(0, 0, r, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();
    }
  }

  function bounds(body: Body) {
    const r = body.radius * body.scale;
    const minX = (12 - layout.left) / layout.scale + r;
    const maxX = (layout.width - 12 - layout.left) / layout.scale - r;
    const minY = (12 - layout.top) / layout.scale + r;
    const maxY = (layout.height - 12 - layout.top) / layout.scale - r;
    if (body.x < minX) { body.x = minX; body.vx = Math.abs(body.vx) * .72; }
    if (body.x > maxX) { body.x = maxX; body.vx = -Math.abs(body.vx) * .72; }
    if (body.y < minY) { body.y = minY; body.vy = Math.abs(body.vy) * .72; }
    if (body.y > maxY) { body.y = maxY; body.vy = -Math.abs(body.vy) * .72; }
  }

  function collide(a: Body, b: Body) {
    if (a.scale < .5 || b.scale < .5) return;
    const dx = b.x - a.x, dy = b.y - a.y;
    const distance = Math.hypot(dx, dy);
    const minimum = (a.radius * a.scale + b.radius * b.scale) * .9;
    if (distance >= minimum) return;
    const nx = distance > .001 ? dx / distance : 1;
    const ny = distance > .001 ? dy / distance : 0;
    const invA = a === dragging ? 0 : 1 / a.mass;
    const invB = b === dragging ? 0 : 1 / b.mass;
    const correction = (minimum - distance) / (invA + invB);
    a.x -= nx * correction * invA; a.y -= ny * correction * invA;
    b.x += nx * correction * invB; b.y += ny * correction * invB;
    const velocity = (b.vx - a.vx) * nx + (b.vy - a.vy) * ny;
    if (velocity >= 0) return;
    const impulse = -1.78 * velocity / (invA + invB);
    a.vx -= impulse * nx * invA; a.vy -= impulse * ny * invA;
    b.vx += impulse * nx * invB; b.vy += impulse * ny * invB;
    const impact = Math.min(1, Math.abs(velocity) / 12);
    if (impact > .18) {
      a.pulse = b.pulse = impact;
      a.spin -= ny * impact * 1.5; b.spin += nx * impact * 1.5;
      // 按双方半径计算接触点，两种颜色的粒子从同一个接触点发射。
      const radiusA = a.radius * a.scale, radiusB = b.radius * b.scale;
      const contact = {
        x: (a.x * radiusB + b.x * radiusA) / (radiusA + radiusB),
        y: (a.y * radiusB + b.y * radiusA) / (radiusA + radiusB),
      };
      emit(a, Math.round(8 + impact * 12), 2 + impact * 4, false, contact);
      emit(b, Math.round(8 + impact * 12), 2 + impact * 4, false, contact);
    }
  }

  function release(event?: PointerEvent) {
    if (event && event.pointerId !== pointerId) return;
    if (dragging) {
      if (event?.type === "pointercancel" || performance.now() - lastMove > 100) dragging.vx = dragging.vy = 0;
      emit(dragging, 16, 3, false, { x: dragging.x + offset.x, y: dragging.y + offset.y });
      dragging.el.classList.remove("dragging");
    }
    const captured = pointerId;
    dragging = null; pointerId = null;
    if (captured !== null && stage.hasPointerCapture(captured)) stage.releasePointerCapture(captured);
  }

  function down(event: PointerEvent) {
    if (scatter || dragging || event.button !== 0) return;
    const element = (event.target as Element).closest<HTMLElement>("[data-shape]");
    const body = bodies.find(item => item.el === element);
    if (!body || body.scale < .5) return;
    event.preventDefault();
    pointer = point(event); dragging = body; pointerId = event.pointerId;
    offset = { x: pointer.x - body.x, y: pointer.y - body.y };
    lastMove = performance.now();
    body.vx = body.vy = 0;
    body.pulse = 1;
    body.el.classList.add("dragging");
    stage.setPointerCapture(event.pointerId);
    emit(body, 24, 4, false, pointer);
  }

  function move(event: PointerEvent) {
    if (scatter || (pointerId !== null && event.pointerId !== pointerId)) return;
    pointer = point(event);
    if (!dragging) return;
    const now = performance.now();
    const elapsed = Math.max(8, now - lastMove) / (1000 / 60);
    const x = pointer.x - offset.x, y = pointer.y - offset.y;
    dragging.vx = Math.max(-24, Math.min(24, (x - dragging.x) / elapsed));
    dragging.vy = Math.max(-24, Math.min(24, (y - dragging.y) / elapsed));
    dragging.x = x; dragging.y = y;
    lastMove = now;
    render(dragging);
    emit(dragging, 3, .7, true, pointer);
  }

  function keydown(event: KeyboardEvent) {
    if (scatter) return;
    const body = bodies.find(item => item.el === event.target);
    if (!body) return;
    const arrows: Record<string, Point> = {
      ArrowLeft: { x: -1, y: 0 }, ArrowRight: { x: 1, y: 0 },
      ArrowUp: { x: 0, y: -1 }, ArrowDown: { x: 0, y: 1 },
    };
    const direction = arrows[event.key];
    if (direction) {
      event.preventDefault();
      body.x += direction.x * 16; body.y += direction.y * 16;
      body.vx = direction.x * 4; body.vy = direction.y * 4;
      bounds(body); render(body); emit(body, 5, 2);
    } else if (event.key === " " || event.key === "Enter") {
      event.preventDefault(); body.pulse = 1; emit(body, 32, 6);
    }
  }

  function tick(now: number) {
    if (disposed) return;
    const step = Math.min(2, (now - (last || now - 16.67)) / (1000 / 60));
    last = now;
    const elapsed = now - started;
    if (scatter) {
      const t = Math.min(1, (now - scatter.start) / 900);
      const ease = t * t * t;
      bodies.forEach((body, index) => {
        const from = scatter!.from[index];
        const right = body.key === "pink" || body.key === "yellow";
        const bottom = body.key === "yellow" || body.key === "teal";
        const margin = body.radius * layout.scale;
        const targetX = ((right ? layout.width + margin : -margin) - layout.left) / layout.scale;
        const targetY = ((bottom ? layout.height + margin : -margin) - layout.top) / layout.scale;
        body.x = from.x + (targetX - from.x) * ease;
        body.y = from.y + (targetY - from.y) * ease;
        body.rotation = from.rotation + (right ? 50 : -50) * ease;
        body.scale = from.scale * (1 - .35 * ease);
        emit(body, 2, 1, true); render(body);
      });
    } else {
      bodies.forEach((body, index) => {
        if (elapsed < index * 140) return;
        if (!launched.has(body.key)) {
          launched.add(body.key);
          body.vx = (REST[body.key].x - 240) * .035;
          body.vy = (REST[body.key].y - 215) * .035;
          body.spin = (index % 2 ? -1 : 1) * .8;
          emit(body, 26, 5);
        }
        body.scale += (1 - body.scale) * (1 - .9 ** step);
        body.pulse *= .92 ** step;
        if (body !== dragging) {
          const rest = REST[body.key];
          let x = rest.x, y = rest.y;
          if (elapsed > 1100 && elapsed < 3000) {
            const angle = index * Math.PI / 2 + elapsed * .0011;
            x = 240 + Math.cos(angle) * 175; y = 215 + Math.sin(angle) * 130;
          } else if (elapsed >= 3000) {
            x += Math.sin(now * .0011 + index * 1.2) * 6;
            y += Math.cos(now * .0009 + index * 1.2) * 5;
          }
          const strength = elapsed < 1100 ? .008 : elapsed < 3000 ? .012 : .018;
          body.vx += (x - body.x) * strength * step;
          body.vy += (y - body.y) * strength * step;
          if (pointer && elapsed > 3000) {
            const distance = Math.hypot(pointer.x - body.x, pointer.y - body.y);
            if (distance > 1 && distance < 160) {
              const force = (1 - distance / 160) * .06 * step;
              body.vx += (pointer.x - body.x) / distance * force;
              body.vy += (pointer.y - body.y) / distance * force;
            }
          }
          body.vx *= .93 ** step; body.vy *= .93 ** step;
          body.x += body.vx * step; body.y += body.vy * step;
          body.spin += (Math.sin(now * .0007 + index) * 5 - body.rotation) * .002 * step;
          body.spin *= .95 ** step;
          body.rotation += body.spin * step;
          bounds(body);
        }
        if (elapsed < 3000 || body === dragging) {
          const origin = body === dragging ? { x: body.x + offset.x, y: body.y + offset.y } : body;
          emit(body, 1, .5, true, origin);
        }
      });
      for (let i = 0; i < bodies.length; i++) for (let j = i + 1; j < bodies.length; j++) collide(bodies[i], bodies[j]);
      bodies.forEach(render);
      sparkle += step;
      if (sparkle > 12) { sparkle = 0; emit(bodies[Math.floor(Math.random() * bodies.length)], 1, .7, true); }
    }
    drawParticles(step);
    frame = requestAnimationFrame(tick);
  }

  function resume() {
    cancelAnimationFrame(frame); frame = 0; last = 0;
    if (disposed) return;
    if (motion.matches) {
      particles.length = 0;
      ctx?.clearRect(0, 0, layout.width, layout.height);
      bodies.forEach(body => { Object.assign(body, REST[body.key], { scale: 1, rotation: 0, pulse: 0 }); render(body); });
    } else if (!document.hidden) frame = requestAnimationFrame(tick);
  }

  resize();
  const observer = new ResizeObserver(resize);
  observer.observe(stage); observer.observe(emblem);
  stage.addEventListener("pointerdown", down);
  stage.addEventListener("pointermove", move);
  stage.addEventListener("pointerup", release);
  stage.addEventListener("pointercancel", release);
  stage.addEventListener("lostpointercapture", release);
  stage.addEventListener("keydown", keydown);
  document.addEventListener("visibilitychange", resume);
  motion.addEventListener("change", resume);
  resume();

  return {
    scatter() {
      if (scatter) return;
      release();
      scatter = { start: performance.now(), from: bodies.map(body => ({ x: body.x, y: body.y, rotation: body.rotation, scale: body.scale })) };
      bodies.forEach(body => emit(body, 34, 7));
    },
    destroy() {
      disposed = true;
      release(); cancelAnimationFrame(frame); observer.disconnect();
      stage.removeEventListener("pointerdown", down);
      stage.removeEventListener("pointermove", move);
      stage.removeEventListener("pointerup", release);
      stage.removeEventListener("pointercancel", release);
      stage.removeEventListener("lostpointercapture", release);
      stage.removeEventListener("keydown", keydown);
      document.removeEventListener("visibilitychange", resume);
      motion.removeEventListener("change", resume);
      particles.length = 0;
      ctx?.clearRect(0, 0, layout.width, layout.height);
    },
  };
}
