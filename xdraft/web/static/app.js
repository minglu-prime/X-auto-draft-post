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

  // ---- motion: one language, read from the CSS tokens ----------------------------------------
  // Entering: fade + rise. Leaving: fade + slight shrink. Moving: FLIP (animate from the old place
  // to the new one with transform only). Every animation can be interrupted by the next change:
  // FLIP measures where things *look* right now, so a new move starts from mid-flight.
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const css = getComputedStyle(document.documentElement);
  const ms = name => parseFloat(css.getPropertyValue(name)) || 0;
  const EASE_OUT = "cubic-bezier(.2,.8,.2,1)", EASE_IN = "cubic-bezier(.4,0,1,1)";

  function flip(els, mutate) {
    const list = els.filter(Boolean);
    if (reduced.matches || !list.length) { mutate(); return; }
    const before = list.map(e => e.getBoundingClientRect());
    list.forEach(e => e.getAnimations().forEach(a => { if (a.id === "flip") a.cancel(); }));
    mutate();
    list.forEach((e, i) => {
      if (!e.isConnected) return;
      const now = e.getBoundingClientRect();
      const dx = before[i].left - now.left, dy = before[i].top - now.top;
      if (Math.abs(dx) < 1 && Math.abs(dy) < 1) return;
      if (Math.abs(dy) > innerHeight * 1.5) return;  // far away: moving it would only be noise
      const a = e.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }],
        { duration: ms("--t-move"), easing: EASE_OUT });
      a.id = "flip";
    });
  }

  function enter(el) {
    if (!el || reduced.matches) return;
    el.classList.remove("entering");
    void el.offsetWidth;
    el.classList.add("entering");
    el.addEventListener("animationend", () => el.classList.remove("entering"), { once: true });
  }

  function leave(el) {
    if (!el || reduced.matches || !el.getClientRects().length) return Promise.resolve();
    return el.animate([{ opacity: 1, transform: "none" }, { opacity: 0, transform: "scale(.98)" }],
      { duration: ms("--t-fast"), easing: EASE_IN, fill: "forwards" }).finished.catch(() => {});
  }

  // What moves when `el` changes size or disappears: its siblings in the same grid, the following
  // topic groups in its section, and the sections after it (never an element and its ancestor).
  function movers(el) {
    const out = [];
    const grid = el.parentElement;
    if (grid) out.push(...[...grid.children].filter(c => c !== el));
    const group = el.closest(".topic, .more-topics");
    if (group) { let n = group.nextElementSibling; while (n) { out.push(n); n = n.nextElementSibling; } }
    const block = el.closest(".block");
    if (block) { let n = block.nextElementSibling; while (n) { out.push(n); n = n.nextElementSibling; } }
    return out;
  }
  // Everything after `el` inside its card, plus what moves when that card changes height.
  function following(el) {
    const out = [];
    for (let n = el.nextElementSibling; n; n = n.nextElementSibling) out.push(n);
    const host = el.closest("[data-swap]");
    return host ? [...out, ...movers(host)] : out;
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
    let closing = false;
    const close = () => {
      if (closing) return;
      closing = true;
      const remove = () => flip([...box.children].filter(c => c !== el), () => el.remove());
      if (reduced.matches) { remove(); return; }
      el.classList.add("leaving");
      el.addEventListener("animationend", remove, { once: true });
    };
    if (opts.action) {
      const b = document.createElement("button");
      b.className = "toast-action";
      b.textContent = opts.action.label;
      b.onclick = () => { if (closing) return; close(); opts.action.run(); };  // runs once, even on double-click
      el.appendChild(b);
    }
    const x = document.createElement("button");
    x.className = "link toast-close";
    x.textContent = "×";
    x.setAttribute("aria-label", "关闭");
    x.onclick = close;
    el.appendChild(x);
    box.appendChild(el);
    el.dismiss = close;
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
  // A card that goes away fades out and the rest slide up; one that comes back (undo) rises in;
  // one updated in place stays put while only its new parts (e.g. a new image) fade in.
  async function swapIn(target, html) {
    if (!target) return null;
    if (!target.hasAttribute("data-swap")) {
      target.innerHTML = html;
      enter(target);
      announce(target);
      return target;
    }
    const frag = parse(html);
    const node = frag.firstElementChild;
    if (!node) { target.remove(); return null; }
    const wasHidden = target.hidden, goesHidden = node.hidden;
    const oldImgs = new Set([...target.querySelectorAll("img")].map(i => i.getAttribute("src")));
    const newImgs = new Set([...node.querySelectorAll("img")].map(i => i.getAttribute("src")));
    const caret = caretOf(target);
    if (goesHidden && !wasHidden) await leave(target);
    else await Promise.all([...target.querySelectorAll("img")]  // removed images fade out first
      .filter(i => !newImgs.has(i.getAttribute("src"))).map(i => leave(i.closest("figure, a") || i)));
    flip(movers(target), () => target.replaceWith(node));
    restoreCaret(node, caret);
    if (wasHidden && !goesHidden) enter(node);
    else node.querySelectorAll("img").forEach(i => { if (!oldImgs.has(i.getAttribute("src"))) enter(i.closest("figure, a") || i); });
    announce(node);
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
      if (el) await swapIn(el, html);
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
  // Show a page's fresh state without a full reload: fetch it, swap header + main inside a view
  // transition (the step bar and header stay still, content cross-fades), then glide to #section.
  let refreshGen = 0;  // only the latest refresh may replace the page
  async function refresh(u) {
    const gen = ++refreshGen;
    try {
      // Pending autosaves would be lost with the old cards: finish them first.
      const saved = await Promise.all([...document.querySelectorAll(".draft")].map(flush));
      if (!saved.every(Boolean)) { toast("有修改没保存上，先没刷新页面", "err"); return; }
      const resp = await fetch(u.pathname + u.search, { headers: H });
      if (!resp.ok) throw new Error(resp.statusText);
      const doc = new DOMParser().parseFromString(await resp.text(), "text/html");
      if (gen !== refreshGen) return;
      const apply = () => {
        document.querySelector("header.top").replaceWith(doc.querySelector("header.top"));
        document.querySelector("main").replaceWith(doc.querySelector("main"));
        document.title = doc.title;
        const same = u.pathname + u.search === location.pathname + location.search;
        history[same ? "replaceState" : "pushState"](null, "", u.href);
      };
      if (document.startViewTransition && !reduced.matches) {
        await document.startViewTransition(apply).updateCallbackDone;
      } else {
        apply();
      }
      const main = document.querySelector("main");
      flashes(main);
      announce(main);
      knownDue = document.querySelectorAll("#due .draft").length;
      baseTitle = document.title;
      const target = u.hash && document.getElementById(decodeURIComponent(u.hash.slice(1)));
      if (target) target.scrollIntoView({ behavior: reduced.matches ? "auto" : "smooth", block: "start" });
    } catch (err) {
      toast("页面刷新失败：" + err.message, "err", { action: { label: "重新加载", run: () => location.reload() } });
    }
  }
  window.addEventListener("popstate", () => location.reload());

  function go(url) {
    const u = new URL(url, location.href);
    if (typing()) {
      toast("有新内容", "ok", { action: { label: "刷新", run: () => refresh(u) } });
    } else {
      refresh(u);
    }
  }

  // ---- autosave: one box per tweet, saved 800 ms after typing stops -------------------------
  const timers = new WeakMap();
  const inflight = new WeakMap();

  function blocks(editor) { return [...editor.querySelectorAll(".tweet-block")]; }

  function setState(card, text, kind) {
    const s = card.querySelector(".save-state");
    if (!s || s.textContent === text) return;
    s.textContent = text;
    s.dataset.kind = kind || "";
    if (!reduced.matches) s.animate([{ opacity: .35 }, { opacity: 1 }], { duration: 90, easing: EASE_OUT });
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
  // `before` (optional) is a change still animating out, e.g. a tweet being deleted: the save, and
  // so any flush(), waits for it.
  function save(card, before) {
    clearTimeout(timers.get(card));
    timers.delete(card);
    const prev = inflight.get(card) || Promise.resolve(true);
    const job = prev.then(() => before).then(() => doSave(card));
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
        await swapIn(document.getElementById(`draft-${id}`), html);
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
      const editor = add.closest(".editor"), card = editor.closest(".draft");
      const b = newBlock();
      flip([add, ...movers(card)], () => add.before(b));
      enter(b);
      renumber(editor);
      recount(card);
      b.querySelector("textarea").focus({ preventScroll: true });
      return;
    }
    const del = e.target.closest(".tweet-del");
    if (del && !del.disabled) {
      const editor = del.closest(".editor"), card = del.closest(".draft");
      const block = del.closest(".tweet-block");
      const at = blocks(editor).indexOf(block);
      const text = block.querySelector("textarea").value;
      const after = [];
      for (let n = block.nextElementSibling; n; n = n.nextElementSibling) after.push(n);
      del.disabled = true;
      // Registered now, so an action clicked during the fade-out waits for the deletion to be saved.
      save(card, leave(block).then(() => {
        flip([...after, ...movers(card)], () => block.remove());
        renumber(editor);
        recount(card);
      }));
      if (text.trim()) {
        toast(`已删掉第 ${at + 1} 条`, "ok", { action: { label: "撤销", run: () => {
          const back = newBlock(text);
          const list = blocks(editor);
          const anchor = list[at] || editor.querySelector(".add-tweet");
          const shifted = [];
          for (let n = anchor; n; n = n.nextElementSibling) shifted.push(n);
          flip([...shifted, ...movers(card)], () => anchor.before(back));
          enter(back);
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
  let busyCount = 0;  // overlapping requests: the bar stays until the last one finishes
  function busy(on) {
    busyCount = Math.max(0, busyCount + (on ? 1 : -1));
    const bar = document.getElementById("busy-bar");
    if (bar) bar.hidden = busyCount === 0;
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
    // The clicked button keeps its exact size: it shows a short status + seconds; the longer
    // explanation ("usually 1–2 minutes") appears as a line under it instead of widening it.
    buttons.forEach(b => {
      b.disabled = true;
      b.dataset.label = b.textContent;
      if (b === clicked) { b.style.width = b.offsetWidth + "px"; b.style.whiteSpace = "nowrap"; b.setAttribute("aria-busy", "true"); }
    });
    const locked = card ? [...card.querySelectorAll("textarea")] : [];
    locked.forEach(t => { t.readOnly = true; });
    const [short, ...rest] = ((clicked && clicked.dataset.busy) || "").split("，");
    let hint = null;
    if (clicked && rest.length) {
      hint = document.createElement("p");
      hint.className = "busy-hint muted small";
      hint.setAttribute("role", "status");
      hint.textContent = `${short}，${rest.join("，")}`;
      const anchor = clicked.closest(".row, .next, form") || clicked.parentElement;
      flip(following(anchor), () => anchor.after(hint));  // what's below slides down, never jumps
      enter(hint);
    }
    const started = Date.now();
    const tick = () => {
      if (!clicked) return;
      const secs = Math.round((Date.now() - started) / 1000);
      clicked.textContent = short ? (secs ? `${short} ${secs}s` : `${short}…`) : "处理中…";
    };
    tick();
    const timer = short ? setInterval(tick, 1000) : null;
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
        if (li) {
          const after = [];
          for (let n = li.nextElementSibling; n; n = n.nextElementSibling) after.push(n);
          await leave(li);
          flip(after, () => li.remove());
        }
      } else if (target) {
        await swapIn(target, html);
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
      if (hint) leave(hint).then(() => flip(following(hint), () => hint.remove()));
      locked.forEach(t => { if (t.isConnected) t.readOnly = false; });
      buttons.forEach(b => {
        if (!b.isConnected) return;
        b.disabled = false;
        b.textContent = b.dataset.label;
        b.style.width = b.style.whiteSpace = "";
        b.removeAttribute("aria-busy");
      });
      if (card && card.isConnected) recount(card);
    }
  });

  // ---- page load --------------------------------------------------------------------------------
  // One feedback channel: one-shot success messages become toasts; problems stay on the page.
  function flashes(root) {
    root.querySelectorAll(".flash").forEach(f => {
      f.querySelectorAll(":scope > p.ok").forEach(p => { toast(p.textContent.trim()); p.remove(); });
      if (!f.textContent.trim()) f.remove();
    });
  }
  flashes(document);
  announce(document);

  // Leaving with unsaved text: save it, and let the browser warn if that cannot finish.
  window.addEventListener("beforeunload", e => {
    const pending = [...document.querySelectorAll(".draft")].some(c => timers.has(c) || inflight.has(c));
    if (pending) { document.querySelectorAll(".draft").forEach(c => timers.has(c) && save(c)); e.preventDefault(); }
  });

  // A post may come due while the page is open: say so without interrupting.
  let baseTitle = document.title;
  let knownDue = document.querySelectorAll("#due .draft").length;
  let offline = null;
  async function poll() {
    if (document.hidden) return;
    try {
      const resp = await fetch("/status", { headers: H });
      if (!resp.ok) throw new Error(resp.statusText);
      const s = await resp.json();
      if (offline) { offline.dismiss(); offline = null; toast("已重新连上 xdraft"); }
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
