/**
 * HealthSwap web SDK: renders HealthSwap-recommended swaps for a shopper's cart and reports suggestion events.
 * One ES module, no dependencies, no build step; current Chrome, Edge, Firefox and Safari.
 *
 *   import { createHealthSwap } from "https://partner.example/healthswap/healthswap-web.js";
 *
 *   const hs = createHealthSwap({
 *     baseUrl: "https://api.in.healthswap.example",       // API root ("/v1" is added); "" = same origin
 *     getSessionToken: () => fetch("/api/healthswap-session", { method: "POST" })   // your backend calls POST /v1/sdk/sessions
 *       .then((r) => r.json()).then((s) => s.token),
 *     locale: "en-IN",
 *     onEvent: (e) => console.debug("healthswap", e),     // queued / flushed / evaluated / fail-open / error / token
 *   });
 *   const evaluation = await hs.evaluateCart(cart);       // never rejects: fails open to { suggestions: [], degraded: true, failOpen }
 *   hs.renderSuggestions(container, evaluation, {
 *     onAdd: (d) => myCart.replaceLine(d.lineId, d.partnerSku, d.quantity),   // the SDK never touches your cart
 *     onUndo: (d) => myCart.restoreLine(d.lineId),
 *     onDismiss: (d) => {},
 *   });
 *   hs.trackImpressions(container);                       // one IMPRESSION per suggestion after >= 50% visible for >= 1 s
 *   hs.addToCart(suggestionId, partnerSku, { quantity: 1 }); // any later add of the recommended product ("+" on the line)
 *
 * Events are batched (sent every 2 s or at 20 queued events) and sent with fetch keepalive on pagehide / when the page is
 * hidden. "Add" reports ACCEPT (the swap was accepted: one RA-1 add) and offers a 5 s Undo that reports UNDO; report every
 * other add of a recommended product with addToCart (ADD_TO_CART). Security: the short-lived SDK session token is the
 * only credential the browser holds; API data is rendered with textContent only and links are limited to http(s) URLs.
 */

const VERSION = "0.1.0";
const MAX_EVENTS_PER_REQUEST = 500; // EventBatchRequest.events maxItems
const MAX_QUEUED_EVENTS = 1000;
const KEEPALIVE_BODY_BYTES = 60000; // browsers cap in-flight keepalive bodies at 64 KiB per page
const RETRY_MAX_MS = 30000;
const SEND_TIMEOUT_MS = 10000;
const GRADES = new Set(["A", "B", "C", "D", "E"]);

const TEXT = {
  heading: "Healthier swaps",
  insteadOf: "Healthier option for ",
  grade: (g) => `Grade ${g}`,
  notGraded: "Not graded",
  was: (g) => `was ${g}`,
  more: (m) => `${m} more per unit`,
  less: (m) => `${m} less per unit`,
  same: "Same price per unit",
  priceUnknown: "Price shown at checkout",
  why: "Why?",
  whyLabel: (t) => `Why is ${t} recommended?`,
  add: "Add",
  addLabel: (alt, orig) => `Add ${alt} instead of ${orig}`,
  keep: "Keep original",
  keepLabel: (orig) => `Keep original: ${orig}`,
  sponsored: "Sponsored",
  aiGenerated: "This wording was written with AI assistance and checked against the label data.",
  methodology: "How HealthSwap compares products",
  swapped: (t) => `Swapped to ${t}.`,
  undo: "Undo",
  undoLabel: (t) => `Undo swap to ${t}`,
  seconds: (s) => `${s} s`,
  undone: (t) => `Kept ${t}.`,
};

const NUTRIENTS = {
  energyKcal: "Energy", proteinG: "Protein", carbohydrateG: "Carbohydrate", totalSugarsG: "Sugars", addedSugarsG: "Added sugars",
  totalFatG: "Fat", saturatedFatG: "Saturated fat", transFatG: "Trans fat", sodiumMg: "Sodium", saltG: "Salt", dietaryFibreG: "Fibre",
};
const BASIS = { PER_100G: "per 100 g", PER_100ML: "per 100 ml", PER_SERVING: "per serving" };

