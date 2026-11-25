// QuickBasket demo shop: a cart page that uses the HealthSwap web SDK (served at /demo by the API in Development).
// The demo backend (POST /demo/api/session) stands in for the partner's server: it mints the short-lived SDK session token,
// so the browser never holds partner credentials. The "Partner view" reads this cart's RA-1 charges through
// GET /demo/api/charges, again server-side.

import { createHealthSwap } from "/demo/sdk/healthswap-web.js";

const MARKET = "IN";
const STORE = "BLR-KOR-012";
const CURRENCY = "INR";
const LOCALE = "en-IN";
const MAX_QUANTITY = 20;
const LOG_LIMIT = 60;

const cartRef = `c_demo_${randomHex(12)}`; // one cart (and SDK session) per page load
const cart = [
  { lineId: "1", partnerSku: "SKU-NDL-280", gtin: "8901234567890", title: "Swaad Masala Instant Noodles 4-pack 280 g", unitPrice: "56.00", quantity: 2 },
  { lineId: "2", partnerSku: "SKU-COLA-750", title: "Fizzo Cola 750 ml", unitPrice: "40.00", quantity: 1 },
];

const $ = (id) => document.getElementById(id);
const linesEl = $("cart-lines");
const totalEl = $("cart-total");
const findButton = $("find-swaps");
const statusEl = $("status");
const swapsEl = $("swaps");
const logEl = $("event-log");
const chargesEl = $("charges");
const chargesSummaryEl = $("charges-summary");
const money = new Intl.NumberFormat(LOCALE, { style: "currency", currency: CURRENCY });

function randomHex(length) {
  const bytes = crypto.getRandomValues(new Uint8Array(Math.ceil(length / 2)));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("").slice(0, length);
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text != null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}

/** "56.00" -> 5600 (integer paise: no floating-point sums). */
function toMinor(amount) {
  const [units, fraction = ""] = String(amount).split(".");
  return Number(units) * 100 + Number(`${fraction}00`.slice(0, 2));
}

const formatMinor = (minor) => money.format(minor / 100);

// ---------- HealthSwap web SDK ----------

async function getSessionToken() {
  const response = await fetch("/demo/api/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cartRef, market: MARKET, storeId: STORE }),
  });
  if (!response.ok) throw new Error(`demo session: HTTP ${response.status}`);
  return (await response.json()).token;
}

const hs = createHealthSwap({ baseUrl: "", getSessionToken, locale: LOCALE, onEvent: logEvent });
hs.trackImpressions(swapsEl);

function describe(event) {
  // Suggestion IDs are UUIDv7: the first digits are a timestamp shared by one evaluation, the tail tells them apart.
  const short = (id) => (typeof id === "string" ? `...${id.slice(-8)}` : "");
  switch (event.type) {
    case "queued": {
      const e = event.event;
      const sku = e.alternativePartnerSku ? ` ${e.alternativePartnerSku}` : "";
      const qty = e.quantity ? ` x${e.quantity}` : "";
      return `${e.type} queued${sku}${qty} (suggestion ${short(e.suggestionId)})`;
    }
    case "flushed": {
      const r = event.result || {};
      const rejected = Array.isArray(r.rejected) && r.rejected.length ? `, ${r.rejected.length} rejected (${r.rejected.map((x) => x.code).join(", ")})` : "";
      return `Sent ${event.events.length} event(s)${event.keepalive ? " with keepalive" : ""}: ${r.accepted ?? 0} accepted, ${r.duplicates ?? 0} duplicates${rejected}`;
    }
    case "evaluated":
      return `Cart evaluated in ${event.ms} ms: ${event.suggestions} suggestion(s)${event.degraded ? " (degraded)" : ""}`;
    case "fail-open":
      return `Failed open (${event.reason}${event.status ? `, HTTP ${event.status}` : ""}) after ${event.ms} ms: ${event.detail}`;
    case "token":
      return `SDK session token ${event.detail}`;
    case "error":
      return `Error: ${event.message}`;
    default:
      return event.type;
  }
}

function logEvent(event) {
  const item = node("li", null, `log-${event.type}`);
  item.append(node("time", new Date(event.at).toLocaleTimeString(LOCALE), "log-time"), " ", describe(event));
  logEl.prepend(item);
  while (logEl.children.length > LOG_LIMIT) logEl.lastElementChild.remove();
  if (event.type === "flushed") refreshCharges();
}

// ---------- Cart ----------

const lineTotals = new Map(); // lineId -> the line's total cell

/** Rebuilds the cart rows (after a swap or an undo); quantity changes only update the totals, so a focused input stays put. */
function renderCart() {
  lineTotals.clear();
  linesEl.replaceChildren(...cart.map(renderLine));
  updateTotals();
}

function renderLine(line) {
  const row = document.createElement("tr");
  const item = node("td");
  item.append(node("span", line.title, "item-title"));
  if (line.reco) {
    // The partner renders the HealthSwap label on a cart line that holds a recommended product (RA-1).
    item.append(" ", node("span", line.reco.label?.text || "Recommended by HealthSwap", "hs-label"));
  }
  item.append(node("span", line.partnerSku, "item-sku"));

  const quantityCell = node("td", null, "num");
  const input = document.createElement("input");
  Object.assign(input, { type: "number", min: "1", max: String(MAX_QUANTITY), step: "1", value: String(line.quantity), id: `qty-${line.lineId}` });
  input.className = "qty";
  input.setAttribute("aria-label", `Quantity of ${line.title}`);
  input.addEventListener("change", () => changeQuantity(line, input));
  quantityCell.append(input);

  const total = node("td", null, "num");
  lineTotals.set(line.lineId, total);
  row.append(item, node("td", formatMinor(toMinor(line.unitPrice)), "num"), quantityCell, total);
  return row;
}

