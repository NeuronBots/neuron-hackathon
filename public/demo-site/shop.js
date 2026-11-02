/* shop.js — one job: pure order math and validation (no DOM). Integer rupees only. */
var SHIPPING_FLAT = 79;
var FREE_SHIPPING_OVER = 1499;

function findProduct(products, id) {
  for (var i = 0; i < products.length; i++) if (products[i].id === id) return products[i];
  return null;
}

/* Whole-rupee en-IN formatting, e.g. 1499 -> "₹1,499". */
var _inr = null;
function rupee(n) {
  if (!_inr) _inr = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
  return _inr.format(Math.round(n));
}

function shippingFor(subtotal) {
  if (subtotal <= 0) return 0;
  return subtotal >= FREE_SHIPPING_OVER ? 0 : SHIPPING_FLAT;
}

/* cart: {id: qty}. Returns lines, counts and totals from one code path. */
function cartTotals(cart, products) {
  var lines = [], subtotal = 0, count = 0;
  var ids = Object.keys(cart || {});
  for (var i = 0; i < ids.length; i++) {
    var p = findProduct(products, ids[i]);
    var qty = Math.max(0, Math.floor(cart[ids[i]] || 0));
    if (!p || qty <= 0) continue;
    var line_total = p.price_inr * qty;
    lines.push({ id: p.id, name: p.name, price_inr: p.price_inr, qty: qty, line_total: line_total });
    subtotal += line_total;
    count += qty;
  }
  var shipping = shippingFor(subtotal);
  return { lines: lines, count: count, subtotal_inr: subtotal, shipping_inr: shipping, total_inr: subtotal + shipping };
}

function isValidEmail(email) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(String(email || "").trim());
}

function makeRef(now) {
  var d = now instanceof Date ? now : new Date(now);
  var abc = "ABCDEFGHJKMNPQRSTUVWXYZ23456789", tail = "";
  var seed = d.getTime();
  for (var i = 0; i < 4; i++) { seed = (seed * 9301 + 49297) % 233280; tail += abc[Math.floor(seed / 233280 * abc.length)]; }
  return "WW-" + d.getFullYear() + "-" + tail;
}

/* Builds the order payload in the plan's data model, or {error}. Never lets an out-of-stock line through. */
function buildOrder(cart, products, customer, now) {
  var totals = cartTotals(cart, products);
  if (totals.count === 0) return { error: "Your cart is empty. Add a candle first." };
  var ids = Object.keys(cart || {});
  for (var i = 0; i < ids.length; i++) {
    var qty = Math.floor(cart[ids[i]] || 0);
    if (qty <= 0) continue;
    var p = findProduct(products, ids[i]);
    if (!p || p.status !== "in_stock") return { error: "Sorry, “" + (p ? p.name : ids[i]) + "” is out of stock and cannot be ordered." };
  }
  var name = String(customer.name || "").trim();
  var email = String(customer.email || "").trim();
  var area = String(customer.area || "").trim();
  if (!name) return { error: "Please tell us your name." };
  if (!isValidEmail(email)) return { error: "That email address does not look right. Please check it." };
  if (!area) return { error: "Please tell us your delivery area (city and PIN)." };
  var at = now instanceof Date ? now : new Date(now);
  return {
    order: {
      order_ref: makeRef(at),
      created_at: at.toISOString(),
      customer_name: name,
      customer_email: email,
      delivery_notes: String(customer.notes || "").trim(),
      delivery_area: area,
      currency: "INR",
      items: totals.lines,
      subtotal_inr: totals.subtotal_inr,
      shipping_inr: totals.shipping_inr,
      total_inr: totals.total_inr,
      status: "new"
    }
  };
}

/* Smallest-unit price (paise, cents) as "₹499". Server prices only; the page never invents one. */
function formatPrice(amount, currency) {
  var cur = currency || "INR";
  var v = (Number(amount) || 0) / 100;
  try {
    return new Intl.NumberFormat("en-IN", { style: "currency", currency: cur, minimumFractionDigits: 0, maximumFractionDigits: 2 }).format(v);
  } catch (e) { return String(Math.round(v * 100) / 100) + " " + cur; }
}

/* Index a _catalog items array by sku. */
function catalogBySku(items) {
  var map = {};
  (items || []).forEach(function (it) { if (it && it.sku) map[it.sku] = it; });
  return map;
}

/* Products priced from the catalog where it has a sku, local prices otherwise. */
function pricedWithCatalog(products, bySku) {
  return (products || []).map(function (p) {
    var hit = bySku && bySku[p.id];
    if (hit && Number(hit.amount) > 0) {
      return { id: p.id, name: p.name, scent_notes: p.scent_notes, image: p.image, status: p.status, price_inr: Math.round(Number(hit.amount) / 100) };
    }
    return p;
  });
}

/* Payment payload: items plus customer, never an amount — the server works it out. */
function buildPayment(cart, products, customer) {
  var ids = Object.keys(cart || {});
  var items = [];
  for (var i = 0; i < ids.length; i++) {
    var qty = Math.floor(cart[ids[i]] || 0);
    if (qty <= 0) continue;
    var p = findProduct(products, ids[i]);
    if (!p) continue;
    if (p.status !== "in_stock") return { error: "Sorry, “" + p.name + "” is out of stock and cannot be paid for." };
    items.push({ sku: p.id, qty: qty });
  }
  if (!items.length) return { error: "Your cart is empty. Add a candle first." };
  var name = String(customer.name || "").trim();
  var email = String(customer.email || "").trim();
  if (!name) return { error: "Please tell us your name." };
  if (!isValidEmail(email)) return { error: "That email address does not look right. Please check it." };
  return { payment: { items: items, name: name, email: email, area: String(customer.area || "").trim(), notes: String(customer.notes || "").trim() } };
}

/* Duplicate guard: same cart+email+total submitted twice reuses the first ref. */
function orderSignature(cart, email, total) {
  return JSON.stringify({ cart: cart, email: String(email).toLowerCase().trim(), total: total });
}

if (typeof module !== "undefined") {
  module.exports = { SHIPPING_FLAT: SHIPPING_FLAT, FREE_SHIPPING_OVER: FREE_SHIPPING_OVER, rupee: rupee, shippingFor: shippingFor, cartTotals: cartTotals, isValidEmail: isValidEmail, makeRef: makeRef, buildOrder: buildOrder, orderSignature: orderSignature, formatPrice: formatPrice, catalogBySku: catalogBySku, pricedWithCatalog: pricedWithCatalog, buildPayment: buildPayment };
}
