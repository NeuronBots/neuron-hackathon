/* app.js — one job: binds products.js + shop.js to the page DOM. */
(function () {
  "use strict";
  var grid = document.getElementById("product-grid");
  var linesEl = document.getElementById("cart-lines");
  var emptyEl = document.getElementById("cart-empty");
  var form = document.getElementById("order-form");
  var errEl = document.getElementById("form-error");
  var thanksEl = document.getElementById("thanks");
  var orderGrid = document.getElementById("order-grid");
  var bar = document.getElementById("sticky-bar");

  var cart = {};
  try { cart = JSON.parse(localStorage.getItem("ww_cart") || "{}") || {}; } catch (e) { cart = {}; }

  /* Server prices (_catalog) win wherever the server has a sku; local list is the fallback. */
  var catalog = {};
  var catalogLive = false;
  function activeProducts() { return catalogLive ? pricedWithCatalog(PRODUCTS, catalog) : PRODUCTS; }
  function escapeHtml(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function cardPrice(p) {
    var hit = catalogLive && catalog[p.id];
    if (hit && Number(hit.amount) > 0) return formatPrice(hit.amount, hit.currency);
    return rupee(p.price_inr);
  }

  function save() { try { localStorage.setItem("ww_cart", JSON.stringify(cart)); } catch (e) {} }
  function fail(msg) { errEl.textContent = msg; errEl.hidden = false; errEl.scrollIntoView({ block: "nearest" }); }
  function clearFail() { errEl.hidden = true; errEl.textContent = ""; }

  /* Product cards render from the one product list. */
  function renderProducts() {
    grid.querySelectorAll(".card").forEach(function (n) { n.remove(); });
    PRODUCTS.forEach(function (p) {
      var sold = p.status !== "in_stock";
      var card = document.createElement("article");
      card.className = "card" + (sold ? " soldout" : "");
      card.innerHTML =
        '<img src="' + p.image + '" alt="Wick and Wax ' + p.name + ' soy candle (' + p.scent_notes + ')" width="640" height="544" loading="lazy" />' +
        '<div class="card-body"><h3>' + p.name + "</h3>" +
        '<p class="scent">' + p.scent_notes + "</p>" +
        '<div class="price-row"><span class="price">' + escapeHtml(cardPrice(p)) + '</span><span class="per">220 g · ~45 h burn</span></div>' +
        '<span class="stock-flag ' + (sold ? "out-stock" : "in-stock") + '">' + (sold ? "Out of stock" : "In stock") + "</span>" +
        '<div class="buy-row"><span class="stepper" role="group" aria-label="Quantity for ' + p.name + '">' +
        '<button type="button" data-dec="' + p.id + '" aria-label="One fewer">−</button>' +
        '<output id="q-' + p.id + '">1</output>' +
        '<button type="button" data-inc="' + p.id + '" aria-label="One more">+</button></span>' +
        '<button type="button" class="btn small add-btn" data-add="' + p.id + '"' + (sold ? " disabled" : "") + ">" +
        (sold ? "Unavailable" : "Add to cart") + "</button></div></div>";
      grid.appendChild(card);
    });
  }

  function cardQty(id) {
    var o = document.getElementById("q-" + id);
    return Math.max(1, parseInt(o ? o.textContent : "1", 10) || 1);
  }

  grid.addEventListener("click", function (ev) {
    var t = ev.target;
    var inc = t.getAttribute("data-inc"), dec = t.getAttribute("data-dec"), add = t.getAttribute("data-add");
    if (inc) { var o1 = document.getElementById("q-" + inc); o1.textContent = cardQty(inc) + 1; }
    else if (dec) { var o2 = document.getElementById("q-" + dec); o2.textContent = Math.max(1, cardQty(dec) - 1); }
    else if (add) {
      var p = null;
      PRODUCTS.forEach(function (x) { if (x.id === add) p = x; });
      if (!p || p.status !== "in_stock") return; /* stock gate at the button */
      cart[add] = (cart[add] || 0) + cardQty(add);
      save(); renderCart();
      document.getElementById("order").scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  });

  /* Cart section, header count, totals and sticky bar share one totals call. */
  function renderCart() {
    var t = cartTotals(cart, activeProducts());
    document.getElementById("cart-count").textContent = t.count;
    document.getElementById("t-sub").textContent = rupee(t.subtotal_inr);
    document.getElementById("t-ship").textContent = t.shipping_inr === 0 && t.subtotal_inr > 0 ? "Free" : rupee(t.shipping_inr);
    document.getElementById("t-total").textContent = rupee(t.total_inr);
    document.getElementById("sticky-total").textContent = rupee(t.total_inr) + " · " + t.count + " item" + (t.count === 1 ? "" : "s");
    bar.hidden = t.count === 0;
    var hint = document.getElementById("ship-hint");
    if (t.subtotal_inr > 0 && t.shipping_inr > 0) hint.textContent = "Add " + rupee(FREE_SHIPPING_OVER - t.subtotal_inr) + " more for free shipping.";
    else if (t.subtotal_inr > 0) hint.textContent = "You have unlocked free shipping.";
    else hint.textContent = "";
    linesEl.innerHTML = "";
    emptyEl.style.display = t.lines.length ? "none" : "";
    t.lines.forEach(function (l) {
      var li = document.createElement("li");
      li.innerHTML = '<div><div class="name">' + l.name + '</div><div class="scent">' + rupee(l.price_inr) + " each</div></div>" +
        '<span class="stepper" role="group" aria-label="Quantity for ' + l.name + '">' +
        '<button type="button" data-cdec="' + l.id + '" aria-label="One fewer">−</button><output>' + l.qty + "</output>" +
        '<button type="button" data-cinc="' + l.id + '" aria-label="One more">+</button></span>' +
        '<span class="line-total">' + rupee(l.line_total) + '</span>' +
        '<button type="button" class="remove" data-rm="' + l.id + '">Remove</button>';
      linesEl.appendChild(li);
    });
    var btn = document.getElementById("place-btn");
    var note = document.getElementById("pay-note");
    if (catalogLive) {
      note.hidden = true;
      btn.textContent = t.count ? "Pay " + rupee(t.total_inr) : "Pay now";
    } else {
      note.hidden = false;
      btn.textContent = t.count ? "Place order · " + rupee(t.total_inr) : "Place order";
    }
  }

  linesEl.addEventListener("click", function (ev) {
    var t = ev.target;
    var id = t.getAttribute("data-cinc") || t.getAttribute("data-cdec") || t.getAttribute("data-rm");
    if (!id) return;
    if (t.hasAttribute("data-cinc")) cart[id] = (cart[id] || 0) + 1;
    else if (t.hasAttribute("data-cdec")) { cart[id] = (cart[id] || 0) - 1; if (cart[id] <= 0) delete cart[id]; }
    else delete cart[id];
    save(); renderCart();
  });

  /* Restore the thank-you screen after a refresh instead of re-submitting. */
  try {
    var saved = JSON.parse(sessionStorage.getItem("ww_last_order") || "null");
    if (saved && saved.order_ref) showThanks(saved, true);
  } catch (e) {}

  /* One checkout: pay online when the catalog is live, otherwise the
     request order below keeps working. */
  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    clearFail();
    if (catalogLive) { payCheckout(); return; }
    var customer = {
      name: form.name.value, email: form.email.value,
      area: form.area.value, notes: form.notes.value
    };
    var built = buildOrder(cart, PRODUCTS, customer, new Date());
    if (built.error) { fail(built.error); return; }
    var order = built.order;
    var sig = orderSignature(cart, order.customer_email, order.total_inr);
    var btn = document.getElementById("place-btn");
    btn.disabled = true;
    var payload = { kind: "order" };
    Object.keys(order).forEach(function (k) { payload[k] = order[k]; });
    fetch("_action", { method: "POST", body: JSON.stringify(payload).slice(0, 4000) })
      .then(function (r) { if (!r.ok) throw new Error("bad status"); return r.json(); })
      .then(function (body) {
        if (!body || body.ok !== true) throw new Error("not ok");
        finishOrder(order, sig);
      })
      .catch(function () {
        btn.disabled = false;
        fail("We could not send your order just now. Check your connection and try again — nothing was charged and your cart is kept.");
      });
  });

  /* Online payment: the form's own fields go with the items, never an amount.
     The card is only ever typed on Razorpay's own page, so this page has no card form. */
  function payCheckout() {
    var built = buildPayment(cart, activeProducts(), {
      name: form.name.value, email: form.email.value,
      area: form.area.value, notes: form.notes.value
    });
    if (built.error) { fail(built.error); return; }
    var btn = document.getElementById("place-btn");
    btn.disabled = true;
    fetch("_pay", { method: "POST", body: JSON.stringify(built.payment) })
      .then(function (r) { if (!r.ok) throw new Error("bad status"); return r.json(); })
      .then(function (body) {
        if (!body || body.ok === false) throw new Error((body && body.error) || "not ok");
        if (body.test === true) document.getElementById("test-pill").hidden = false;
        var pay_url = body.pay_url;
        if (!pay_url) throw new Error("The server did not send a payment page. Try again.");
        btn.disabled = false;
        if (window.top === window) { location.href = pay_url } else { window.open(pay_url, '_blank') }
      })
      .catch(function (err) {
        btn.disabled = false;
        var msg = err && err.message ? err.message : "";
        fail(msg && msg !== "bad status" && msg !== "not ok" ? msg : "We could not start your payment just now. Check your connection and try again — nothing was charged.");
      });
  }

  function finishOrder(order, sig) {
    try {
      var prior = JSON.parse(sessionStorage.getItem("ww_last_order") || "null");
      if (prior && prior.sig === sig) { showThanks(prior, true); return; } /* no duplicate order */
      var record = { sig: sig, order_ref: order.order_ref, email: order.customer_email, total: order.total_inr, items: order.items, at: order.created_at };
      sessionStorage.setItem("ww_last_order", JSON.stringify(record));
    } catch (e) {}
    cart = {}; save(); renderCart();
    showThanks({ order_ref: order.order_ref, email: order.customer_email, total: order.total_inr, items: order.items, at: order.created_at }, false);
  }

  function showThanks(rec, restored) {
    form.hidden = true;
    thanksEl.hidden = false;
    document.getElementById("thanks-title").textContent = "Order placed. Thank you.";
    document.getElementById("thanks-refund").hidden = true;
    if (orderGrid) orderGrid.style.gridTemplateColumns = "";
    document.getElementById("thanks-ref").textContent = rec.order_ref;
    document.getElementById("thanks-email").textContent = rec.email;
    var when = "";
    try {
      when = new Date(rec.at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit" });
    } catch (e) {}
    var html = "<ul>";
    (rec.items || []).forEach(function (l) { html += "<li>" + l.qty + " × " + l.name + " — " + rupee(l.line_total) + "</li>"; });
    html += "</ul><p><strong>Total: " + rupee(rec.total) + "</strong>" + (when ? " · ordered " + when + " IST" : "") + "</p>";
    document.getElementById("thanks-summary").innerHTML = html;
    if (!restored) thanksEl.scrollIntoView({ behavior: "smooth" });
  }

  document.getElementById("new-order-btn").addEventListener("click", function () {
    try { sessionStorage.removeItem("ww_last_order"); } catch (e) {}
    thanksEl.hidden = true;
    document.getElementById("thanks-refund").hidden = true;
    form.hidden = false;
    form.reset();
  });

  /* Prices come from the server. Empty, disabled or unreachable catalog:
     say prices are coming soon and keep the request form working on local prices. */
  function showComingSoon() {
    var n = document.getElementById("price-note");
    if (n) n.hidden = false;
  }
  fetch("_catalog").then(function (r) { return r.json(); }).then(function (body) {
    if (body && body.ok && body.enabled !== false && Array.isArray(body.items) && body.items.length) {
      catalog = catalogBySku(body.items);
      catalogLive = true;
      renderProducts();
      renderCart();
    } else showComingSoon();
  }).catch(showComingSoon);

  /* Razorpay sends the customer back with ?paid=<payment_id>. Coming back is
     not proof of payment: ask the server until it says paid (at most 60 s).
     The checkout's own thanks box carries the whole flow. */
  var paidId = null;
  try { paidId = new URLSearchParams(location.search).get("paid"); } catch (e) { paidId = null; }
  if (paidId) confirmPayment(paidId);

  function confirmPayment(pid) {
    form.hidden = true;
    thanksEl.hidden = false;
    document.getElementById("thanks-title").textContent = "Confirming your payment…";
    document.getElementById("thanks-ref").textContent = pid;
    document.getElementById("thanks-email").textContent = "";
    document.getElementById("thanks-summary").innerHTML = "<p>Reference <strong>" + escapeHtml(pid) + "</strong>. Waiting for the payment server — this can take a few seconds.</p>";
    document.getElementById("thanks-refund").hidden = true;
    thanksEl.scrollIntoView();
    var tries = 0;
    check();
    var timer = setInterval(check, 2000);
    function check() {
      tries++;
      fetch("_pay/" + encodeURIComponent(pid)).then(function (r) { return r.json(); }).then(function (st) {
        if (st && st.status === "paid") { clearInterval(timer); showPaid(pid, st); }
        else if (st && st.status === "failed") { clearInterval(timer); showNotPaid(pid); }
        else if (tries >= 30) { clearInterval(timer); showConfirming(pid); }
      }).catch(function () { if (tries >= 30) { clearInterval(timer); showConfirming(pid); } });
    }
  }

  function paymentLines(st) {
    if (!st || !Array.isArray(st.items) || !st.items.length) return "";
    var html = "<ul>";
    st.items.forEach(function (l) {
      html += "<li>" + escapeHtml(l.qty || 1) + " × " + escapeHtml(l.name || l.sku || "candle");
      if (l.amount != null) html += " — " + escapeHtml(formatPrice(l.amount, l.currency || st.currency));
      html += "</li>";
    });
    return html + "</ul>";
  }

  function showPaid(pid, st) {
    document.getElementById("thanks-title").textContent = "Payment confirmed. Thank you.";
    document.getElementById("thanks-ref").textContent = pid;
    document.getElementById("thanks-email").textContent = (st && st.email) || "";
    var html = paymentLines(st);
    if (st.amount != null) html += "<p><strong>Total paid: " + escapeHtml(formatPrice(st.amount, st.currency)) + "</strong></p>";
    html += "<p>Keep this reference: <strong>" + escapeHtml(pid) + "</strong></p>";
    if (st.test === true) html += '<p class="test-pill">Test mode: no real money</p>';
    document.getElementById("thanks-summary").innerHTML = html;
    var fold = document.getElementById("thanks-refund");
    fold.hidden = false;
    try { document.getElementById("refund-form").payment_id.value = pid; } catch (e) {}
    thanksEl.scrollIntoView();
    cart = {}; save(); renderCart();
  }

  function showNotPaid(pid) {
    document.getElementById("thanks-title").textContent = "That payment did not go through.";
    document.getElementById("thanks-ref").textContent = pid;
    document.getElementById("thanks-summary").innerHTML = "<p>Reference <strong>" + escapeHtml(pid) + "</strong> was not paid. No money left your account — check your cart and pay again.</p>";
    thanksEl.scrollIntoView();
  }

  function showConfirming(pid) {
    document.getElementById("thanks-title").textContent = "Still being confirmed.";
    document.getElementById("thanks-ref").textContent = pid;
    document.getElementById("thanks-summary").innerHTML = "<p>Your payment is being confirmed. Keep this reference: <strong>" + escapeHtml(pid) + "</strong> — write to us and we will check it.</p>";
    thanksEl.scrollIntoView();
  }

  /* Refund requests: reference plus reason. Only a request — the owner approves it. */
  function wireRefund(formId, errId, doneId, btnId) {
    var refundForm = document.getElementById(formId);
    if (!refundForm) return;
    refundForm.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var errEl = document.getElementById(errId);
      var doneEl = document.getElementById(doneId);
      errEl.hidden = true; doneEl.hidden = true;
      var pid = String(refundForm.payment_id.value || "").trim();
      var reason = String(refundForm.reason.value || "").trim();
      if (!pid) { errEl.textContent = "Please tell us the payment reference."; errEl.hidden = false; return; }
      if (!reason) { errEl.textContent = "Please tell us the reason."; errEl.hidden = false; return; }
      var btn = document.getElementById(btnId);
      btn.disabled = true;
      fetch("_refund", { method: "POST", body: JSON.stringify({ payment_id: pid, reason: reason }) })
        .then(function (r) { return r.json(); })
        .then(function (body) {
          btn.disabled = false;
          if (body && body.ok) {
            doneEl.textContent = "Refund requested — the owner approves each request." + (body.message ? " " + body.message : "");
            doneEl.hidden = false;
          } else {
            errEl.textContent = (body && body.error) || "That did not go through. Try again.";
            errEl.hidden = false;
          }
        })
        .catch(function () {
          btn.disabled = false;
          errEl.textContent = "That did not go through. Check your connection and try again.";
          errEl.hidden = false;
        });
    });
  }
  wireRefund("refund-form", "refund-error", "refund-done", "refund-btn");
  wireRefund("foot-refund-form", "foot-refund-error", "foot-refund-done", "foot-refund-btn");

  renderProducts();
  renderCart();
})();