let instances = 0;

function newUuid() {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID(); // secure contexts (https, localhost)
  const b = c.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

function str(value) {
  return typeof value === "string" ? value : value == null ? "" : String(value);
}

/** An http(s) URL from API data, or null (javascript:, data: and other schemes are never put into the DOM). */
function safeUrl(value) {
  if (typeof value !== "string" || value === "") return null;
  try {
    const url = new URL(value, globalThis.document ? document.baseURI : undefined);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}

/** document.createElement with textContent for text: API data never reaches innerHTML. */
function el(tag, options = {}, ...children) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.id) node.id = options.id;
  if (options.text != null) node.textContent = str(options.text);
  for (const [name, value] of Object.entries(options.attrs || {})) {
    if (value == null || value === false) continue;
    if (/^on/i.test(name) || /^(href|src|srcdoc|style)$/i.test(name)) throw new Error(`attribute ${name} must be set explicitly`);
    node.setAttribute(name, value === true ? "" : str(value));
  }
  for (const child of children) {
    if (child == null || child === false) continue;
    node.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

function formatMoney(money, locale) {
  if (!money || typeof money.amount !== "string") return "";
  const amount = money.amount.replace(/^-/, "");
  const value = Number(amount);
  try {
    if (Number.isFinite(value)) return new Intl.NumberFormat(locale || undefined, { style: "currency", currency: money.currency }).format(value);
  } catch {
    // unknown currency code: fall through
  }
  return `${str(money.currency)} ${amount}`.trim();
}

function priceDelta(delta, locale) {
  if (!delta || typeof delta.amount !== "string") return "";
  const value = Number(delta.amount);
  if (!Number.isFinite(value) || value === 0) return TEXT.same;
  return value > 0 ? TEXT.more(formatMoney(delta, locale)) : TEXT.less(formatMoney(delta, locale));
}

function gradeBadge(health) {
  const grade = typeof health?.hsiGrade === "string" ? health.hsiGrade.toUpperCase() : null;
  if (!grade || !GRADES.has(grade)) return el("span", { className: "hs-grade hs-grade--none", text: TEXT.notGraded });
  return el("span", { className: `hs-grade hs-grade--${grade.toLowerCase()}`, text: TEXT.grade(grade) });
}

function detailBullet(d) {
  const name = NUTRIENTS[d?.nutrient] || str(d?.nutrient);
  const unit = d?.unit ? ` ${str(d.unit)}` : "";
  const basis = BASIS[d?.basis] ? ` ${BASIS[d.basis]}` : "";
  return `${name}: ${str(d?.from)}${unit} \u2192 ${str(d?.to)}${unit}${basis}`;
}

/** The "Why?" bullets: the explanation's bullets, else its nutrient comparisons. */
function bulletsOf(explanation) {
  const bullets = Array.isArray(explanation?.bullets) ? explanation.bullets.filter((b) => typeof b === "string" && b) : [];
  if (bullets.length) return bullets;
  return (Array.isArray(explanation?.details) ? explanation.details : []).filter((d) => d && d.direction === "BETTER").slice(0, 3).map(detailBullet);
}

function failOpen(request, reason, detail, status = null) {
  return {
    evaluationId: null, cartRef: request?.cartRef ?? null, market: request?.market ?? null, storeId: request?.storeId ?? null,
    degraded: true, degradationReasons: [], suggestions: [], lineLabels: [],
    failOpen: { reason, detail: str(detail), status },
  };
}

/** The JSON object of a response (a problem, an EventBatchResult), or {} when the body is not a JSON object. */
async function readJson(response) {
  try {
    const body = await response.json();
    return body && typeof body === "object" ? body : {};
  } catch {
    return {};
  }
}

class SessionError extends Error {}

/**
 * Creates one SDK instance (per page / cart session).
 * Options: baseUrl, getSessionToken (required; returns a token string or an SdkSession object), locale, onEvent, and the
 * tunables timeoutMs (evaluation deadline, default 1500), flushIntervalMs (2000), maxBatchEvents (20), impressionMs (1000),
 * undoMs (5000).
 */
export function createHealthSwap(options = {}) {
  const {
    baseUrl = "", getSessionToken, locale, onEvent, timeoutMs = 1500, flushIntervalMs = 2000, maxBatchEvents = 20, impressionMs = 1000,
    undoMs = 5000,
  } = options;
  if (typeof getSessionToken !== "function") throw new TypeError("createHealthSwap: getSessionToken must be a function");
  const apiRoot = `${str(baseUrl).replace(/\/+$/, "").replace(/\/v1$/, "")}/v1`;
  const instance = ++instances;
  let ids = 0;
  const nextId = (kind) => `hs${instance}-${kind}-${++ids}`;

  function emit(event) {
    if (typeof onEvent !== "function") return;
    try {
      onEvent({ ...event, at: new Date().toISOString() });
    } catch (error) {
      console.error("HealthSwap onEvent handler failed", error); // never let a host handler break the SDK
    }
  }

  // ---------- Session token ----------

  let token = null;
  let tokenRequest = null;
  let tokensFetched = 0;

  function fetchToken() {
    if (!tokenRequest) {
      tokenRequest = Promise.resolve()
        .then(() => getSessionToken())
        .then((value) => {
          const next = typeof value === "string" ? value : value && typeof value.token === "string" ? value.token : "";
          if (!next) throw new SessionError("getSessionToken() returned no token");
          token = next;
          emit({ type: "token", detail: tokensFetched++ > 0 ? "refreshed" : "acquired" });
          return next;
        }, (error) => {
          throw error instanceof SessionError ? error : new SessionError(`getSessionToken() failed: ${error?.message || error}`);
        })
        .finally(() => {
          tokenRequest = null;
        });
    }
    return tokenRequest;
  }

  const currentToken = () => (token !== null ? Promise.resolve(token) : fetchToken());

  /** fetch with the bearer session token; a 401 refreshes the token once and repeats the request. */
  async function apiFetch(path, init, signal) {
    const send = (bearer) => fetch(apiRoot + path, { ...init, signal, headers: { ...init.headers, Authorization: `Bearer ${bearer}` } });
    const used = await currentToken();
    const response = await send(used);
    if (response.status !== 401) return response;
    if (token === used) token = null;
    return send(await currentToken());
  }

  // ---------- Cart evaluation (fail-open) ----------

  async function evaluateCart(cart) {
    const request = { ...(cart || {}) };
    if (!request.locale && locale) request.locale = locale;
    const controller = new AbortController();
    const started = typeof performance !== "undefined" ? performance.now() : Date.now();
    let timer;
    const deadline = new Promise((resolve) => {
      timer = setTimeout(() => {
        controller.abort();
        resolve(failOpen(request, "TIMEOUT", `No response within ${timeoutMs} ms`));
      }, timeoutMs);
    });
    const attempt = (async () => {
      try {
        const response = await apiFetch("/cart-evaluations", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request),
        }, controller.signal);
        if (response.ok) {
          const evaluation = await response.json();
          if (!evaluation || typeof evaluation !== "object" || !Array.isArray(evaluation.suggestions)) {
            return failOpen(request, "SERVER_ERROR", "The evaluation response is malformed", response.status);
          }
          return evaluation;
        }
        const problem = await readJson(response);
        const status = response.status;
        const reason = status === 429 ? "RATE_LIMITED" : status >= 500 ? "SERVER_ERROR" : status === 401 ? "UNAUTHORIZED" : "REQUEST_REJECTED";
        return failOpen(request, reason, problem.detail || problem.title || `HTTP ${status}`, status);
      } catch (error) {
        if (controller.signal.aborted) return failOpen(request, "TIMEOUT", `No response within ${timeoutMs} ms`);
        if (error instanceof SessionError) return failOpen(request, "UNAUTHORIZED", error.message);
        return failOpen(request, "CONNECTION_ERROR", error?.message || "Network error");
      }
    })();
    const result = await Promise.race([attempt, deadline]);
    clearTimeout(timer);
    const ms = Math.round((typeof performance !== "undefined" ? performance.now() : Date.now()) - started);
    if (result.failOpen) emit({ type: "fail-open", ...result.failOpen, ms });
    else emit({ type: "evaluated", evaluationId: result.evaluationId, suggestions: result.suggestions.length, degraded: result.degraded === true, ms });
    return result;
  }

  // ---------- Events: batching, retries, keepalive on page hide ----------

  const queue = [];
  let flushTimer = null;
  let retryDelay = flushIntervalMs;
  let sending = Promise.resolve();

  function schedule(delay) {
    if (flushTimer === null) flushTimer = setTimeout(() => {
      flushTimer = null;
      flush();
    }, delay);
  }

  function requeue(batch) {
    queue.unshift(...batch);
    if (queue.length > MAX_QUEUED_EVENTS) {
      const dropped = queue.splice(MAX_QUEUED_EVENTS);
      emit({ type: "error", message: `Dropped ${dropped.length} events: the queue is full` });
    }
  }

  function enqueue(event) {
    queue.push(event);
    if (queue.length > MAX_QUEUED_EVENTS) {
      queue.shift();
      emit({ type: "error", message: "Dropped the oldest event: the queue is full" });
    }
    emit({ type: "queued", event });
    if (queue.length >= maxBatchEvents) flush();
    else schedule(flushIntervalMs);
    return event;
  }

  async function sendQueued() {
    while (queue.length > 0) {
      const batch = queue.splice(0, MAX_EVENTS_PER_REQUEST);
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), SEND_TIMEOUT_MS);
      let response;
      let result;
      try {
        response = await apiFetch("/events", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ events: batch }),
        }, controller.signal);
        result = await readJson(response);
      } catch (error) {
        requeue(batch); // deduplicated by eventId on the server, so sending them again is safe
        emit({ type: "error", message: `Events not sent (${controller.signal.aborted ? "timeout" : error?.message || "network error"}); retrying` });
        break;
      } finally {
        clearTimeout(timer);
      }
      if (response.ok) {
        retryDelay = flushIntervalMs;
        emit({ type: "flushed", events: batch, result });
        continue;
      }
      const code = result.code ? ` ${result.code}` : "";
      if (response.status === 401 || response.status === 408 || response.status === 429 || response.status >= 500) {
        requeue(batch);
        const retryAfter = Number(response.headers.get("Retry-After"));
        if (Number.isFinite(retryAfter) && retryAfter > 0) retryDelay = Math.max(retryDelay, retryAfter * 1000);
        emit({ type: "error", message: `Events not sent (HTTP ${response.status}${code}); retrying` });
        break;
      }
      emit({ type: "error", message: `Dropped ${batch.length} events: HTTP ${response.status}${code}${result.detail ? ` ${result.detail}` : ""}` });
    }
    if (queue.length > 0) {
      schedule(retryDelay);
      retryDelay = Math.min(retryDelay * 2, RETRY_MAX_MS);
    }
  }

  /** Sends every queued event now; resolves when the attempt has finished (failed batches stay queued for a retry). */
  function flush() {
    if (flushTimer !== null) {
      clearTimeout(flushTimer);
      flushTimer = null;
    }
    sending = sending.then(sendQueued, sendQueued);
    return sending;
  }

  /** pagehide / hidden page: send the queue right away with keepalive (the page may be gone before a timer fires). */
  function flushOnHide() {
    if (queue.length === 0 || token === null) return;
    if (flushTimer !== null) {
      clearTimeout(flushTimer);
      flushTimer = null;
    }
    const pending = queue.splice(0, queue.length);
    while (pending.length > 0) {
      const batch = [];
      let size = 12;
      while (pending.length > 0 && batch.length < MAX_EVENTS_PER_REQUEST) {
        const bytes = JSON.stringify(pending[0]).length + 1;
        if (batch.length > 0 && size + bytes > KEEPALIVE_BODY_BYTES) break;
        batch.push(pending.shift());
        size += bytes;
      }
      fetch(`${apiRoot}/events`, {
        method: "POST", keepalive: true, headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ events: batch }),
      }).then(async (response) => {
        if (response.ok) emit({ type: "flushed", events: batch, result: await readJson(response), keepalive: true });
        else if (response.status === 401 || response.status === 429 || response.status >= 500) {
          requeue(batch);
          schedule(flushIntervalMs);
        }
      }, () => {
        requeue(batch); // still here (the page was only hidden): the next flush retries
        schedule(flushIntervalMs);
      });
    }
  }

  if (typeof window !== "undefined" && typeof document !== "undefined") {
    window.addEventListener("pagehide", flushOnHide);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") flushOnHide();
    });
  }

  function makeEvent(type, suggestionId, sku, extra = {}) {
    if (typeof suggestionId !== "string" || !suggestionId) throw new TypeError(`${type}: suggestionId is required`);
    const event = { eventId: newUuid(), type, suggestionId, occurredAt: new Date().toISOString() };
    if (sku != null) {
      if (typeof sku !== "string" || !sku) throw new TypeError(`${type}: alternativePartnerSku must be a non-empty string`);
      event.alternativePartnerSku = sku;
    }
    if (extra.quantity != null) {
      if (typeof extra.quantity !== "number" || !(extra.quantity > 0)) throw new TypeError(`${type}: quantity must be a positive number`);
      event.quantity = extra.quantity;
    }
    if (extra.metadata) event.metadata = { ...extra.metadata };
    return enqueue(event);
  }

  /** ACCEPT: the shopper accepted the swap and the alternative was added to the cart (one RA-1 add). */
  const accept = (suggestionId, alternativePartnerSku, { quantity, metadata } = {}) =>
    makeEvent("ACCEPT", suggestionId, alternativePartnerSku, { quantity, metadata });
  /** ADD_TO_CART: any other add of a recommended alternative (re-add, "+" on the cart line, product page): one RA-1 add. */
  const addToCart = (suggestionId, alternativePartnerSku, { quantity, metadata } = {}) =>
    makeEvent("ADD_TO_CART", suggestionId, alternativePartnerSku, { quantity, metadata });
  /** UNDO: the swap was reverted or the added product removed (voids the latest add within the undo grace period). */
  const undo = (suggestionId, alternativePartnerSku, { metadata } = {}) => makeEvent("UNDO", suggestionId, alternativePartnerSku ?? null, { metadata });
  /** DISMISS: the shopper kept the original. */
  const dismiss = (suggestionId, { metadata } = {}) => makeEvent("DISMISS", suggestionId, null, { metadata });

  // ---------- Impressions: >= 50% visible for >= 1 s, once per suggestion ----------

  const impressed = new Set();

  function trackImpressions(root) {
    if (!root || typeof IntersectionObserver !== "function" || typeof MutationObserver !== "function") return () => {};
    const visible = new Set();
    const timers = new Map();
    const observed = new Set();

    const cancel = (element) => {
      clearTimeout(timers.get(element));
      timers.delete(element);
    };
    const start = (element) => {
      const sid = element.dataset.hsSuggestionId;
      if (!sid || impressed.has(sid) || timers.has(element) || document.visibilityState !== "visible") return;
      timers.set(element, setTimeout(() => {
        timers.delete(element);
        if (!visible.has(element) || !element.isConnected || document.visibilityState !== "visible" || impressed.has(sid)) return;
        impressed.add(sid);
        io.unobserve(element);
        observed.delete(element);
        visible.delete(element);
        makeEvent("IMPRESSION", sid, null);
      }, impressionMs));
    };
    const io = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting && entry.intersectionRatio >= 0.499) {
          visible.add(entry.target);
          start(entry.target);
        } else {
          visible.delete(entry.target);
          cancel(entry.target); // visibility must be continuous
        }
      }
    }, { threshold: [0, 0.5, 1] });

    const observe = (element) => {
      if (observed.has(element) || impressed.has(element.dataset.hsSuggestionId)) return;
      observed.add(element);
      io.observe(element);
    };
    const forget = (element) => {
      if (!observed.has(element)) return;
      observed.delete(element);
      visible.delete(element);
      cancel(element);
      io.unobserve(element);
    };
    const scan = (node, action) => {
      if (!(node instanceof Element)) return;
      if (node.matches("[data-hs-suggestion-id]")) action(node);
      node.querySelectorAll("[data-hs-suggestion-id]").forEach(action);
    };
    const mo = new MutationObserver((records) => {
      for (const record of records) {
        record.addedNodes.forEach((n) => scan(n, observe));
        record.removedNodes.forEach((n) => scan(n, forget));
      }
    });
    const onVisibility = () => {
      if (document.visibilityState === "visible") visible.forEach(start);
      else [...timers.keys()].forEach(cancel);
    };

    scan(root, observe);
    mo.observe(root, { childList: true, subtree: true });
    document.addEventListener("visibilitychange", onVisibility);
    return function stop() {
      mo.disconnect();
      io.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
      [...timers.keys()].forEach(cancel);
    };
  }

  // ---------- Rendering ----------

  function call(handler, detail) {
    if (typeof handler !== "function") return;
    try {
      handler(detail);
    } catch (error) {
      emit({ type: "error", message: `Host callback failed: ${error?.message || error}` });
    }
  }

  function renderWhy(alternative, title) {
    const explanation = alternative.explanation || {};
    const panelId = nextId("why");
    const toggle = el("button", {
      className: "hs-why-toggle", text: TEXT.why,
      attrs: { type: "button", "aria-expanded": "false", "aria-controls": panelId, "aria-label": TEXT.whyLabel(title) },
    });
    const panel = el("div", { className: "hs-why-panel", id: panelId, attrs: { hidden: true } });
    const bullets = bulletsOf(explanation);
    if (bullets.length) panel.append(el("ul", { className: "hs-why-list" }, ...bullets.map((b) => el("li", { text: b }))));
    const disclosure = alternative.recommendation?.disclosure;
    if (disclosure) panel.append(el("p", { className: "hs-note", text: disclosure }));
    if (explanation.disclaimer) panel.append(el("p", { className: "hs-note", text: explanation.disclaimer }));
    if (explanation.aiGenerated === true) panel.append(el("p", { className: "hs-note", text: TEXT.aiGenerated }));
    const methodology = safeUrl(explanation.methodologyUrl);
    if (methodology) {
      const link = el("a", { className: "hs-link", text: TEXT.methodology, attrs: { target: "_blank", rel: "noopener noreferrer" } });
      link.href = methodology;
      panel.append(el("p", { className: "hs-note" }, link));
    }
    toggle.addEventListener("click", () => {
      const open = toggle.getAttribute("aria-expanded") !== "true";
      toggle.setAttribute("aria-expanded", String(open));
      panel.hidden = !open;
    });
    return el("div", { className: "hs-why" }, toggle, panel);
  }

  function renderAlternative(suggestion, alternative, originalTitle, onAddClick) {
    const product = alternative.product || {};
    const title = str(product.title) || str(product.partnerSku);
    const label = alternative.recommendation || {};
    const tags = el("p", { className: "hs-tags" }, el("span", { className: "hs-label", text: str(label.text) || "Recommended by HealthSwap" }),
      alternative.sponsored === true ? el("span", { className: "hs-tag", text: TEXT.sponsored }) : null);
    const originalGrade = suggestion.original?.health?.hsiGrade;
    const health = el("p", { className: "hs-health" }, gradeBadge(product.health),
      typeof originalGrade === "string" && GRADES.has(originalGrade) ? el("span", { className: "hs-muted", text: TEXT.was(originalGrade) }) : null);
    const price = alternative.unitPrice
      ? el("p", { className: "hs-price" }, el("span", { className: "hs-price-value", text: formatMoney(alternative.unitPrice, locale) }),
        alternative.unitPriceDelta ? el("span", { className: "hs-price-delta", text: priceDelta(alternative.unitPriceDelta, locale) }) : null)
      : el("p", { className: "hs-price", text: TEXT.priceUnknown });
    const reason = alternative.explanation?.short ? el("p", { className: "hs-reason", text: alternative.explanation.short }) : null;
    const add = el("button", { className: "hs-button hs-button--primary", text: TEXT.add, attrs: { type: "button", "aria-label": TEXT.addLabel(title, originalTitle) } });
    add.addEventListener("click", () => onAddClick(alternative, add));
    return el("li", { className: "hs-alt", attrs: { "data-hs-partner-sku": str(product.partnerSku) } },
      tags, el("h3", { className: "hs-alt-title", text: title }), health, price, reason, renderWhy(alternative, title), el("div", { className: "hs-alt-actions" }, add));
  }

  function renderCard(suggestion, handlers, focusAfterRemoval) {
    const original = suggestion.original || {};
    const originalTitle = str(original.title) || str(original.partnerSku);
    const titleId = nextId("card");
    const card = el("article", { className: "hs-card", attrs: { "data-hs-suggestion-id": str(suggestion.suggestionId), "aria-labelledby": titleId } });
    const heading = el("p", { className: "hs-card-title", id: titleId }, TEXT.insteadOf, el("strong", { text: originalTitle }), " ", gradeBadge(original.health));
    const list = el("ul", { className: "hs-alts" });
    const keep = el("button", { className: "hs-button hs-button--secondary", text: TEXT.keep, attrs: { type: "button", "aria-label": TEXT.keepLabel(originalTitle) } });
    const actions = el("div", { className: "hs-card-actions" }, keep);
    const status = el("div", { className: "hs-status", attrs: { hidden: true } });
    const live = el("p", { className: "hs-visually-hidden", attrs: { role: "status" } }); // present before it changes, so it is announced
    let undoTimer = null;
    let tick = null;

    const onAddClick = (alternative, button) => {
      const product = alternative.product || {};
      const sku = str(product.partnerSku);
      const quantity = typeof alternative.suggestedQuantity === "number" && alternative.suggestedQuantity > 0 ? alternative.suggestedQuantity : 1;
      const detail = {
        suggestionId: suggestion.suggestionId, lineId: suggestion.lineId, original, alternative, partnerSku: sku, quantity,
        label: alternative.recommendation || null,
      };
      accept(suggestion.suggestionId, sku, { quantity });
      call(handlers.onAdd, detail);

      const altTitle = str(product.title) || sku;
      list.hidden = true;
      actions.hidden = true;
      live.textContent = TEXT.swapped(altTitle);
      const message = el("p", { className: "hs-status-text", text: TEXT.swapped(altTitle), attrs: { tabindex: "-1" } });
      const undoButton = el("button", { className: "hs-button hs-button--secondary", text: TEXT.undo, attrs: { type: "button", "aria-label": TEXT.undoLabel(altTitle) } });
      let left = Math.ceil(undoMs / 1000);
      const countdown = el("span", { className: "hs-countdown", text: TEXT.seconds(left), attrs: { "aria-hidden": "true" } });
      status.replaceChildren(el("span", { className: "hs-label", text: str(alternative.recommendation?.text) || "Recommended by HealthSwap" }), message,
        el("div", { className: "hs-undo" }, undoButton, countdown));
      status.hidden = false;
      undoButton.focus();
      tick = setInterval(() => {
        left = Math.max(0, left - 1);
        countdown.textContent = TEXT.seconds(left);
      }, 1000);
      undoTimer = setTimeout(() => {
        clearInterval(tick);
        const hadFocus = status.contains(document.activeElement);
        status.querySelector(".hs-undo")?.remove();
        if (hadFocus) message.focus();
      }, undoMs);
      undoButton.addEventListener("click", () => {
        clearTimeout(undoTimer);
        clearInterval(tick);
        undo(suggestion.suggestionId, sku);
        call(handlers.onUndo, detail);
        live.textContent = TEXT.undone(originalTitle);
        status.replaceChildren();
        status.hidden = true;
        list.hidden = false;
        actions.hidden = false;
        button.focus();
      });
    };

    for (const alternative of Array.isArray(suggestion.alternatives) ? suggestion.alternatives : []) {
      list.append(renderAlternative(suggestion, alternative, originalTitle, onAddClick));
    }
    keep.addEventListener("click", () => {
      dismiss(suggestion.suggestionId);
      call(handlers.onDismiss, { suggestionId: suggestion.suggestionId, lineId: suggestion.lineId, original });
      const next = card.nextElementSibling;
      card.remove();
      focusAfterRemoval(next);
    });
    card.append(heading, list, actions, status, live);
    return card;
  }

  /**
   * Renders the evaluation's suggestions into `container` (replacing its content): per suggestion the original item and
   * every alternative with its "Recommended by HealthSwap" label, name, price and price difference, health grade, short
   * reason and a "Why?" disclosure; the fee disclosure is shown above the suggestions. Returns the rendered section (or
   * null when there is nothing to show). Handlers receive { suggestionId, lineId, original, alternative, partnerSku,
   * quantity, label } (onDismiss: { suggestionId, lineId, original }).
   */
  function renderSuggestions(container, evaluation, handlers = {}) {
    if (!container || typeof container.replaceChildren !== "function") throw new TypeError("renderSuggestions: container must be an element");
    container.replaceChildren();
    const suggestions = Array.isArray(evaluation?.suggestions) ? evaluation.suggestions.filter((s) => s && Array.isArray(s.alternatives) && s.alternatives.length) : [];
    if (suggestions.length === 0) return null;
    const headingId = nextId("heading");
    const section = el("section", { className: "hs-suggestions", attrs: { "aria-labelledby": headingId, "data-hs-sdk": VERSION } });
    const heading = el("h2", { className: "hs-heading", id: headingId, text: TEXT.heading, attrs: { tabindex: "-1" } });
    const disclosures = evaluation.disclosures || {};
    const cards = el("div", { className: "hs-cards" });
    const focusAfterRemoval = (next) => {
      if (cards.children.length === 0) {
        section.remove();
        return;
      }
      (next?.querySelector("button:not([hidden])") || heading).focus();
    };
    section.append(heading);
    if (disclosures.feeDisclosure) section.append(el("p", { className: "hs-fee-disclosure", text: disclosures.feeDisclosure }));
    for (const suggestion of suggestions) cards.append(renderCard(suggestion, handlers, focusAfterRemoval));
    section.append(cards);
    const footer = el("div", { className: "hs-footer" });
    if (disclosures.labelDisclaimer) footer.append(el("p", { className: "hs-note", text: disclosures.labelDisclaimer }));
    for (const a of Array.isArray(disclosures.attributions) ? disclosures.attributions : []) {
      if (a?.attributionText) footer.append(el("p", { className: "hs-note", text: a.attributionText }));
    }
    const methodology = safeUrl(disclosures.methodologyUrl);
    if (methodology) {
      const link = el("a", { className: "hs-link", text: TEXT.methodology, attrs: { target: "_blank", rel: "noopener noreferrer" } });
      link.href = methodology;
      footer.append(el("p", { className: "hs-note" }, link));
    }
    if (footer.children.length) section.append(footer);
    container.append(section);
    return section;
  }

  return { version: VERSION, evaluateCart, renderSuggestions, trackImpressions, accept, addToCart, undo, dismiss, flush };
}

export default createHealthSwap;
