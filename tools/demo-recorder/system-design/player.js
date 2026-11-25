/* HealthSwap system-design video player: renders a scene graph (nodes, groups, edges, cards, lists, texts) on a
   1920 x 1080 stage and animates it deterministically. `HSVideo.renderAt(t)` sets every animated value for time t (in
   seconds) and returns a key that only changes when the picture changes, so a recorder can reuse unchanged frames. */
(function () {
  "use strict";

  const KINDS = {
    client: "#f59e0b", api: "#60a5fa", service: "#2dd4bf", domain: "#a78bfa", store: "#94a3b8", worker: "#f472b6",
    external: "#fb923c", data: "#facc15", ml: "#4ade80", neutral: "#cbd5e1", danger: "#f87171",
  };
  const FADE = 0.45; // element fade-in
  const FOCUS_FADE = 0.35; // dimming of out-of-focus elements
  const SCENE_FADE = 0.4; // scene fade in and out
  const CAPTION_FADE = 0.25;
  const DIM = 0.16;
  const GROUP_DIM = 0.4;
  const SVG_NS = "http://www.w3.org/2000/svg";
  const NORMAL = { left: [-1, 0], right: [1, 0], top: [0, -1], bottom: [0, 1] };

  let video = null;
  let built = -1;
  let parts = new Map();
  let packets = [];
  let captionBeat = null;

  const clamp01 = (x) => Math.min(1, Math.max(0, x));
  const smooth = (x) => {
    const c = clamp01(x);
    return c * c * (3 - 2 * c);
  };
  const ease = (x) => {
    const c = clamp01(x);
    return c < 0.5 ? 2 * c * c : 1 - Math.pow(-2 * c + 2, 2) / 2;
  };
  const lerp = (a, b, k) => a + (b - a) * k;

  function h(tag, cls, parent, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    if (parent) parent.append(e);
    return e;
  }

  function s(tag, attrs, parent) {
    const e = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v != null) e.setAttribute(k, String(v));
    if (parent) parent.append(e);
    return e;
  }

  /** Text with `code` and **bold** spans (textContent only). */
  function rich(target, text) {
    target.replaceChildren();
    for (const part of String(text ?? "").split(/(`[^`]+`|\*\*[^*]+\*\*)/)) {
      if (!part) continue;
      if (part.length > 2 && part.startsWith("`") && part.endsWith("`")) h("code", null, target, part.slice(1, -1));
      else if (part.length > 4 && part.startsWith("**") && part.endsWith("**")) h("strong", null, target, part.slice(2, -2));
      else target.append(part);
    }
  }

  function place(e, spec) {
    e.style.left = `${spec.x}px`;
    e.style.top = `${spec.y}px`;
    if (spec.w != null) e.style.width = `${spec.w}px`;
    if (spec.h != null) e.style.height = `${spec.h}px`;
  }

  // ---------- Edge geometry ----------

  function rectOf(scene, ref) {
    if (Array.isArray(ref)) return { x: ref[0], y: ref[1], w: 0, h: 0, point: true };
    const spec = scene.nodeMap[ref] || scene.groupMap[ref];
    if (!spec) throw new Error(`Scene ${scene.id}: unknown edge endpoint ${ref}`);
    return spec;
  }

  function sides(a, b) {
    const xGap = Math.max(b.x - (a.x + a.w), a.x - (b.x + b.w));
    const yGap = Math.max(b.y - (a.y + a.h), a.y - (b.y + b.h));
    const dx = b.x + b.w / 2 - (a.x + a.w / 2);
    const dy = b.y + b.h / 2 - (a.y + a.h / 2);
    if (xGap >= yGap) return dx > 0 ? ["right", "left"] : ["left", "right"];
    return dy > 0 ? ["bottom", "top"] : ["top", "bottom"];
  }

  function anchor(r, side, offset) {
    if (r.point) return [r.x, r.y];
    switch (side) {
      case "left": return [r.x, r.y + r.h / 2 + offset];
      case "right": return [r.x + r.w, r.y + r.h / 2 + offset];
      case "top": return [r.x + r.w / 2 + offset, r.y];
      default: return [r.x + r.w / 2 + offset, r.y + r.h];
    }
  }

  function pathOf(scene, e) {
    if (e.points) return "M " + e.points.map((p) => `${p[0]} ${p[1]}`).join(" L ");
    const a = rectOf(scene, e.from);
    const b = rectOf(scene, e.to);
    const [sa, sb] = sides(a, b);
    const s0 = e.fromSide || sa;
    const s1 = e.toSide || sb;
    const p0 = anchor(a, s0, e.fromOffset || 0);
    const p1 = anchor(b, s1, e.toOffset || 0);
    if (e.straight || (a.point && b.point)) return `M ${p0[0]} ${p0[1]} L ${p1[0]} ${p1[1]}`;
    const d = Math.max(30, Math.hypot(p1[0] - p0[0], p1[1] - p0[1]) * (e.curve ?? 0.35));
    const n0 = a.point ? [0, 0] : NORMAL[s0];
    const n1 = b.point ? [0, 0] : NORMAL[s1];
    const c0 = [p0[0] + n0[0] * d, p0[1] + n0[1] * d];
    const c1 = [p1[0] + n1[0] * d, p1[1] + n1[1] * d];
    return `M ${p0[0]} ${p0[1]} C ${c0[0]} ${c0[1]}, ${c1[0]} ${c1[1]}, ${p1[0]} ${p1[1]}`;
  }

  // ---------- Scene construction ----------

  function build(index) {
    const scene = video.scenes[index];
    scene.nodeMap = Object.fromEntries((scene.nodes || []).map((n) => [n.id, n]));
    scene.groupMap = Object.fromEntries((scene.groups || []).map((g) => [g.id, g]));
    const root = document.getElementById("scene");
    root.replaceChildren();
    parts = new Map();
    packets = [];
    const add = (id, type, el, extra) => {
      if (parts.has(id)) throw new Error(`Scene ${scene.id}: duplicate id ${id}`);
      const p = { id, type, el, appear: scene.start, shown: false, f: [], on: [], ...extra };
      parts.set(id, p);
      return p;
    };

    for (const g of scene.groups || []) {
      const e = h("div", "group", root);
      place(e, g);
      if (g.label) rich(h("div", "gl", e), g.label);
      add(g.id, "group", e);
    }

    const layer = s("svg", { class: "edges", width: 1920, height: 1080, viewBox: "0 0 1920 1080" }, root);
    const defs = s("defs", {}, layer);
    for (const [id, color] of [["ah", "#5b6b85"], ["ah-on", "#e2e8f0"]]) {
      const m = s("marker", { id, markerUnits: "userSpaceOnUse", markerWidth: 16, markerHeight: 16, refX: 13, refY: 6, orient: "auto" }, defs);
      s("path", { d: "M 0 0 L 14 6 L 0 12 z", fill: color }, m);
    }
    const labels = [];
    for (const e of scene.edges || []) {
      const path = s("path", { d: pathOf(scene, e), class: "edge" + (e.dash ? " dash" : ""), "marker-end": e.noArrow ? null : "url(#ah)" }, layer);
      const p = add(e.id, "edge", path, { spec: e, length: path.getTotalLength() });
      if (e.label) labels.push([p, e]);
    }

    for (const n of scene.nodes || []) {
      const cls = ["node", n.shape, n.small ? "small" : null, n.center ? "center" : null].filter(Boolean).join(" ");
      const e = h("div", cls, root);
      place(e, n);
      e.style.setProperty("--c", KINDS[n.kind || "neutral"] || n.kind);
      if (n.title) rich(h("div", "t", e), n.title);
      if (n.sub) rich(h("div", "s", e), n.sub);
      if (n.body) {
        const b = h("div", "b", e);
        for (const line of [].concat(n.body)) rich(h("div", null, b), line);
      }
      add(n.id, "node", e);
    }

    for (const c of scene.cards || []) {
      const e = h("div", "card", root);
      place(e, c);
      if (c.title) h("div", "ct", e, c.title);
      const lines = c.lines.map((line) => h("div", "cl", e, line));
      add(c.id, "card", e, { lines });
    }

    for (const l of scene.lists || []) {
      const e = h("div", "list", root);
      place(e, l);
      const items = l.items.map((text, i) => {
        const it = h("div", "item", e);
        if (l.size) it.style.fontSize = `${l.size}px`;
        h("div", "n", it, l.marks ? l.marks[i] : String(i + 1));
        rich(h("div", null, it), text);
        return it;
      });
      add(l.id, "list", e, { items });
    }

    for (const t of scene.texts || []) {
      const e = h("div", "text", root);
      place(e, t);
      Object.assign(e.style, {
        fontSize: `${t.size || 28}px`, fontWeight: String(t.weight || 400), color: t.color || "#e5edf7", textAlign: t.align || "left",
        lineHeight: String(t.lineHeight || 1.3), letterSpacing: t.spacing || "normal",
      });
      if (t.pill) Object.assign(e.style, { padding: "10px 22px", borderRadius: "999px", border: "1px solid rgba(148,163,184,.35)", background: "rgba(15,23,42,.85)" });
      rich(e, t.text);
      add(t.id, "text", e);
    }

    for (const [p, e] of labels) {
      const lab = h("div", "elabel", root);
      rich(lab, e.label);
      const at = p.el.getPointAtLength(p.length * (e.labelAt ?? 0.5));
      lab.style.left = `${at.x + (e.labelDx || 0)}px`;
      lab.style.top = `${at.y + (e.labelDy || 0)}px`;
      p.label = lab;
    }

    // Appearance: first beat that shows an element; edges wait for both endpoints.
    for (const b of scene.beats) {
      for (const id of b.show || []) {
        const p = parts.get(id);
        if (!p) throw new Error(`Scene ${scene.id}: beat shows unknown id ${id}`);
        if (!p.shown) {
          p.appear = b.start;
          p.shown = true;
        }
      }
    }
    for (const p of parts.values()) {
      if (p.type !== "edge") continue;
      for (const end of [p.spec.from, p.spec.to]) {
        if (typeof end === "string" && parts.has(end)) p.appear = Math.max(p.appear, parts.get(end).appear);
      }
    }

    // Focus and highlight targets per beat.
    scene.beats.forEach((b, bi) => {
      const focus = b.focus ? new Set(b.focus) : null;
      const moving = new Set((b.flow || []).flat().map((id) => id.replace(/^-/, "")));
      const hl = new Set([...(b.hl || []), ...moving]);
      for (const id of [...(b.focus || []), ...hl]) if (!parts.has(id)) throw new Error(`Scene ${scene.id}: beat refers to unknown id ${id}`);
      for (const p of parts.values()) {
        let f = 1;
        if (focus) {
          if (p.type === "edge") {
            const { from, to } = p.spec;
            const inside = focus.has(p.id) || hl.has(p.id) || (typeof from === "string" && typeof to === "string" && focus.has(from) && focus.has(to));
            f = inside ? 1 : DIM;
          } else if (p.type === "group") {
            f = focus.has(p.id) ? 1 : GROUP_DIM;
          } else {
            f = focus.has(p.id) ? 1 : DIM;
          }
        }
        p.f[bi] = f;
        p.on[bi] = p.type === "edge" && hl.has(p.id);
      }
    });
  }

  function packet(i) {
    while (packets.length <= i) {
      const e = h("div", "packet", document.getElementById("scene"));
      h("div", "p", e);
      const label = h("div", "pl", e);
      e.style.opacity = "0";
      packets.push({ el: e, label, text: null });
    }
    return packets[i];
  }

  function header(index) {
    const scene = video.scenes[index];
    const chapter = document.getElementById("chapter");
    chapter.textContent = scene.chapter || "";
    chapter.style.display = scene.chapter ? "" : "none";
    document.getElementById("section").textContent = scene.section || "";
    [...document.getElementById("dots").children].forEach((d, i) => {
      d.className = "dot" + (i < index ? " done" : i === index ? " now" : "");
    });
  }

  // ---------- Public API ----------

  function load(data) {
    video = data;
    built = -1;
    captionBeat = null;
    const brand = document.getElementById("brand");
    brand.replaceChildren();
    h("strong", null, brand, data.brand || "HealthSwap");
    brand.append(` · ${data.subtitle || ""}`);
    const dots = document.getElementById("dots");
    dots.replaceChildren();
    for (let i = 0; i < data.scenes.length; i++) h("div", "dot", dots);
  }

  /** Renders time t. `still` (for posters) shows every element undimmed, hides packets and uses the scene's first caption. */
  function renderAt(t, { still = false } = {}) {
    let si = video.scenes.findIndex((sc) => t < sc.end);
    if (si < 0) si = video.scenes.length - 1;
    if (si !== built) {
      build(si);
      header(si);
      built = si;
      captionBeat = null;
    }
    const scene = video.scenes[si];
    let bi = scene.beats.findIndex((b) => t < b.end);
    if (bi < 0) bi = scene.beats.length - 1;
    const beat = scene.beats[bi];
    const key = [si, bi];

    const sceneOpacity = smooth((t - scene.start) / SCENE_FADE) * smooth((scene.end - t) / SCENE_FADE);
    document.getElementById("scene").style.opacity = sceneOpacity.toFixed(3);
    key.push(Math.round(sceneOpacity * 100));

    const k = smooth((t - beat.start) / FOCUS_FADE);
    for (const p of parts.values()) {
      const visible = t < p.appear ? 0 : smooth((t - p.appear) / FADE);
      const f = still ? 1 : lerp(bi > 0 ? p.f[bi - 1] : 1, p.f[bi], k);
      const o = visible * f;
      p.el.style.opacity = o.toFixed(3);
      if (p.type !== "edge" && p.type !== "group") p.el.style.transform = `scale(${(0.965 + 0.035 * visible).toFixed(4)})`;
      if (p.label) p.label.style.opacity = o.toFixed(3);
      key.push(Math.round(o * 100), Math.round(visible * 100));
      if (p.type === "edge") {
        const on = !still && p.on[bi] && visible > 0.99;
        p.el.classList.toggle("on", on);
        if (!p.spec.noArrow) p.el.setAttribute("marker-end", on ? "url(#ah-on)" : "url(#ah)");
        key.push(on ? 1 : 0);
      } else if (p.type === "list") {
        const on = new Set((beat.on && beat.on[p.id]) || []);
        p.items.forEach((it, i) => it.classList.toggle("on", on.has(i)));
        key.push([...on].join("."));
      } else if (p.type === "card") {
        const on = new Set((beat.lines && beat.lines[p.id]) || []);
        p.lines.forEach((ln, i) => ln.classList.toggle("hl", on.has(i)));
        key.push([...on].join("."));
      }
    }

    const chains = still ? [] : beat.flow || [];
    const seg = beat.seg || 1.15;
    for (let c = 0; c < Math.max(chains.length, packets.length); c++) {
      const pk = packet(c);
      const chain = chains[c];
      let shown = false;
      if (chain && chain.length) {
        const n = chain.length;
        const cycle = n * seg + 0.9;
        const u = t - beat.start - 0.35;
        if (u >= 0) {
          const m = u % cycle;
          if (m < n * seg) {
            const step = Math.floor(m / seg);
            const frac = ease((m - step * seg) / seg);
            let id = chain[step];
            const reverse = id.startsWith("-");
            if (reverse) id = id.slice(1);
            const edge = parts.get(id);
            const at = (reverse ? 1 - frac : frac) * edge.length;
            const pt = edge.el.getPointAtLength(at);
            const behind = edge.el.getPointAtLength(Math.max(0, at - 3));
            const ahead = edge.el.getPointAtLength(Math.min(edge.length, at + 3));
            const left = (ahead.x - behind.x) * (reverse ? -1 : 1) < -0.5;
            const fade = Math.min(1, m / 0.15, (n * seg - m) / 0.15);
            pk.el.style.transform = `translate(${pt.x.toFixed(1)}px, ${pt.y.toFixed(1)}px)`;
            pk.el.style.opacity = (fade * sceneOpacity).toFixed(3);
            pk.el.classList.toggle("left", left);
            const text = (beat.packets && beat.packets[c]) || beat.packet || "";
            if (pk.text !== text) {
              pk.label.textContent = text;
              pk.label.style.display = text ? "" : "none";
              pk.text = text;
            }
            key.push(c, Math.round(pt.x), Math.round(pt.y), Math.round(fade * 100));
            shown = true;
          }
        }
      }
      if (!shown) pk.el.style.opacity = "0";
    }

    const caption = document.getElementById("caption-text");
    const captionFor = still ? scene.beats[0] : beat;
    if (captionBeat !== captionFor) {
      rich(caption, captionFor.text);
      captionBeat = captionFor;
    }
    const co = still ? 1 : smooth((t - beat.start) / CAPTION_FADE);
    caption.style.opacity = co.toFixed(3);
    key.push(Math.round(co * 100));
    return key.join(",");
  }

  window.HSVideo = { load, renderAt };
})();
