// 使用确定性替身检查开场，不依赖浏览器或正在运行的控制服务。
// 在 frontend 中执行 npm ci 后，运行 node tools/check_intro_motion.mjs。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const require = createRequire(new URL("../frontend/package.json", import.meta.url));
const ts = require("typescript");
const source = readFileSync(new URL("../frontend/src/introMotion.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;

function fixture(reduced = false) {
  let now = 0, nextFrame = 0, painted = 0;
  const paintPoints = [];
  const frames = new Map();
  const shapeKeys = ["blue", "pink", "yellow", "teal"];
  const elements = shapeKeys.map(key => ({
    key, style: { transform: "", setProperty() {} }, classList: { add() {}, remove() {} },
    closest() { return this; },
  }));
  function target(box) {
    const listeners = new Map();
    return {
      listeners, getBoundingClientRect: () => box,
      addEventListener: (name, callback) => listeners.set(name, callback),
      removeEventListener: name => listeners.delete(name),
      send(name, event) { listeners.get(name)?.({ type: name, preventDefault() {}, ...event }); },
    };
  }
  const stage = target({ left: 0, top: 0, width: 1000, height: 800 });
  let captured = null;
  Object.assign(stage, {
    setPointerCapture: id => { captured = id; }, hasPointerCapture: id => id === captured,
    releasePointerCapture: () => { captured = null; },
  });
  const emblem = target({ left: 260, top: 160, width: 480, height: 430 });
  emblem.querySelector = selector => elements.find(el => selector.includes(el.key));
  const context = {
    setTransform() {}, clearRect() {}, save() {}, restore() {},
    translate(x, y) { paintPoints.push({ x, y, color: this.fillStyle }); }, rotate() {},
    beginPath() {}, moveTo() {}, lineTo() {}, closePath() {}, arc() {},
    fill() { painted++; }, fillRect() { painted++; },
  };
  const document = target({}); document.hidden = false;
  const motion = target({}); motion.matches = reduced;
  const sandbox = {
    exports: {}, window: { matchMedia: () => motion, devicePixelRatio: 3 }, document,
    performance: { now: () => now }, ResizeObserver: class { observe() {} disconnect() {} },
    requestAnimationFrame: fn => { frames.set(++nextFrame, fn); return nextFrame; },
    cancelAnimationFrame: id => frames.delete(id),
  };
  vm.runInNewContext(compiled, sandbox, { filename: fileURLToPath(new URL("../frontend/src/introMotion.ts", import.meta.url)) });
  const canvas = { getContext: () => context, width: 0, height: 0 };
  const controller = sandbox.exports.createIntroMotion(stage, emblem, canvas);
  function advance(milliseconds) {
    for (let elapsed = 0; elapsed < milliseconds; elapsed += 1000 / 60) {
      now += 1000 / 60;
      const pending = [...frames.values()]; frames.clear(); pending.forEach(fn => fn(now));
    }
  }
  function position(index) {
    const match = elements[index].style.transform.match(/translate\(([-\d.]+)px, ([-\d.]+)px\)/);
    return { x: Math.round(Number(match[1]) * 1e6) / 1e6, y: Math.round(Number(match[2]) * 1e6) / 1e6 };
  }
  function drag(index, x, y, offsetX = 0, offsetY = 0) {
    const origin = position(index);
    stage.send("pointerdown", { target: elements[index], button: 0, pointerId: 1, clientX: origin.x + offsetX + 260, clientY: origin.y + offsetY + 160 });
    advance(17);
    stage.send("pointermove", { target: stage, pointerId: 1, clientX: x + offsetX + 260, clientY: y + offsetY + 160 });
  }
  return { stage, elements, canvas, frames, controller, advance, position, drag, paintPoints, painted: () => painted };
}

const scene = fixture();
scene.advance(6500);
assert(scene.painted() > 0, "intro emits visible canvas particles");
assert.equal(scene.canvas.width, 2000, "DPR is capped at two");
scene.drag(0, 190, 200);
assert.deepEqual(scene.position(0), { x: 190, y: 200 }, "pointer capture moves a shape with the pointer");
const paintingBeforeDrag = scene.painted();
scene.advance(17);
assert(scene.painted() > paintingBeforeDrag, "dragging renders a particle trail");
scene.stage.send("pointerup", { pointerId: 1 });
scene.advance(34);
assert.notDeepEqual(scene.position(0), { x: 190, y: 200 }, "release preserves momentum");

// 散开从当前拖拽后的位置开始，重复触发不会重新启动。
const beforeScatter = scene.elements.map((_, index) => scene.position(index));
scene.controller.scatter(); scene.controller.scatter();
scene.advance(200);
const duringScatter = scene.elements.map((_, index) => scene.position(index));
for (let i = 0; i < 4; i++) {
  assert((duringScatter[i].x - beforeScatter[i].x) * ([1, 2].includes(i) ? 1 : -1) > 0, `shape ${i} exits toward its horizontal corner`);
  assert((duringScatter[i].y - beforeScatter[i].y) * (i >= 2 ? 1 : -1) > 0, `shape ${i} exits toward its vertical corner`);
}
scene.controller.destroy();
assert.equal(scene.frames.size, 0, "destroy cancels animation frames");
assert.equal(scene.stage.listeners.size, 0, "destroy removes pointer and keyboard listeners");
const destroyed = scene.position(0); scene.advance(100);
assert.deepEqual(scene.position(0), destroyed, "destroyed scene does not move");

const collision = fixture();
collision.advance(6500);
const pink = collision.position(1);
collision.drag(0, pink.x, pink.y);
collision.advance(34);
const pushed = collision.position(1);
assert(Math.hypot(pushed.x - pink.x, pushed.y - pink.y) > 20, `overlapping shapes separate: ${JSON.stringify({ before: pink, after: pushed })}`);
collision.controller.destroy();

const idle = fixture();
idle.advance(30000);
const idleBefore = idle.elements.map((_, index) => idle.position(index));
idle.advance(500);
assert(idle.elements.every((_, index) => Math.hypot(idle.position(index).x - idleBefore[index].x, idle.position(index).y - idleBefore[index].y) < 20), "idle physics settles into a compact pose instead of continuing to fling shapes apart");
idle.controller.destroy();

const contactScene = fixture();
contactScene.advance(6500);
contactScene.drag(0, 190, 200, 80, 0);
contactScene.paintPoints.length = 0;
contactScene.advance(17);
assert(contactScene.paintPoints.some(p => p.color === "#4BA3E3" && Math.hypot(p.x - 530, p.y - 360) < 4), "drag particles originate at the off-centre pointer contact rather than the body centre");
contactScene.controller.destroy();

const quiet = fixture(true);
assert.equal(quiet.frames.size, 0, "reduced motion does not run an idle animation loop");
quiet.drag(0, 190, 200);
assert.deepEqual(quiet.position(0), { x: 190, y: 200 }, "reduced motion still supports direct manipulation");
assert.equal(quiet.painted(), 0, "reduced motion suppresses particles");
quiet.stage.send("pointercancel", { pointerId: 1 });
quiet.stage.send("keydown", { target: quiet.elements[0], key: "ArrowRight" });
assert.equal(quiet.position(0).x, 206, "keyboard arrows provide a drag alternative");
quiet.controller.destroy();
console.log("Intro motion checks passed: dragging, particles, inertia, four-corner scatter, cleanup, reduced motion and keyboard control.");
