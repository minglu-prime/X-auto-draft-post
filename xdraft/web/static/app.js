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
    const buttons = form.querySelectorAll("button");
    buttons.forEach(b => { b.disabled = true; b.dataset.label = b.textContent; b.textContent = "Working…"; });
    try {
      const resp = await fetch(form.action, {
        method: "POST", body: new FormData(form, e.submitter), headers: { "X-Xdraft": "1" },
      });
      const redirect = resp.headers.get("X-Redirect");
      if (redirect) { location.href = redirect; return; }
      const html = resp.status === 204 ? "" : await resp.text();
      const target = form.dataset.target ? document.querySelector(form.dataset.target) : form.closest("[data-swap]");
      if (!resp.ok) { alert(html || resp.statusText); return; }
      if (target && target.hasAttribute("data-swap")) target.outerHTML = html;
      else if (target) target.innerHTML = html;
      if (form.hasAttribute("data-reset")) form.reset();
    } catch (err) {
      alert("Request failed: " + err);
    } finally {
      buttons.forEach(b => { if (b.isConnected) { b.disabled = false; b.textContent = b.dataset.label; } });
    }
  });
})();
