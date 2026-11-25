// Records the client-app integration demo: the QuickBasket demo shop (web/demo) using the HealthSwap web SDK (sdk/web)
// against a running local stack, with a caption naming the SDK call and API request behind every step.
//
//   npm ci --prefix tools/demo-recorder
//   npx --prefix tools/demo-recorder playwright-core install ffmpeg     # Playwright's video encoder, once per machine
//   python scripts/run_local.py --memory                                 # in another terminal
//   node tools/demo-recorder/record-demo.mjs --base-url http://127.0.0.1:8080
//
// Writes docs/media/client-integration-demo.webm (the video) and docs/media/client-integration-demo.gif (one frame per
// step, embedded in docs/09). Drives an installed Microsoft Edge by default; use --channel chrome, or --channel chromium
// after `npx --prefix tools/demo-recorder playwright-core install chromium`. Every page load is a new demo cart, so a
// recording never changes what other demo users see. Playwright records at a high bit rate, so the video is re-encoded
// (VP9, 15 frames/s) with Playwright's ffmpeg, or with --ffmpeg <path>; --frames <dir> also saves the storyboard as PNGs.

import { execFile } from "node:child_process";
import { existsSync, readdirSync } from "node:fs";
import { copyFile, mkdir, mkdtemp, rm, stat, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs, promisify } from "node:util";

import gifenc from "gifenc"; // CommonJS packages: default imports
import { chromium } from "playwright-core";
import pngjs from "pngjs";

const { applyPalette, GIFEncoder, quantize } = gifenc;
const { PNG } = pngjs;
const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "..", "..");
const { values: args } = parseArgs({
  options: {
    "base-url": { type: "string", default: "http://127.0.0.1:8080" },
    out: { type: "string", default: path.join(repo, "docs", "media") },
    name: { type: "string", default: "client-integration-demo" },
    channel: { type: "string", default: "msedge" },
    headed: { type: "boolean", default: false },
    ffmpeg: { type: "string" },
    frames: { type: "string" },
  },
});

const BASE = args["base-url"].replace(/\/+$/, "");
const WIDTH = 1280;
const HEIGHT = 1000;
const CAPTION_HEIGHT = 96; // the bottom caption bar; content is kept above it
const GIF_SCALE = 0.75;
const STEPS = 9;

// ---------- Overlay: caption bar and a highlight ring (recording only; the demo page itself is unchanged) ----------

async function installOverlay(page) {
  await page.evaluate((height) => {
    const bar = document.createElement("div");
    Object.assign(bar.style, {
      position: "fixed", left: "0", right: "0", bottom: "0", height: `${height}px`, boxSizing: "border-box", zIndex: "2147483647",
      padding: "14px 28px", background: "rgba(15, 23, 42, 0.95)", color: "#ffffff", pointerEvents: "none",
      fontFamily: "'Segoe UI', system-ui, sans-serif", boxShadow: "0 -2px 12px rgba(0, 0, 0, 0.25)",
    });
    const title = document.createElement("div");
    title.id = "rec-title";
    Object.assign(title.style, { fontSize: "21px", fontWeight: "600", marginBottom: "6px" });
    const detail = document.createElement("div");
    detail.id = "rec-detail";
    Object.assign(detail.style, { fontSize: "16px", lineHeight: "1.45", color: "#e2e8f0" });
    bar.append(title, detail);
    const ring = document.createElement("div");
    ring.id = "rec-ring";
    Object.assign(ring.style, {
      position: "absolute", zIndex: "2147483646", display: "none", pointerEvents: "none", borderRadius: "10px",
      border: "3px solid #f59e0b", boxShadow: "0 0 0 5px rgba(245, 158, 11, 0.3)",
    });
    document.body.append(bar, ring);
    document.body.style.paddingBottom = `${height}px`; // the last content can scroll above the bar
  }, CAPTION_HEIGHT);
}

