const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const rupees = paise => new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 2 }).format((paise || 0) / 100);
const dateText = value => value ? new Date(`${value}T00:00:00`).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" }) : "";
let products = [];
let accounts = [];

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  if (response.status === 401) {
    showLogin();
    throw new Error("Please sign in to continue.");
  }
  const result = response.headers.get("content-type")?.includes("application/json") ? await response.json() : null;
  if (!response.ok) throw new Error(result?.error || "Request failed. Please try again.");
  return result;
}

function showLogin() {
  $("#app-shell").classList.add("hidden");
  $("#login-screen").classList.remove("hidden");
}

function showApp() {
  $("#login-screen").classList.add("hidden");
  $("#app-shell").classList.remove("hidden");
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("visible");
  window.setTimeout(() => node.classList.remove("visible"), 2800);
}

async function start() {
  try {
    const status = await api("/api/session");
    if (!status.authenticated) return showLogin();
    showApp();
    $("#today-label").textContent = new Date().toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
    await refresh();
  } catch (error) {
    if (error.message !== "Please sign in to continue.") toast(error.message);
  }
}

async function refresh() {
  const [dashboard, productList, transactionList, accountList] = await Promise.all([
    api("/api/dashboard"), api("/api/products"), api("/api/transactions"), api("/api/accounts")
  ]);
  products = productList;
  accounts = accountList;
  renderDashboard(dashboard);
  renderProducts(productList);
  renderTransactions(transactionList.slice(0, 8));
  populateOptions();
}

function renderDashboard(data) {
  const m = data.metrics;
  $("#metric-revenue").textContent = rupees(m.revenue_paise);
  $("#metric-profit").textContent = rupees(m.operating_profit_paise);
  $("#metric-inventory").textContent = rupees(m.inventory_value_paise);
  $("#metric-units").textContent = `${new Intl.NumberFormat("en-IN", { maximumFractionDigits: 3 }).format(m.inventory_units)} units in stock`;
  $("#metric-month-revenue").textContent = `${rupees(m.month_revenue_paise)} this month`;
  $("#month-sales").textContent = rupees(m.month_revenue_paise);
  $("#month-expenses").textContent = rupees(m.month_expenses_paise);
  $("#gross-profit").textContent = rupees(m.gross_profit_paise);
  const gst = m.gst_estimate_paise;
  $("#metric-gst").textContent = rupees(Math.abs(gst));
  $("#gst-subtitle").textContent = gst > 0 ? "Estimated net payable based on entries" : gst < 0 ? "Estimated eligible input credit exceeds output GST" : "No net GST position from entries yet";
  $("#account-list").innerHTML = data.accounts.map(account => `
    <div class="account-row"><span class="account-name"><i>${account.account_key === "gst_reserve" ? "◎" : account.account_key === "marketplace" ? "⇄" : "₹"}</i>${escapeHtml(account.name)}</span><strong>${rupees(account.balance_paise)}</strong></div>`).join("");
  const partners = $("#partner-list");
  partners.innerHTML = data.partners.length
    ? data.partners.map(partner => `<div class="partner-chip"><span>${escapeHtml(partner.partner)}</span><strong>${rupees(partner.balance_paise)}</strong></div>`).join("")
    : '<p class="empty-state">Record a partner contribution or withdrawal to see balances.</p>';
}

function renderProducts(list) {
  const body = $("#product-table");
  body.innerHTML = list.length ? list.map(product => `
    <tr><td>${escapeHtml(product.name)}</td><td>${escapeHtml(product.sku)}</td>
    <td>${new Intl.NumberFormat("en-IN", { maximumFractionDigits: 3 }).format(product.stock_qty)}</td>
    <td>${rupees(product.avg_cost_paise)}</td><td>${rupees(product.sell_price_paise)}</td><td>${product.gst_rate}%</td></tr>`).join("")
    : '<tr><td colspan="6" class="empty-state">No products yet. Add your first product to start tracking stock.</td></tr>';
}

function renderTransactions(list) {
  const body = $("#transaction-table");
  body.innerHTML = list.length ? list.map(tx => {
    const context = tx.channel || tx.category || tx.partner || (tx.kind === "transfer" ? `${tx.from_account} → ${tx.to_account}` : "");
    return `<tr><td>${escapeHtml(tx.description || tx.product_name || "Transfer")}</td><td><span class="type-pill ${tx.kind}">${escapeHtml(tx.kind.replaceAll("_", " "))}</span></td><td>${dateText(tx.transaction_date)}</td><td>${escapeHtml(context)}</td><td>${rupees(tx.total_paise)}</td></tr>`;
  }).join("") : '<tr><td colspan="5" class="empty-state">No entries recorded yet.</td></tr>';
}