function updateTotals() {
  let total = 0;
  for (const line of cart) {
    const lineTotal = toMinor(line.unitPrice) * line.quantity;
    total += lineTotal;
    const cell = lineTotals.get(line.lineId);
    if (cell) cell.textContent = formatMinor(lineTotal);
  }
  totalEl.textContent = formatMinor(total);
}

function changeQuantity(line, input) {
  const requested = Math.round(Number(input.value));
  const quantity = input.value.trim() !== "" && Number.isFinite(requested) ? Math.min(MAX_QUANTITY, Math.max(1, requested)) : line.quantity;
  input.value = String(quantity);
  const added = quantity - line.quantity;
  line.quantity = quantity;
  if (line.reco && added > 0) {
    // A further add of the HealthSwap-recommended product ("+" on the cart line): ADD_TO_CART, one more RA-1 add.
    hs.addToCart(line.reco.suggestionId, line.partnerSku, { quantity: added });
  }
  updateTotals();
}

// The SDK never changes the cart: these callbacks are the partner's cart operations.
function onAdd(detail) {
  const line = cart.find((l) => l.lineId === detail.lineId);
  if (!line) return;
  const { reco, ...original } = line;
  line.reco = { suggestionId: detail.suggestionId, label: detail.label, original: reco ? reco.original : original };
  line.partnerSku = detail.partnerSku;
  line.title = detail.alternative.product?.title || detail.partnerSku;
  line.unitPrice = detail.alternative.unitPrice?.amount || line.unitPrice;
  line.quantity = detail.quantity;
  delete line.gtin;
  renderCart();
}

function onUndo(detail) {
  const index = cart.findIndex((l) => l.lineId === detail.lineId);
  if (index < 0 || !cart[index].reco) return;
  cart[index] = { ...cart[index].reco.original };
  renderCart();
}

function onDismiss(detail) {
  setStatus(`Kept ${detail.original?.title || "the original item"}.`);
}

function setStatus(text) {
  statusEl.textContent = text;
}

async function findSwaps() {
  findButton.disabled = true;
  setStatus("Finding healthier swaps...");
  const evaluation = await hs.evaluateCart({
    cartRef, market: MARKET, storeId: STORE, currency: CURRENCY, locale: LOCALE,
    context: { placement: "CART", channel: "WEB" },
    lines: cart.map((l) => ({
      lineId: l.lineId, partnerSku: l.partnerSku, ...(l.gtin ? { gtin: l.gtin } : {}), quantity: l.quantity,
      unitPrice: { amount: l.unitPrice, currency: CURRENCY },
    })),
  });
  hs.renderSuggestions(swapsEl, evaluation, { onAdd, onUndo, onDismiss });
  const count = evaluation.suggestions.length;
  if (evaluation.failOpen) setStatus(`Healthier swaps are unavailable right now (${evaluation.failOpen.reason}); your cart is not affected.`);
  else setStatus(count ? `${count} healthier swap${count === 1 ? "" : "s"} found.` : "No healthier swaps for this cart.");
  findButton.disabled = false;
}

// ---------- Partner view: RA-1 charges of this cart ----------

let refreshing = null;

function refreshCharges() {
  refreshing ??= loadCharges().finally(() => {
    refreshing = null;
  });
  return refreshing;
}

async function loadCharges() {
  let page;
  try {
    const response = await fetch(`/demo/api/charges?cartRef=${encodeURIComponent(cartRef)}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    page = await response.json();
  } catch (error) {
    chargesSummaryEl.textContent = `Charges unavailable: ${error.message}`;
    return;
  }
  const items = Array.isArray(page.items) ? page.items : [];
  chargesEl.replaceChildren();
  if (items.length === 0) {
    const row = document.createElement("tr");
    const cell = node("td", "No charges yet: add a recommended swap.", "empty");
    cell.colSpan = 5;
    row.append(cell);
    chargesEl.append(row);
  }
  const totals = new Map();
  for (const c of items) {
    const row = document.createElement("tr");
    const state = node("span", c.state, `state state-${String(c.state).toLowerCase()}`);
    const stateCell = node("td");
    stateCell.append(state);
    if (c.rejectionReason) stateCell.append(node("span", c.rejectionReason, "reason"));
    row.append(
      node("td", new Date(c.occurredAt).toLocaleTimeString(LOCALE)),
      node("td", c.eventType),
      node("td", `${c.alternativePartnerSku}${c.quantity ? ` x${c.quantity}` : ""}`),
      node("td", `${c.amount?.currency ?? ""} ${c.amount?.amount ?? ""}`, "num"),
      stateCell,
    );
    chargesEl.append(row);
    totals.set(c.state, (totals.get(c.state) || 0) + toMinor(c.amount?.amount || "0"));
  }
  chargesSummaryEl.textContent = items.length
    ? `${items.length} charge(s): ${[...totals].map(([state, minor]) => `${state} ${formatMinor(minor)}`).join(", ")}. PENDING charges become FINAL after the settlement window (10 minutes) when the evidence holds.`
    : "";
}

// ---------- Start ----------

$("cart-ref").textContent = cartRef;
findButton.addEventListener("click", findSwaps);
renderCart();
refreshCharges();
setInterval(() => {
  if (document.visibilityState === "visible") refreshCharges();
}, 3000);