/** Shows a caption; `text` spans in the detail are rendered as code. */
async function caption(page, title, detail) {
  await page.evaluate(([t, d]) => {
    document.getElementById("rec-title").textContent = t;
    const el = document.getElementById("rec-detail");
    el.replaceChildren();
    for (const part of d.split(/(`[^`]+`)/)) {
      if (part.length > 1 && part.startsWith("`") && part.endsWith("`")) {
        const code = document.createElement("code");
        code.textContent = part.slice(1, -1);
        Object.assign(code.style, {
          fontFamily: "'Cascadia Mono', Consolas, monospace", fontSize: "15px", color: "#fde68a",
          background: "rgba(255, 255, 255, 0.12)", padding: "1px 6px", borderRadius: "4px",
        });
        el.append(code);
      } else if (part) {
        el.append(part);
      }
    }
  }, [title, detail]);
}

async function highlight(page, locator, ms = 800) {
  const box = await locator.boundingBox();
  if (!box) throw new Error(`Cannot highlight ${locator}: not visible`);
  await page.evaluate(({ x, y, w, h }) => {
    const ring = document.getElementById("rec-ring");
    Object.assign(ring.style, {
      display: "block", left: `${x + window.scrollX - 6}px`, top: `${y + window.scrollY - 6}px`, width: `${w + 12}px`, height: `${h + 12}px`,
    });
  }, { x: box.x, y: box.y, w: box.width, h: box.height });
  await page.waitForTimeout(ms);
}

async function unhighlight(page) {
  await page.evaluate(() => {
    document.getElementById("rec-ring").style.display = "none";
  });
}

async function scrollTo(page, top) {
  await page.evaluate((y) => window.scrollTo({ top: y, behavior: "smooth" }), Math.max(0, Math.round(top)));
  await page.waitForTimeout(900);
}

/** Scrolls (smoothly) so that the element is fully visible above the caption bar. */
async function reveal(page, locator, margin = 24) {
  const box = await locator.boundingBox();
  const scrollY = await page.evaluate(() => window.scrollY);
  const bottom = HEIGHT - CAPTION_HEIGHT - margin;
  if (box.y + box.height > bottom) await scrollTo(page, scrollY + box.y + box.height - bottom);
  else if (box.y < margin) await scrollTo(page, scrollY + box.y - margin);
}

async function click(page, locator) {
  await reveal(page, locator);
  await highlight(page, locator);
  await locator.click();
  await unhighlight(page);
}

/** Scrolls as little as possible from the top so that the whole partner view (the RA-1 charges) is above the caption bar. */
async function showPartnerView(page) {
  const box = await page.locator(".partner").boundingBox();
  const scrollY = await page.evaluate(() => window.scrollY);
  await scrollTo(page, scrollY + box.y + box.height - (HEIGHT - CAPTION_HEIGHT - 16));
}

const logCount = (page, text, count = 1) => page.waitForFunction(([t, n]) =>
  [...document.querySelectorAll("#event-log li")].filter((li) => li.textContent.includes(t)).length >= n, [text, count], { timeout: 20000 });

const chargeRow = (page, eventType, state, count = 1) => page.waitForFunction(([e, s, n]) =>
  [...document.querySelectorAll("#charges tr")].filter((tr) => tr.textContent.includes(e) && tr.textContent.includes(s)).length >= n,
[eventType, state, count], { timeout: 20000 });

// ---------- GIF storyboard ----------

/** Area-average weights for resampling `src` pixels to `dst` pixels along one axis. */
function weights(src, dst) {
  const ratio = src / dst;
  return Array.from({ length: dst }, (_, d) => {
    const start = d * ratio;
    const end = start + ratio;
    const list = [];
    for (let s = Math.floor(start); s < Math.min(src, Math.ceil(end)); s++) {
      const overlap = Math.min(end, s + 1) - Math.max(start, s);
      if (overlap > 0) list.push([s, overlap / ratio]);
    }
    return list;
  });
}

function downscale({ width, height, data }, scale) {
  const w = Math.round(width * scale);
  const h = Math.round(height * scale);
  const xw = weights(width, w);
  const yw = weights(height, h);
  const rows = new Float32Array(w * height * 4);
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < w; x++) {
      const o = (y * w + x) * 4;
      for (const [sx, k] of xw[x]) {
        const i = (y * width + sx) * 4;
        rows[o] += data[i] * k;
        rows[o + 1] += data[i + 1] * k;
        rows[o + 2] += data[i + 2] * k;
      }
      rows[o + 3] = 255;
    }
  }
  const out = new Uint8ClampedArray(w * h * 4);
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const o = (y * w + x) * 4;
      let r = 0;
      let g = 0;
      let b = 0;
      for (const [sy, k] of yw[y]) {
        const i = (sy * w + x) * 4;
        r += rows[i] * k;
        g += rows[i + 1] * k;
        b += rows[i + 2] * k;
      }
      out[o] = r;
      out[o + 1] = g;
      out[o + 2] = b;
      out[o + 3] = 255;
    }
  }
  return { width: w, height: h, data: out };
}

function encodeGif(frames) {
  const gif = GIFEncoder();
  for (const frame of frames) {
    const image = downscale(PNG.sync.read(frame.png), GIF_SCALE);
    const palette = quantize(image.data, 256);
    gif.writeFrame(applyPalette(image.data, palette), image.width, image.height, { palette, delay: frame.delay });
  }
  gif.finish();
  return gif.bytes();
}

// ---------- Video: re-encode Playwright's high-bit-rate recording ----------

/** --ffmpeg, else the newest ffmpeg that `playwright-core install ffmpeg` downloaded (PLAYWRIGHT_BROWSERS_PATH aware). */
function findFfmpeg() {
  if (args.ffmpeg) return args.ffmpeg;
  const defaults = { win32: path.join(process.env.LOCALAPPDATA || "", "ms-playwright"), darwin: path.join(homedir(), "Library", "Caches", "ms-playwright") };
  const root = process.env.PLAYWRIGHT_BROWSERS_PATH || defaults[process.platform] || path.join(homedir(), ".cache", "ms-playwright");
  if (!existsSync(root)) return null;
  const dirs = readdirSync(root).filter((d) => /^ffmpeg-\d+$/.test(d)).sort((a, b) => Number(b.slice(7)) - Number(a.slice(7)));
  for (const dir of dirs) {
    const exe = readdirSync(path.join(root, dir)).find((f) => /^ffmpeg(-[a-z0-9]+)*(\.exe)?$/.test(f));
    if (exe) return path.join(root, dir, exe);
  }
  return null;
}

async function reencode(input, output) {
  const ffmpeg = findFfmpeg();
  if (!ffmpeg) {
    console.warn("record-demo: no ffmpeg found (npx --prefix tools/demo-recorder playwright-core install ffmpeg); keeping the raw recording");
    return false;
  }
  await promisify(execFile)(ffmpeg, [
    "-hide_banner", "-loglevel", "error", "-y", "-i", input, "-an", "-r", "15",
    "-c:v", "libvpx-vp9", "-crf", "40", "-b:v", "0", "-deadline", "good", "-cpu-used", "2", "-row-mt", "1", output,
  ]);
  return true;
}

// ---------- The walkthrough ----------

async function walkthrough(page, frames) {
  const snap = async (delay = 3500) => frames.push({ png: await page.screenshot(), delay });
  const pause = (ms) => page.waitForTimeout(ms);
  const cards = page.locator(".hs-card");

  await page.goto(`${BASE}/demo`);
  await page.waitForSelector("#cart-lines tr");
  await installOverlay(page);

  await caption(page, "QuickBasket web shop + HealthSwap web SDK",
    "The shop's backend mints a short-lived SDK session (`POST /v1/sdk/sessions`) bound to this cart; the browser never holds partner credentials.");
  await pause(4500);
  await snap(4500);

  await caption(page, `1/${STEPS} · Evaluate the cart`,
    "`hs.evaluateCart(cart)` → `POST /v1/cart-evaluations` with the session token. It fails open after 1.5 s, so the cart never waits.");
  await pause(1500);
  await click(page, page.locator("#find-swaps"));
  await logCount(page, "suggestion(s)");
  await pause(3000);
  await snap();

  await caption(page, `2/${STEPS} · Render the labeled suggestions`,
    "`hs.renderSuggestions(container, evaluation, { onAdd, onUndo, onDismiss })`: label, fee disclosure, grade, price difference and a factual reason.");
  await highlight(page, cards.nth(0).locator(".hs-alt").first(), 4000);
  await snap();
  await unhighlight(page);

  await caption(page, `3/${STEPS} · Report impressions`,
    "`hs.trackImpressions(container)`: one IMPRESSION per card once ≥ 50% of it is visible for 1 s, batched to `POST /v1/events` (RA-1 evidence).");
  await logCount(page, "Sent ");
  await highlight(page, page.locator("#event-log"), 3500);
  await snap();
  await unhighlight(page);

  await caption(page, `4/${STEPS} · Explain the swap`,
    "“Why?” lists the nutrient facts with their basis, the fee disclosure, the AI-assistance note and the methodology link.");
  const why = cards.nth(0).locator(".hs-why-toggle").first();
  await click(page, why);
  await reveal(page, cards.nth(0).locator(".hs-why-panel").first());
  await pause(4500);
  await snap();
  await why.click(); // collapse again
  await scrollTo(page, 0);

  await caption(page, `5/${STEPS} · Add the recommended product`,
    "The SDK sends ACCEPT and calls your `onAdd`: your code swaps the cart line and shows the label. Partner view: one INR 1.00 PENDING RA-1 charge.");
  await pause(1500);
  await click(page, cards.nth(0).locator(".hs-alt .hs-button--primary").first());
  await chargeRow(page, "ACCEPT", "PENDING");
  await showPartnerView(page);
  await highlight(page, page.locator(".partner"), 4000);
  await snap(4000);
  await unhighlight(page);

  await caption(page, `6/${STEPS} · Undo within 5 seconds`,
    "Add, then Undo: the SDK sends UNDO and calls your `onUndo` to restore the line; the charge becomes VOID (UNDONE_WITHIN_GRACE).");
  await logCount(page, "IMPRESSION queued", 2);
  await pause(1200);
  await click(page, cards.nth(1).locator(".hs-alt .hs-button--primary").first());
  await pause(1300);
  await click(page, cards.nth(1).locator(".hs-undo button"));
  await chargeRow(page, "ACCEPT", "VOID");
  await scrollTo(page, 0);
  await showPartnerView(page);
  await pause(3500);
  await snap(4000);

  await caption(page, `7/${STEPS} · More units of a labeled line`,
    "Raising the quantity calls `hs.addToCart(suggestionId, sku, { quantity: 1 })` → ADD_TO_CART → one more PENDING charge. Lowering it sends nothing.");
  const quantity = page.locator("#qty-1");
  await highlight(page, quantity, 1200);
  await quantity.fill("3");
  await quantity.evaluate((input) => input.blur()); // commits the value: the input's change event
  await unhighlight(page);
  await chargeRow(page, "ADD_TO_CART", "PENDING");
  await showPartnerView(page);
  await pause(3500);
  await snap(4000);

  await caption(page, `8/${STEPS} · Keep original`,
    "DISMISS: your `onDismiss` runs, nothing is charged, and this swap is not suggested again for 7 days.");
  await pause(1500);
  await click(page, cards.nth(1).locator(".hs-card-actions .hs-button--secondary"));
  await logCount(page, "DISMISS queued");
  await pause(3000);
  await snap();

  await caption(page, `9/${STEPS} · Evaluate the same cart again`,
    "The line holding the HealthSwap pick gets no new suggestion, and the dismissed swap is in its cool-down.");
  await scrollTo(page, 0);
  await click(page, page.locator("#find-swaps"));
  await page.waitForFunction(() => document.getElementById("status").textContent.includes("No healthier swaps"), null, { timeout: 20000 });
  await showPartnerView(page);
  await pause(3500);
  await snap();

  await caption(page, "Then, on your server",
    "Send the order with each line's `suggestionId` (FP-1), verify signed webhooks and reconcile the charges: docs/09-client-app-integration.md.");
  await pause(5000);
  await snap(5000);
}

async function main() {
  try {
    const health = await fetch(`${BASE}/healthz`);
    if (!health.ok) throw new Error(`HTTP ${health.status}`);
  } catch (error) {
    console.error(`record-demo: the API is not reachable at ${BASE} (${error.message}). Start it with: python scripts/run_local.py --memory`);
    process.exit(1);
  }
  await mkdir(args.out, { recursive: true });
  const videoDir = await mkdtemp(path.join(tmpdir(), "hs-demo-video-"));
  const browser = await chromium.launch({ channel: args.channel === "chromium" ? undefined : args.channel, headless: !args.headed });
  const frames = [];
  const raw = path.join(videoDir, "raw.webm");
  const webm = path.join(args.out, `${args.name}.webm`);
  const gif = path.join(args.out, `${args.name}.gif`);
  try {
    const context = await browser.newContext({
      viewport: { width: WIDTH, height: HEIGHT }, deviceScaleFactor: 1, locale: "en-IN", timezoneId: "Asia/Kolkata",
      recordVideo: { dir: videoDir, size: { width: WIDTH, height: HEIGHT } },
    });
    const page = await context.newPage();
    const started = Date.now();
    await walkthrough(page, frames);
    const video = page.video();
    await context.close();
    await video.saveAs(raw);
    await browser.close();
    console.log(`Recorded about ${Math.round((Date.now() - started) / 1000)} s; encoding ...`);
    if (!(await reencode(raw, webm))) await copyFile(raw, webm);
    console.log(`Video: ${webm} (${((await stat(webm)).size / 1e6).toFixed(1)} MB)`);
  } finally {
    await browser.close(); // no-op when already closed
    await rm(videoDir, { recursive: true, force: true });
  }
  await writeFile(gif, encodeGif(frames));
  console.log(`Storyboard: ${gif} (${frames.length} frames, ${((await stat(gif)).size / 1e6).toFixed(1)} MB)`);
  if (args.frames) {
    await mkdir(args.frames, { recursive: true });
    await Promise.all(frames.map((f, i) => writeFile(path.join(args.frames, `step-${String(i).padStart(2, "0")}.png`), f.png)));
    console.log(`Frames: ${args.frames}`);
  }
}

await main();
