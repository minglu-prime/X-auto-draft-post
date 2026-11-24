// Progressive enhancement for the dashboard. Forms marked data-post submit via fetch with the
// X-Xdraft header (required by the server) and swap the returned HTML into the page.
(function () {
  "use strict";
  const H = { "X-Xdraft": "1" };
  const MAX = 280;

  // ---- X weighted length (mirrors pipeline/check.py) ---------------------------------------
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

  // ---- toasts: success fades out, errors stay; optional undo / action button --------------
  function toast(text, kind = "ok", opts = {}) {
    const box = document.getElementById("toasts");
    if (!box || !text) return null;
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.setAttribute("role", kind === "err" ? "alert" : "status");
    const msg = document.createElement("span");
    msg.textContent = text;
    el.appendChild(msg);
    const close = () => el.remove();
    if (opts.action) {
      const b = document.createElement("button");
      b.className = "toast-action";
      b.textContent = opts.action.label;
      b.onclick = () => { close(); opts.action.run(); };
      el.appendChild(b);
    }
    const x = document.createElement("button");
    x.className = "link toast-close";
    x.textContent = "×";
    x.setAttribute("aria-label", "关闭");
    x.onclick = close;
    el.appendChild(x);
    box.appendChild(el);
    if (kind === "ok") setTimeout(close, opts.action ? 8000 : 4000);
    return el;
  }

  // ---- swapping server HTML ------------------------------------------------------------------
  function parse(html) {
    const t = document.createElement("template");
    t.innerHTML = html.trim();
    return t.content;
  }

  // Keep the caret where it was when a card is re-rendered (e.g. after pasting an image).
  function caretOf(root) {
    const a = document.activeElement;
    if (!root || !a || !root.contains(a) || a.tagName !== "TEXTAREA") return null;
    const all = [...root.querySelectorAll("textarea")];
    return { i: all.indexOf(a), s: a.selectionStart, e: a.selectionEnd, top: a.scrollTop };
  }
  function restoreCaret(root, c) {
    if (!root || !c) return;
    const ta = root.querySelectorAll("textarea")[c.i];
    if (!ta) return;
    ta.focus({ preventScroll: true });
    ta.setSelectionRange(c.s, c.e);
    ta.scrollTop = c.top;
  }

  // Replace `target` (an element with data-swap) or fill it, then surface any server message.
  function swapIn(target, html) {
    if (!target) return null;
    let node = target;
    if (target.hasAttribute("data-swap")) {
      const frag = parse(html);
      const first = frag.firstElementChild;
      const caret = caretOf(target);
      target.replaceWith(frag);
      node = first || null;
      restoreCaret(node, caret);
    } else {
      target.innerHTML = html;
    }
    if (node) announce(node);
    return node;
  }

  // Server messages travel as hidden [data-toast] elements; show them once as toasts.
  function announce(root) {
    root.querySelectorAll("[data-toast]").forEach(el => {
      const undo = el.dataset.undo;
      const owner = el.closest("[id]");
      toast(el.textContent.trim(), el.classList.contains("warn") ? "err" : "ok",
        undo ? { action: { label: "撤销", run: () => runUndo(undo, owner && owner.id) } } : {});
      el.removeAttribute("data-toast");
    });
    initCards(root);
  }

  async function runUndo(url, id) {
    try {
      const resp = await fetch(url, { method: "POST", headers: H });
      const html = await resp.text();
      if (!resp.ok) throw new Error(textOf(html) || resp.statusText);
      const el = id && document.getElementById(id);
      if (el) swapIn(el, html);
      else go(location.href);
    } catch (err) {
      toast("撤销失败：" + err.message, "err");
    }
  }

  function textOf(html) {
    const d = document.createElement("div");
    d.innerHTML = html;
    return d.textContent.trim();
  }

  // ---- navigation that never interrupts typing ----------------------------------------------
  function typing() {
    const a = document.activeElement;
    return a && (a.tagName === "TEXTAREA" || (a.tagName === "INPUT" && /^(text|url|search|)$/.test(a.type)))
      && a.value.trim() !== "";
  }
  function go(url) {
    const u = new URL(url, location.href);
    const reload = () => {
      if (u.pathname === location.pathname && u.search === location.search) {
        history.scrollRestoration = "manual";  // land on the #section, not the old scroll position
        location.hash = u.hash;
        location.reload();
      } else {
        location.href = u.href;
      }
    };
    if (typing()) {
      toast("有新内容", "ok", { action: { label: "刷新", run: reload } });
    } else {
      reload();
    }
  }

  // ---- autosave: one box per tweet, saved 800 ms after typing stops -------------------------
  const timers = new WeakMap();
  const inflight = new WeakMap();

  function blocks(editor) { return [...editor.querySelectorAll(".tweet-block")]; }

  function setState(card, text, kind) {
    const s = card.querySelector(".save-state");
    if (s) { s.textContent = text; s.dataset.kind = kind || ""; }
  }

  function renumber(editor) {
    const list = blocks(editor);
    list.forEach((b, i) => {
      const num = b.querySelector(".num");
      if (num) num.textContent = `${i + 1}/${list.length}`;
      const ta = b.querySelector("textarea");
      if (ta) ta.setAttribute("aria-label", `第 ${i + 1} 条推文`);
      b.querySelectorAll('input[name="tweet"]').forEach(inp => { inp.value = i + 1; });
      const del = b.querySelector(".tweet-del");
      if (del) del.hidden = list.length < 2;
    });
  }

  function recount(card) {
    let over = false;
    card.querySelectorAll(".tweet-block").forEach(b => {
      const ta = b.querySelector("textarea"), out = b.querySelector(".count");
      if (!ta || !out) return;
      const n = weighted(ta.value.trim());
      out.textContent = `${n}/${MAX}`;
      out.classList.toggle("over", n > MAX);
      over = over || n > MAX;
    });
    const approve = card.querySelector("[data-approve]");
    if (approve && approve.dataset.slot === "1") {
      approve.disabled = over;
      approve.title = over ? "有推文超长，改短后才能排期" : "";
    }
  }

  function scheduleSave(card) {
    clearTimeout(timers.get(card));
    setState(card, "未保存", "");
    timers.set(card, setTimeout(() => save(card), 800));
  }

  // Saves for one card run strictly one after another (each reads the boxes when it starts), and the
  // queue is registered at once, so flush() never misses a save that is still waiting its turn.
  function save(card) {
    clearTimeout(timers.get(card));
    timers.delete(card);
    const prev = inflight.get(card) || Promise.resolve(true);
    const job = prev.then(() => doSave(card));
    inflight.set(card, job);
    job.finally(() => { if (inflight.get(card) === job) inflight.delete(card); });
    return job;
  }

  async function doSave(card) {
    const editor = card.querySelector(".editor");
    if (!editor || !card.isConnected) return true;
    const body = new FormData();
    blocks(editor).forEach(b => {
      body.append("tweet", b.querySelector("textarea").value);
      body.append("src", b.dataset.src ?? "-1");
    });
    {
      setState(card, "保存中…", "");
      try {
        const resp = await fetch(editor.dataset.autosave, {
          method: "POST", body, headers: { ...H, "X-Autosave": "1" },
        });
        const html = await resp.text();
        if (!resp.ok) throw new Error(textOf(html) || resp.statusText);
        const meta = card.querySelector(".meta");
        const fresh = parse(html);
        const refused = fresh.querySelector('[role="alert"]');
        if (meta) meta.replaceWith(fresh);
        if (refused) { setState(card, "没有保存", "err"); return false; }
        // The server now stores the tweets in on-screen order (empty boxes dropped).
        let i = 0;
        blocks(editor).forEach(b => { b.dataset.src = b.querySelector("textarea").value.trim() ? i++ : -1; });
        setState(card, "已保存", "ok");
        return true;
      } catch (err) {
        setState(card, "保存失败", "err");
        toast("自动保存失败：" + err.message, "err");
        return false;
      }
    }
  }

  // Save pending edits first, so no other action works on stale text.
  async function flush(card) {
    if (!card) return true;
    let ok = true;
    while (timers.has(card) || inflight.has(card)) {
      ok = await (timers.has(card) ? save(card) : inflight.get(card));
    }
    return ok;
  }

  function newBlock(text = "") {
    const b = document.createElement("div");
    b.className = "tweet-block";
    b.dataset.src = "-1";
    b.innerHTML = `<div class="tweet-head"><span class="num"></span><span class="count">0/${MAX}</span>`
      + `<button type="button" class="link tweet-del">删掉这条</button></div>`
      + `<textarea name="tweet" rows="2" placeholder="写这一条。粘贴或拖进截图就能配图。"></textarea>`;
    b.querySelector("textarea").value = text;
    return b;
  }

  // ---- pasting / dropping screenshots attaches them to that tweet ------------------------------
  async function uploadImages(ta, files) {
    const card = ta.closest(".draft"), editor = ta.closest(".editor");
    const imgs = [...files].filter(f => f.type.startsWith("image/"));
    if (!card || !editor || !imgs.length) return false;
    if (!ta.value.trim()) { toast("先写这一条的文字，再配图", "err"); return true; }
    if (!(await flush(card))) return true;
    const index = blocks(editor).indexOf(ta.closest(".tweet-block")) + 1;
    const id = card.dataset.draft;
    for (const file of imgs) {
      const body = new FormData();
      body.append("tweet", index);
      body.append("file", file, file.name || "paste.png");
      busy(true);
      try {
        const resp = await fetch(`/drafts/${id}/images`, { method: "POST", body, headers: H });
        const html = await resp.text();
        if (!resp.ok) throw new Error(textOf(html) || resp.statusText);
        swapIn(document.getElementById(`draft-${id}`), html);
      } catch (err) {
        toast("配图失败：" + err.message, "err");
      } finally {
        busy(false);
      }
    }
    return true;
  }

  // ---- per-card wiring (runs on load and on every swapped-in card) ----------------------------
  function initCards(root) {
    const cards = root.matches && root.matches(".draft") ? [root] : [...root.querySelectorAll(".draft")];
    cards.forEach(card => {
      const editor = card.querySelector(".editor");
      if (editor) { renumber(editor); recount(card); }
    });
  }

  document.addEventListener("input", e => {
    const ta = e.target.closest(".editor textarea");
    if (!ta) return;
    const card = ta.closest(".draft");
    recount(card);
    scheduleSave(card);
  });

  document.addEventListener("click", e => {
    const add = e.target.closest(".add-tweet");
    if (add) {
      const editor = add.closest(".editor");
      const b = newBlock();
      add.before(b);
      renumber(editor);
      recount(editor.closest(".draft"));
      b.querySelector("textarea").focus();
      return;
    }
    const del = e.target.closest(".tweet-del");
    if (del && !del.disabled) {
      const editor = del.closest(".editor"), card = del.closest(".draft");
      const block = del.closest(".tweet-block");
      const at = blocks(editor).indexOf(block);
      const text = block.querySelector("textarea").value;
      block.remove();
      renumber(editor);
      recount(card);
      save(card);
      if (text.trim()) {
        toast(`已删掉第 ${at + 1} 条`, "ok", { action: { label: "撤销", run: () => {
          const back = newBlock(text);
          const list = blocks(editor);
          (list[at] ? list[at].before(back) : editor.querySelector(".add-tweet").before(back));
          renumber(editor);
          recount(card);
          save(card);
        } } });
      }
      return;
    }
    const copy = e.target.closest("[data-copy]");
    if (copy) copyText(copy);
  });

  // Clipboard API first; some embedded browsers deny it, so fall back to a selection copy, and if
  // even that is refused, leave the text selected so Ctrl+C works.
  async function copyText(btn) {
    const text = btn.dataset.copy;
    let ok = false;
    try {
      await navigator.clipboard.writeText(text);
      ok = true;
    } catch (_) {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.cssText = "position:fixed;top:0;left:0;opacity:0;pointer-events:none";
      document.body.appendChild(ta);
      ta.select();
      try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
      ta.remove();
    }
    if (ok) {
      btn.classList.add("copied");
      btn.textContent = "已复制";
      setTimeout(() => { btn.classList.remove("copied"); btn.textContent = "复制"; }, 1200);
      return;
    }
    const p = btn.closest(".post") && btn.closest(".post").querySelector(".post-text");
    if (p) { const r = document.createRange(); r.selectNodeContents(p); getSelection().removeAllRanges(); getSelection().addRange(r); }
    toast("浏览器不让网页写剪贴板，已选中这条，按 Ctrl+C 复制", "err");
  }

  document.addEventListener("change", e => {
    if (e.target.matches("input[data-autosubmit]") && e.target.files.length) e.target.form.requestSubmit();
  });

  document.addEventListener("paste", e => {
    const ta = e.target.closest && e.target.closest(".editor textarea");
    if (!ta || !e.clipboardData) return;
    const files = [...e.clipboardData.files];
    if (files.some(f => f.type.startsWith("image/"))) {
      e.preventDefault();
      uploadImages(ta, files);
    }
  });
  document.addEventListener("dragover", e => {
    const ta = e.target.closest && e.target.closest(".editor textarea");
    if (ta && [...e.dataTransfer.items].some(i => i.kind === "file")) {
      e.preventDefault();
      ta.classList.add("drop");
    }
  });
  document.addEventListener("dragleave", e => {
    const ta = e.target.closest && e.target.closest(".editor textarea");
    if (ta) ta.classList.remove("drop");
  });
  document.addEventListener("drop", e => {
    const ta = e.target.closest && e.target.closest(".editor textarea");
    if (!ta) return;
    ta.classList.remove("drop");
    if ([...e.dataTransfer.files].some(f => f.type.startsWith("image/"))) {
      e.preventDefault();
      uploadImages(ta, e.dataTransfer.files);
    }
  });

  // ---- form submission ------------------------------------------------------------------------
  function busy(on) {
    const bar = document.getElementById("busy-bar");
    if (bar) bar.hidden = !on;
  }

  document.addEventListener("submit", async e => {
    const form = e.target.closest("form[data-post]");
    if (!form) return;
    e.preventDefault();
    if (form.dataset.confirm && !confirm(form.dataset.confirm)) return;
    const card = form.closest(".draft");
    const submitter = e.submitter;
    if (card && !(await flush(card))) return;
    const action = (submitter && submitter.getAttribute("formaction")) || form.action;
    const body = new FormData(form, submitter);

    // Busy: every button tied to this form is disabled (width kept, so nothing jumps); the clicked
    // one shows elapsed seconds; the card's text boxes are read-only until the answer arrives.
    const buttons = [...form.querySelectorAll("button"), ...document.querySelectorAll(`button[form="${form.id}"]`)];
    const clicked = submitter || form.querySelector("button");
    buttons.forEach(b => { b.disabled = true; b.dataset.label = b.textContent; b.style.minWidth = b.offsetWidth + "px"; });
    const locked = card ? [...card.querySelectorAll("textarea")] : [];
    locked.forEach(t => { t.readOnly = true; });
    const busyText = clicked && clicked.dataset.busy;
    const started = Date.now();
    const tick = () => {
      if (clicked) clicked.textContent = busyText
        ? `${busyText} · ${Math.round((Date.now() - started) / 1000)}s` : "处理中…";
    };
    tick();
    const timer = busyText ? setInterval(tick, 1000) : null;
    busy(true);
    try {
      const resp = await fetch(action, { method: "POST", body, headers: H });
      const redirect = resp.headers.get("X-Redirect");
      if (redirect) { go(redirect); return; }
      const html = resp.status === 204 ? "" : await resp.text();
      if (!resp.ok) { toast(`操作失败：${textOf(html) || resp.statusText}`, "err"); return; }
      const target = form.dataset.target ? document.querySelector(form.dataset.target) : form.closest("[data-swap]");
      if (form.closest(".skipped")) {  // restoring from History: the draft goes back to the workbench
        const li = form.closest("li");
        announce(parse(html).firstElementChild || document.createElement("div"));
        if (li) li.remove();
      } else if (target) {
        swapIn(target, html);
        if (!target.hasAttribute("data-swap")) {
          target.querySelectorAll("p.ok:not([data-toast])").forEach(p => { toast(p.textContent.trim()); });
        }
      }
      if (form.hasAttribute("data-reset")) form.reset();
    } catch (err) {
      toast("连接本地服务失败：" + err.message, "err");
    } finally {
      if (timer) clearInterval(timer);
      busy(false);
      locked.forEach(t => { if (t.isConnected) t.readOnly = false; });
      buttons.forEach(b => {
        if (b.isConnected) { b.disabled = false; b.textContent = b.dataset.label; b.style.minWidth = ""; }
      });
      if (card && card.isConnected) recount(card);
    }
  });

  // ---- page load --------------------------------------------------------------------------------
  // One feedback channel: one-shot success messages become toasts; problems stay on the page.
  document.querySelectorAll(".flash").forEach(f => {
    f.querySelectorAll(":scope > p.ok").forEach(p => { toast(p.textContent.trim()); p.remove(); });
    if (!f.textContent.trim()) f.remove();
  });
  announce(document);

  // Leaving with unsaved text: save it, and let the browser warn if that cannot finish.
  window.addEventListener("beforeunload", e => {
    const pending = [...document.querySelectorAll(".draft")].some(c => timers.has(c) || inflight.has(c));
    if (pending) { document.querySelectorAll(".draft").forEach(c => timers.has(c) && save(c)); e.preventDefault(); }
  });

  // A post may come due while the page is open: say so without interrupting.
  const baseTitle = document.title;
  let knownDue = document.querySelectorAll("#due .draft").length;
  let offline = null;
  async function poll() {
    if (document.hidden) return;
    try {
      const resp = await fetch("/status", { headers: H });
      if (!resp.ok) throw new Error(resp.statusText);
      const s = await resp.json();
      if (offline) { offline.remove(); offline = null; }
      document.title = s.due ? `(${s.due}) 该发了 · ${baseTitle}` : baseTitle;
      if (s.due > knownDue) {
        toast(`有 ${s.due} 篇该发了`, "ok", { action: { label: "查看", run: () => go("/#due") } });
      }
      knownDue = s.due;
    } catch (err) {
      if (!offline) offline = toast("连不上本地的 xdraft，提醒和保存都会暂停：" + err.message, "err");
    }
  }
  setInterval(poll, 60000);
  document.addEventListener("visibilitychange", poll);
})();
