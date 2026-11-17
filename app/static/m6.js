async function postJSON(url, body) {
  const response = await fetch(url, {method: "POST", headers: {"content-type": "application/json"}, body: JSON.stringify(body || {})});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail?.message || "请求失败");
  return data;
}

const out = document.querySelector("#result");
const feedback = document.querySelector("#feedback");
function show(data) { out.textContent = JSON.stringify(data, null, 2); }
function fail(error) { out.textContent = error.message; feedback.textContent = "失败，可重试"; }

document.querySelectorAll("[data-close]").forEach((button) => {
  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true; feedback.textContent = "关账中";
    try {
      const data = await postJSON("/api/periods/close", {period: button.dataset.close, expected_version: Number(button.dataset.version)});
      show(data); feedback.textContent = "已关账";
      setTimeout(() => location.reload(), 300);
    } catch (error) { fail(error); button.disabled = false; }
  });
});

document.querySelectorAll("[data-reopen]").forEach((button) => {
  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true; feedback.textContent = "重开中";
    try {
      const data = await postJSON("/api/periods/reopen", {period: button.dataset.reopen});
      show(data); feedback.textContent = "已重开";
      setTimeout(() => location.reload(), 300);
    } catch (error) { fail(error); button.disabled = false; }
  });
});

function formData(form) {
  return Object.fromEntries(new FormData(form).entries());
}

document.querySelector("#period-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  try { show(await postJSON("/api/periods", formData(event.target))); feedback.textContent = "已开账"; setTimeout(() => location.reload(), 300); }
  catch (error) { fail(error); }
});

document.querySelector("#receipt-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  try { show(await postJSON("/api/receipts", formData(event.target))); feedback.textContent = "验收已登记"; setTimeout(() => location.reload(), 300); }
  catch (error) { fail(error); }
});

document.querySelector("#invoice-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = formData(event.target);
  delete payload.adjust_period;
  try {
    const data = await postJSON("/api/invoices", payload);
    show(data); feedback.textContent = data.cross_period ? "已跨期冲回" : "已登记";
    setTimeout(() => location.reload(), 300);
  } catch (error) { fail(error); }
});
