// Renders the HealthSwap system-design walkthrough: animated architecture and code-flow diagrams
// (system-design/scenes.mjs, drawn by system-design/player.js in Microsoft Edge), narrated with Windows text-to-speech,
// with burned-in captions, encoded with ffmpeg.
//
//   npm ci --prefix tools/demo-recorder
//   node tools/demo-recorder/record-system-design.mjs --ffmpeg <path to ffmpeg>
//
// Writes docs/media/system-design-walkthrough.mp4 (H.264 + AAC, with chapters), .webm (VP9 + Opus), .vtt (captions)
// and .png (poster). Narration needs Windows (System.Speech) and an ffmpeg with libx264, aac, libvpx-vp9 and libopus,
// for example the binary inside the `imageio-ffmpeg` wheel or a system ffmpeg on PATH. Without them (or with --no-voice)
// it renders a captions-only WebM, which Playwright's own ffmpeg can encode
// (`npx --prefix tools/demo-recorder playwright-core install ffmpeg`). Frames are rendered deterministically (a virtual
// clock, not real time), so the result does not depend on machine speed. --preview <dir> writes one PNG per beat instead
// of a video, for layout checks; --poster-only rewrites just the poster; --only <scene ids> limits the scenes.

import { execFile, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { once } from "node:events";
import { existsSync, readdirSync } from "node:fs";
import { mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs, promisify } from "node:util";

import { chromium } from "playwright-core";

import { meta, scenes as allScenes } from "./system-design/scenes.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "..", "..");
const assets = path.join(here, "system-design");
const run = promisify(execFile);

const { values: args } = parseArgs({
  options: {
    out: { type: "string", default: path.join(repo, "docs", "media") },
    name: { type: "string", default: "system-design-walkthrough" },
    ffmpeg: { type: "string" },
    voice: { type: "string", default: "Microsoft Zira Desktop" },
    rate: { type: "string", default: "0" },
    "no-voice": { type: "boolean", default: false },
    fps: { type: "string", default: "25" },
    preview: { type: "string" },
    "poster-only": { type: "boolean", default: false },
    only: { type: "string" },
    poster: { type: "string", default: "context" },
    channel: { type: "string", default: "msedge" },
  },
});

const FPS = Number(args.fps);
const SAMPLE_RATE = 24000;
const LEAD = 0.35; // silence before a beat's narration
const TAIL = 0.55; // silence after it
const SCENE_LEAD = 0.45; // extra time at the start of a scene (fade-in)
const END_HOLD = 1.5; // the last frame stays a little longer
const WORDS_PER_SECOND = 2.7; // estimate when there is no narration

// ---------- Narration text ----------

const SPEECH = [
  [/`([^`]*)`/g, "$1"],
  [/\*\*([^*]*)\*\*/g, "$1"],
  [/\bFP-1\b/g, "F P 1"],
  [/\bRA-1\b/g, "R A 1"],
  [/\bHSI\b/g, "H S I"],
  [/\bSDKs\b/g, "S D Ks"],
  [/\bSDK\b/g, "S D K"],
  [/\bLLMs\b/g, "L L Ms"],
  [/\bLLM\b/g, "L L M"],
  [/\bAPI\b/g, "A P I"],
  [/\bMCP\b/g, "M C P"],
  [/\bONNX\b/g, "O N N X"],
  [/\bSKUs\b/g, "S K Us"],
  [/\bSKU\b/g, "S K U"],
  [/\bGTIN\b/g, "G-tin"],
  [/\bES256\b/g, "E S 256"],
  [/\bOAuth\b/g, "O-Auth"],
  [/\bRLS\b/g, "R L S"],
  [/\bPostgreSQL\b/g, "Postgres"],
  [/\bJSON\b/g, "Jason"],
  [/\bB2B\b/g, "business-to-business"],
  [/\bRFC 9457\b/g, "R F C 94 57"],
  [/\bscikit-learn\b/g, "sci-kit learn"],
  [/\bAzure OpenAI\b/g, "Azure Open A I"],
  [/\bblue\/green\b/g, "blue-green"],
];

const plain = (text) => String(text).replace(/`([^`]*)`/g, "$1").replace(/\*\*([^*]*)\*\*/g, "$1");
const speakable = (text) => SPEECH.reduce((s, [re, to]) => s.replace(re, to), String(text));
const words = (text) => plain(text).split(/\s+/).filter(Boolean).length;

// ---------- WAV ----------

function readWav(buffer) {
  if (buffer.toString("ascii", 0, 4) !== "RIFF" || buffer.toString("ascii", 8, 12) !== "WAVE") throw new Error("not a WAV file");
  let offset = 12;
  let fmt = null;
  while (offset + 8 <= buffer.length) {
    const id = buffer.toString("ascii", offset, offset + 4);
    const size = buffer.readUInt32LE(offset + 4);
    const body = offset + 8;
    if (id === "fmt ") {
      fmt = { format: buffer.readUInt16LE(body), channels: buffer.readUInt16LE(body + 2), rate: buffer.readUInt32LE(body + 4), bits: buffer.readUInt16LE(body + 14) };
    } else if (id === "data") {
      if (!fmt || fmt.format !== 1 || fmt.channels !== 1 || fmt.bits !== 16 || fmt.rate !== SAMPLE_RATE) {
        throw new Error(`unexpected WAV format ${JSON.stringify(fmt)}`);
      }
      const bytes = new Uint8Array(buffer.subarray(body, body + size)); // copy: 2-byte aligned
      return new Int16Array(bytes.buffer, 0, Math.floor(bytes.length / 2));
    }
    offset = body + size + (size % 2);
  }
  throw new Error("WAV file without data");
}

function wavFile(samples) {
  const header = Buffer.alloc(44);
  const dataBytes = samples.length * 2;
  header.write("RIFF", 0, "ascii");
  header.writeUInt32LE(36 + dataBytes, 4);
  header.write("WAVE", 8, "ascii");
  header.write("fmt ", 12, "ascii");
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(SAMPLE_RATE, 24);
  header.writeUInt32LE(SAMPLE_RATE * 2, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36, "ascii");
  header.writeUInt32LE(dataBytes, 40);
  return Buffer.concat([header, Buffer.from(samples.buffer, samples.byteOffset, dataBytes)]);
}

async function synthesize(beats) {
  const cache = path.join(tmpdir(), "hs-system-design-tts");
  await mkdir(cache, { recursive: true });
  for (const b of beats) {
    const hash = createHash("sha256").update(`${args.voice}|${args.rate}|${b.speech}`).digest("hex").slice(0, 24);
    b.wav = path.join(cache, `${hash}.wav`);
  }
  const missing = beats.filter((b) => !existsSync(b.wav));
  if (missing.length) {
    console.log(`Synthesizing ${missing.length} narration clip(s) with ${args.voice} ...`);
    const manifest = path.join(cache, `manifest-${process.pid}.json`);
    await writeFile(manifest, JSON.stringify(missing.map((b) => ({ text: b.speech, file: b.wav }))), "utf8");
    await run("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path.join(assets, "tts-windows.ps1"),
      "-Manifest", manifest, "-Voice", args.voice, "-Rate", String(Number(args.rate))], { maxBuffer: 1 << 24 });
  }
  for (const b of beats) {
    b.clip = readWav(await readFile(b.wav));
    b.speechSeconds = b.clip.length / SAMPLE_RATE;
  }
}

// ---------- ffmpeg ----------

async function ffmpegInfo(file) {
  try {
    const { stdout: encoders } = await run(file, ["-hide_banner", "-encoders"], { maxBuffer: 1 << 24 });
    const { stdout: decoders } = await run(file, ["-hide_banner", "-decoders"], { maxBuffer: 1 << 24 });
    const { stdout: filters } = await run(file, ["-hide_banner", "-filters"], { maxBuffer: 1 << 24 });
    const has = (list, name) => new RegExp(`\\s${name}\\s`).test(list);
    return {
      file, x264: has(encoders, "libx264"), aac: has(encoders, "aac"), vp9: has(encoders, "libvpx-vp9"), opus: has(encoders, "libopus"),
      png: has(decoders, "png"), setparams: has(filters, "setparams"),
    };
  } catch {
    return null;
  }
}

async function findFfmpeg() {
  const candidates = [];
  if (args.ffmpeg) candidates.push(args.ffmpeg);
  candidates.push("ffmpeg");
  const defaults = { win32: path.join(process.env.LOCALAPPDATA || "", "ms-playwright"), darwin: path.join(homedir(), "Library", "Caches", "ms-playwright") };
  const root = process.env.PLAYWRIGHT_BROWSERS_PATH || defaults[process.platform] || path.join(homedir(), ".cache", "ms-playwright");
  if (existsSync(root)) {
    for (const dir of readdirSync(root).filter((d) => /^ffmpeg-\d+$/.test(d)).sort().reverse()) {
      const exe = readdirSync(path.join(root, dir)).find((f) => /^ffmpeg/.test(f) && !f.endsWith(".txt"));
      if (exe) candidates.push(path.join(root, dir, exe));
    }
  }
  for (const c of candidates) {
    const info = await ffmpegInfo(c);
    if (info && info.vp9) return info;
  }
  return null;
}

// ---------- Timeline ----------

function timeline(scenes, voiced) {
  let t = 0;
  const beats = [];
  for (const scene of scenes) {
    scene.start = t;
    scene.beats.forEach((b, i) => {
      const lead = LEAD + (i === 0 ? SCENE_LEAD : 0);
      const speech = voiced ? b.speechSeconds : words(b.text) / WORDS_PER_SECOND;
      const seg = b.seg || 1.15;
      const longestChain = Math.max(0, ...(b.flow || []).map((c) => c.length));
      const flowMin = longestChain ? 0.35 + longestChain * seg + 1.2 : 0;
      b.start = t;
      b.voiceAt = t + lead;
      b.end = t + Math.max(lead + speech + TAIL, flowMin, b.min || 0);
      t = b.end;
      beats.push(b);
    });
    scene.end = t;
  }
  const last = scenes[scenes.length - 1];
  last.beats[last.beats.length - 1].end += END_HOLD;
  last.end += END_HOLD;
  return { total: last.end, beats };
}

const stamp = (seconds, sep = ".") => {
  const ms = Math.round(seconds * 1000);
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  const s = Math.floor((ms % 60000) / 1000);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}${sep}${String(ms % 1000).padStart(3, "0")}`;
};

// ---------- Rendering ----------

async function openPlayer(scenes, total) {
  const browser = await chromium.launch({ channel: args.channel === "chromium" ? undefined : args.channel, headless: true });
  const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
  const css = await readFile(path.join(assets, "player.css"), "utf8");
  const js = await readFile(path.join(assets, "player.js"), "utf8");
  await page.setContent(`<!doctype html><html lang="en"><head><meta charset="utf-8"><style>${css}</style></head><body>
    <div id="stage"><div id="grid"></div><div id="scene"></div>
      <div id="header"><div id="chapter"></div><div id="section"></div><div id="brand"></div></div><div id="dots"></div>
      <div id="caption"><div id="caption-text"></div></div></div></body></html>`);
  await page.addScriptTag({ content: js });
  await page.evaluate(async () => {
    await document.fonts.ready;
  });
  const data = { ...meta, total, scenes: scenes.map(({ id, chapter, section, start, end, nodes, groups, edges, cards, lists, texts, beats }) => ({
    id, chapter, section, start, end, nodes, groups, edges, cards, lists, texts,
    beats: beats.map(({ text, show, focus, flow, packet, packets, seg, on, lines, hl, start: bs, end: be }) =>
      ({ text, show, focus, flow, packet, packets, seg, on, lines, hl, start: bs, end: be })),
  })) };
  await page.evaluate((d) => window.HSVideo.load(d), data);
  const cdp = await page.context().newCDPSession(page);
  // Lossless frames: JPEG ringing around text costs more bits in the video than the PNG capture costs in time.
  // Playwright's minimal ffmpeg decodes only MJPEG, so `jpeg` stays available for it.
  const shot = async ({ fast = false, jpeg = false } = {}) => {
    const params = jpeg ? { format: "jpeg", quality: 92, optimizeForSpeed: true } : { format: "png", optimizeForSpeed: fast };
    const { data: b64 } = await cdp.send("Page.captureScreenshot", params);
    return Buffer.from(b64, "base64");
  };
  const renderAt = (t, options = {}) => page.evaluate(([x, o]) => window.HSVideo.renderAt(x, o), [t, options]);
  return { browser, renderAt, shot };
}

async function writePoster(scenes, renderAt, shot, file) {
  const scene = scenes.find((s) => s.id === args.poster) || scenes[0];
  await renderAt(scene.end - 0.6, { still: true });
  await writeFile(file, await shot());
}

async function main() {
  const selected = args.only ? new Set(args.only.split(",").map((s) => s.trim())) : null;
  const scenes = structuredClone(allScenes).filter((s) => !selected || selected.has(s.id));
  if (scenes.length === 0) throw new Error(`No scenes match --only ${args.only}`);
  const beats = scenes.flatMap((s) => s.beats);
  for (const b of beats) b.speech = speakable(b.say || b.text);

  const preview = Boolean(args.preview);
  const posterOnly = args["poster-only"];
  let ffmpeg = null;
  let voiced = !args["no-voice"] && !preview && !posterOnly && process.platform === "win32";
  if (!preview && !posterOnly) {
    ffmpeg = await findFfmpeg();
    if (!ffmpeg) throw new Error("No ffmpeg with libvpx-vp9 found: pass --ffmpeg <path> or run `npx --prefix tools/demo-recorder playwright-core install ffmpeg`");
    if (voiced && !(ffmpeg.opus || ffmpeg.aac)) {
      console.warn(`record-system-design: ${ffmpeg.file} has no audio encoder; rendering captions only (pass --ffmpeg with libopus/aac for narration)`);
      voiced = false;
    }
  }
  if (voiced) await synthesize(beats);
  const { total } = timeline(scenes, voiced);
  console.log(`Timeline: ${scenes.length} scenes, ${beats.length} beats, ${stamp(total)}`);

  const { browser, renderAt, shot } = await openPlayer(scenes, total);
  let work = null;
  try {
    if (preview) {
      await mkdir(args.preview, { recursive: true });
      for (const scene of scenes) {
        for (const [i, b] of scene.beats.entries()) {
          await renderAt(Math.min(b.end - 0.05, scene.end - 0.45));
          await writeFile(path.join(args.preview, `${scene.id}-${i + 1}.png`), await shot());
        }
      }
      console.log(`Preview frames: ${args.preview}`);
      return;
    }
    if (posterOnly) {
      await mkdir(args.out, { recursive: true });
      await writePoster(scenes, renderAt, shot, path.join(args.out, `${args.name}.png`));
      console.log(`Poster: ${path.join(args.out, `${args.name}.png`)}`);
      return;
    }

    await mkdir(args.out, { recursive: true });
    work = path.join(tmpdir(), `hs-system-design-${process.pid}`);
    await mkdir(work, { recursive: true });
    const base = path.join(args.out, args.name);
    const inputs = ["-f", "image2pipe", "-framerate", String(FPS), "-c:v", ffmpeg.png ? "png" : "mjpeg", "-i", "pipe:0"];
    let audioInput = null;
    let chapterInput = null;
    if (voiced) {
      const mix = new Int16Array(Math.ceil(total * SAMPLE_RATE));
      for (const b of beats) mix.set(b.clip.subarray(0, Math.max(0, mix.length - Math.round(b.voiceAt * SAMPLE_RATE))), Math.round(b.voiceAt * SAMPLE_RATE));
      const wav = path.join(work, "narration.wav");
      await writeFile(wav, wavFile(mix));
      inputs.push("-i", wav);
      audioInput = 1;
    }
    const esc = (v) => String(v).replace(/[=;#\\\n]/g, (c) => `\\${c}`);
    const chapters = [";FFMETADATA1", `title=${esc(`${meta.brand} · ${meta.subtitle}`)}`];
    for (const s of scenes) {
      chapters.push("[CHAPTER]", "TIMEBASE=1/1000", `START=${Math.round(s.start * 1000)}`, `END=${Math.round(s.end * 1000)}`,
        `title=${esc(`${s.chapter ? `${s.chapter} ` : ""}${s.section}`)}`);
    }
    const chapterFile = path.join(work, "chapters.txt");
    await writeFile(chapterFile, chapters.join("\n") + "\n", "utf8");
    inputs.push("-i", chapterFile);
    chapterInput = audioInput ? 2 : 1;

    const outputs = [];
    const audioMap = audioInput ? ["-map", `${audioInput}:a`] : [];
    // Standard BT.709 limited-range video (browsers and players assume it), a keyframe at every chapter for seeking.
    const tags = ffmpeg.setparams ? ",setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv" : "";
    const video = ["-vf", `scale=out_color_matrix=bt709:out_range=tv,format=yuv420p${tags}`, "-colorspace", "bt709",
      "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv", "-g", String(FPS * 20),
      "-force_key_frames", scenes.map((s) => s.start.toFixed(3)).join(",")];
    if (ffmpeg.x264 && (!voiced || ffmpeg.aac)) {
      outputs.push(`${base}.mp4`);
      inputs.push("-map", "0:v", ...audioMap, "-map_metadata", String(chapterInput), "-map_chapters", String(chapterInput),
        "-c:v", "libx264", "-preset", "slow", "-tune", "animation", "-crf", "22", ...video, "-movflags", "+faststart",
        ...(voiced ? ["-c:a", "aac", "-b:a", "64k"] : []), `${base}.mp4`);
    }
    if (!voiced || ffmpeg.opus) {
      outputs.push(`${base}.webm`);
      inputs.push("-map", "0:v", ...(voiced ? audioMap : []), "-map_metadata", String(chapterInput), "-map_chapters", String(chapterInput),
        "-c:v", "libvpx-vp9", "-crf", "36", "-b:v", "0", "-deadline", "good", "-cpu-used", "3", "-row-mt", "1", "-tune-content", "screen",
        ...video, ...(voiced ? ["-c:a", "libopus", "-b:a", "32k"] : []), `${base}.webm`);
    }
    const ff = spawn(ffmpeg.file, ["-hide_banner", "-loglevel", "error", "-y", ...inputs], { stdio: ["pipe", "inherit", "inherit"] });
    const exited = once(ff, "exit");
    const frames = Math.ceil(total * FPS);
    const started = Date.now();
    let lastKey = null;
    let frame = null;
    let shots = 0;
    for (let f = 0; f < frames; f++) {
      const key = await renderAt(f / FPS);
      if (key !== lastKey) {
        frame = await shot({ fast: true, jpeg: !ffmpeg.png });
        lastKey = key;
        shots++;
      }
      if (!ff.stdin.write(frame)) await once(ff.stdin, "drain");
      if (f % (FPS * 60) === 0 && f > 0) console.log(`  ${stamp(f / FPS)} rendered (${shots} distinct frames, ${Math.round((Date.now() - started) / 1000)} s)`);
    }
    ff.stdin.end();
    const [code] = await exited;
    if (code !== 0) throw new Error(`ffmpeg exited with code ${code}`);

    await writePoster(scenes, renderAt, shot, `${base}.png`);
    const vtt = ["WEBVTT", ""];
    beats.forEach((b, i) => vtt.push(String(i + 1), `${stamp(b.start + 0.1)} --> ${stamp(b.end)}`, plain(b.text), ""));
    await writeFile(`${base}.vtt`, vtt.join("\n"), "utf8");

    console.log(`Rendered ${frames} frames (${shots} distinct) in ${Math.round((Date.now() - started) / 1000)} s`);
    for (const file of [...outputs, `${base}.vtt`, `${base}.png`]) console.log(`  ${file} (${((await stat(file)).size / 1e6).toFixed(1)} MB)`);
    console.log("Chapters:");
    for (const s of scenes) console.log(`  ${stamp(s.start).slice(3, 8)}  ${s.chapter ? `${s.chapter} ` : ""}${s.section}`);
  } finally {
    await browser.close();
    if (work) await rm(work, { recursive: true, force: true });
  }
}

await main();