function populateOptions() {
  $("#entry-product").innerHTML = '<option value="">Select product</option>' + products.map(p => `<option value="${p.id}">${escapeHtml(p.name)} · ${p.stock_qty} in stock</option>`).join("");
  $$(".account-options").forEach(select => {
    const previous = select.value;
    select.innerHTML = accounts.map(a => `<option value="${a.key}">${escapeHtml(a.name)}</option>`).join("");
    if (previous && accounts.some(a => a.key === previous)) select.value = previous;
  });
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function toggleEntryFields() {
  const kind = $("#entry-kind").value;
  const productEntry = ["sale", "purchase", "sales_return"].includes(kind);
  const needsAmount = ["expense", "partner_contribution", "partner_withdrawal", "transfer", "sales_return"].includes(kind);
  $(".entry-product-fields").classList.toggle("hidden", !productEntry);
  $(".entry-amount-field").classList.toggle("hidden", !needsAmount);
  $(".entry-category-field").classList.toggle("hidden", kind !== "expense");
  $(".entry-partner-field").classList.toggle("hidden", !kind.startsWith("partner_"));
  $(".entry-transfer-fields").classList.toggle("hidden", kind !== "transfer");
  $(".entry-restock-field").classList.toggle("hidden", kind !== "sales_return");
  $(".entry-credit-field").classList.toggle("hidden", !["purchase", "expense"].includes(kind));
  $(".entry-tax-field").classList.toggle("hidden", kind !== "expense");
  $(".entry-channel-field").classList.toggle("hidden", !["sale", "purchase", "sales_return", "expense"].includes(kind));
  $(".entry-payment-field").classList.toggle("hidden", kind === "transfer");
  const description = $("#entry-description-label");
  description.classList.toggle("hidden", kind === "transfer");
  const descriptionInput = description.querySelector("input");
  descriptionInput.required = kind !== "transfer";
  const unitPrice = $("#unit-price-label");
  unitPrice.classList.toggle("hidden", kind === "sales_return");
  $("#entry-product").required = productEntry;
  $('[name="quantity"]').required = productEntry;
  $('[name="unit_price"]').required = ["sale", "purchase"].includes(kind);
  $('[name="amount"]').required = needsAmount;
  $('[name="gst_credit_eligible"]').checked = kind === "purchase";
  if (kind === "purchase") {
    $("#unit-price-label").firstChild.textContent = "Unit cost (₹)";
    $("#payment-label").firstChild.textContent = "Paid from";
  } else {
    $("#unit-price-label").firstChild.textContent = "Unit price (₹)";
    $("#payment-label").firstChild.textContent = kind === "sale" ? "Received into" : "Refunded from";
  }
  $("#amount-label").firstChild.textContent = kind === "sales_return" ? "Refund before GST (₹)" : "Amount (₹)";
  if (kind === "expense") $("#amount-label").firstChild.textContent = "Expense before GST (₹)";
}

function openDialog(dialog) {
  dialog.showModal();
}

function formObject(form) {
  return Object.fromEntries(new FormData(form).entries());
}

$("#login-form").addEventListener("submit", async event => {
  event.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("/api/login", { method: "POST", body: JSON.stringify({ password: $("#password").value }) });
    $("#password").value = "";
    showApp();
    await refresh();
  } catch (error) {
    $("#login-error").textContent = error.message;
  }
});

$("#logout-button").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  showLogin();
});

$("#new-entry-button").addEventListener("click", () => {
  $("#entry-form").reset();
  $("#entry-form").elements.date.value = new Date().toISOString().slice(0, 10);
  $("#entry-error").textContent = "";
  toggleEntryFields();
  openDialog($("#entry-dialog"));
});
$("#entry-kind").addEventListener("change", toggleEntryFields);
$("#entry-product").addEventListener("change", () => {
  const product = products.find(item => String(item.id) === $("#entry-product").value);
  if (product && $("#entry-kind").value === "sale") $('[name="unit_price"]').value = (product.sell_price_paise / 100).toFixed(2);
  if (product && $("#entry-kind").value === "purchase") $('[name="gst_rate"]').value = product.gst_rate;
});
$("#add-product-button").addEventListener("click", () => {
  $("#product-form").reset();
  $("#product-error").textContent = "";
  openDialog($("#product-dialog"));
});
$("#opening-balance-button").addEventListener("click", () => {
  $("#opening-form").reset();
  $("#opening-error").textContent = "";
  openDialog($("#opening-dialog"));
});
$$(".close-dialog").forEach(button => button.addEventListener("click", () => button.closest("dialog").close()));
$("#entry-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = formObject(form);
  payload.restocked = form.elements.restocked.checked;
  payload.gst_credit_eligible = form.elements.gst_credit_eligible.checked;
  $("#entry-error").textContent = "";
  try {
    await api("/api/transactions", { method: "POST", body: JSON.stringify(payload) });
    $("#entry-dialog").close();
    toast("Entry saved.");
    await refresh();
  } catch (error) { $("#entry-error").textContent = error.message; }
});
$("#product-form").addEventListener("submit", async event => {
  event.preventDefault();
  $("#product-error").textContent = "";
  try {
    await api("/api/products", { method: "POST", body: JSON.stringify(formObject(event.currentTarget)) });
    $("#product-dialog").close();
    toast("Product added.");
    await refresh();
  } catch (error) { $("#product-error").textContent = error.message; }
});
$("#opening-form").addEventListener("submit", async event => {
  event.preventDefault();
  $("#opening-error").textContent = "";
  try {
    await api("/api/accounts/opening-balance", { method: "POST", body: JSON.stringify(formObject(event.currentTarget)) });
    $("#opening-dialog").close();
    toast("Opening balance saved.");
    await refresh();
  } catch (error) { $("#opening-error").textContent = error.message; }
});

document.querySelectorAll(".nav-link[href^='#']").forEach(link => link.addEventListener("click", () => {
  document.querySelectorAll(".nav-link[href^='#']").forEach(item => item.classList.remove("active"));
  link.classList.add("active");
}));
start();
