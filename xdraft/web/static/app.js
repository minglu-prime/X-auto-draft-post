// Tiny progressive-enhancement layer: forms marked data-post submit via fetch with the
// X-Xdraft header (required by the server) and swap the returned HTML into the page.
(function () {
  const LIGHT = [[0, 4351], [8192, 8205], [8208, 8223], [8242, 8247]];
  const URL_RE = /https?:\/\/[^\s<>"'）)]+/g;
  function weighted(text) {
    let n = 0;
    text = text.replace(URL_RE, () => { n += 23; return ""; });
    for (const ch of text) {
      const cp = ch.codePointAt(0);
      n += LIGHT.some(([lo, hi]) => cp >= lo && cp <= hi) ? 1 : 2;
    }
    return n;
  }
  function updateCounter(ta) {
    const out = document.querySelector(ta.dataset.counter);
    if (!out) return;
    out.innerHTML = "";
    ta.value.split(/^\s*-{3,}\s*$/m).map(s => s.trim()).filter(Boolean).forEach(t => {
      const n = weighted(t), span = document.createElement("span");
      span.textContent = n + "/280";
      if (n > 280) span.className = "over";
      out.appendChild(span);
    });
  }

  // Toasts: success fades out, errors stay until closed.
  function toast(text, kind) {
    const box = document.getElementById("toasts");
    if (!box || !text) return;
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.textContent = text;
    const close = document.createElement("button");
    close.className = "link";
    close.textContent = "×";
    close.title = "关闭";
    close.onclick = () => el.remove();
    el.appendChild(close);
    box.appendChild(el);
    if (kind === "ok") setTimeout(() => el.remove(), 4000);
  }
  function htmlText(html) {
    const div = document.createElement("div");
    div.innerHTML = html;
    return div.textContent.trim();
  }
  function go(url) {
    const u = new URL(url, location.href);
    const samePage = u.pathname === location.pathname && u.search === location.search;
    if (!samePage) { location.href = u.href; return; }
    location.hash = u.hash;  // a hash change alone does not reload, but the page must show new data
    history.scrollRestoration = "manual";  // land on the #section, not the old scroll position
    location.reload();
  }

  // After a redirect the page may scroll to a section, so also surface the server's one-shot message as a toast.
  document.querySelectorAll(".flash > p").forEach(p => toast(p.textContent.trim(), p.classList.contains("warn") ? "err" : "ok"));

  document.addEventListener("input", e => {
    if (e.target.matches("textarea[data-counter]")) updateCounter(e.target);
  });
  document.addEventListener("submit", async e => {
    const form = e.target.closest("form[data-post]");
    if (!form) return;
    e.preventDefault();
    if (form.dataset.confirm && !confirm(form.dataset.confirm)) return;
    const tz = form.querySelector("input[name=tz_offset]");
    if (tz) tz.value = new Date().getTimezoneOffset();
    const action = (e.submitter && e.submitter.getAttribute("formaction")) || form.action;
    const body = new FormData(form, e.submitter);

    // Busy state: every button tied to this form is disabled; the clicked one shows elapsed seconds.
    const buttons = [...form.querySelectorAll("button"), ...document.querySelectorAll(`button[form="${form.id}"]`)];
    buttons.forEach(b => { b.disabled = true; b.dataset.label = b.textContent; });
    const clicked = e.submitter || form.querySelector("button");
    const busyText = (clicked && clicked.dataset.busy) || "处理中";
    const started = Date.now();
    const tick = () => { if (clicked) clicked.textContent = `${busyText}… ${Math.round((Date.now() - started) / 1000)}s`; };
    tick();
    const timer = setInterval(tick, 1000);
    const bar = document.getElementById("busy-bar");
    if (bar) bar.hidden = false;
    try {
      const resp = await fetch(action, { method: "POST", body, headers: { "X-Xdraft": "1" } });
      const redirect = resp.headers.get("X-Redirect");
      if (redirect) { go(redirect); return; }
      const html = resp.status === 204 ? "" : await resp.text();
      if (!resp.ok) { toast(`出错了（${resp.status}）：${htmlText(html) || resp.statusText}`, "err"); return; }
      const target = form.dataset.target ? document.querySelector(form.dataset.target) : form.closest("[data-swap]");
      if (target && target.hasAttribute("data-swap")) target.outerHTML = html;
      else if (target) target.innerHTML = html;
      if (form.hasAttribute("data-reset")) form.reset();
      const tmp = document.createElement("div");
      tmp.innerHTML = html;
      const warn = tmp.querySelector(".warn"), ok = tmp.querySelector(".ok");
      if (warn) toast(warn.textContent.trim(), "err");
      else if (ok) toast(ok.textContent.trim(), "ok");
    } catch (err) {
      toast("请求失败，服务可能没在运行：" + err, "err");
    } finally {
      clearInterval(timer);
      if (bar) bar.hidden = true;
      buttons.forEach(b => { if (b.isConnected) { b.disabled = false; b.textContent = b.dataset.label; } });
    }
  });
})();
