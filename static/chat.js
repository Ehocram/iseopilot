// ============================================================
// ISEOPilot — logica chat lato client (vanilla JS)
// Streaming SSE + cronologia conversazioni (salvataggio/gestione) + feedback
// pollice su/giù. Rendering con escaping HTML (nessun innerHTML non sanificato).
// ============================================================
(function () {
  const I18N = window.I18N_FB || {};
  const messagesEl = document.getElementById("messages");
  const inputEl = document.getElementById("input");
  const sendBtn = document.getElementById("send");
  const engineSel = document.getElementById("engine");
  const toneSel = document.getElementById("tone");
  const langSel = document.getElementById("lang");
  const modeSel = document.getElementById("mode");

  const stState = document.getElementById("st-state");
  const stEngine = document.getElementById("st-engine");
  const stConn = document.getElementById("st-conn");
  const stAnon = document.getElementById("st-anon");
  const stAnonDot = document.getElementById("st-anon-dot");

  const newChatBtn = document.getElementById("new-chat");
  const chatListEl = document.getElementById("chat-list");
  const chatEmptyEl = document.getElementById("chat-empty");

  let history = [];      // {role, content}
  let sessionId = null;  // sessione corrente (null = nuova non ancora salvata)
  let attachments = [];  // {name, text, chars}
  const systemWelcomeHTML = messagesEl.innerHTML; // per "Nuova chat"

  // ── Allegati (fino a 20, drag&drop) ─────────────────────
  const attachBtn = document.getElementById("attach-btn");
  const attachInput = document.getElementById("attach-input");
  const attachBar = document.getElementById("attach-bar");
  const composerEl = document.getElementById("composer");

  function humanSize(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return Math.round(n / 1024) + " KB";
    return (n / 1024 / 1024).toFixed(1) + " MB";
  }
  function renderChips() {
    attachBar.innerHTML = "";
    if (!attachments.length) { attachBar.hidden = true; return; }
    attachBar.hidden = false;
    attachments.forEach(function (a, idx) {
      var chip = document.createElement("div");
      chip.className = "chip" + (a.error ? " err" : "");
      var label = document.createElement("span");
      label.className = "chip-name";
      label.textContent = "📄 " + a.name + (a.error ? " — " + a.error : " · " + (a.chars || 0) + " char");
      var x = document.createElement("button");
      x.className = "chip-x"; x.type = "button"; x.textContent = "×";
      x.addEventListener("click", function () { attachments.splice(idx, 1); renderChips(); });
      chip.appendChild(label); chip.appendChild(x);
      attachBar.appendChild(chip);
    });
  }
  async function handleFiles(fileList) {
    var files = Array.from(fileList || []);
    if (!files.length) return;
    if (attachments.length + files.length > 20) {
      var room = 20 - attachments.length;
      if (room <= 0) { alert(I18N.tooMany || "Massimo 20 allegati"); return; }
      files = files.slice(0, room);
    }
    // chip "in corso"
    var pending = files.map(function (f) {
      return { name: f.name, chars: 0, text: "", error: I18N.attaching || "…" };
    });
    var base = attachments.length;
    attachments = attachments.concat(pending); renderChips();
    var fd = new FormData();
    files.forEach(function (f) { fd.append("files", f); });
    try {
      var r = await fetch("/api/attach", { method: "POST", body: fd });
      var data = await r.json();
      (data.attachments || []).forEach(function (res, i) {
        var slot = base + i;
        if (res.ok) attachments[slot] = { name: res.name, text: res.text, chars: res.chars };
        else attachments[slot] = { name: res.name, error: res.error || (I18N.attachErr || "errore") };
      });
    } catch (e) {
      for (var i = 0; i < pending.length; i++) attachments[base + i] = { name: pending[i].name, error: I18N.attachErr };
    }
    renderChips();
  }
  if (attachBtn) attachBtn.addEventListener("click", function () { attachInput.click(); });
  if (attachInput) attachInput.addEventListener("change", function () { handleFiles(attachInput.files); attachInput.value = ""; });
  if (composerEl) {
    ["dragenter", "dragover"].forEach(function (ev) {
      composerEl.addEventListener(ev, function (e) { e.preventDefault(); composerEl.classList.add("drop"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      composerEl.addEventListener(ev, function (e) { e.preventDefault(); composerEl.classList.remove("drop"); });
    });
    composerEl.addEventListener("drop", function (e) {
      if (e.dataTransfer && e.dataTransfer.files) handleFiles(e.dataTransfer.files);
    });
  }

  // ── Barra di stato ──────────────────────────────────────
  function refreshStatus() {
    const isClaude = engineSel.value === "claude";
    stEngine.textContent = isClaude ? "Claude" : "LM Studio (locale)";
    if (isClaude) { stAnon.textContent = "attiva"; stAnonDot.className = "dot on"; }
    else { stAnon.textContent = "n/d (locale)"; stAnonDot.className = "dot off"; }
  }
  function refreshMode() {
    const free = modeSel && modeSel.value === "free";
    const el = document.getElementById("st-mode");
    if (el) el.textContent = free ? "AI libera" : "Documentale";
    // i flag "Dove cercare?" esistono solo in Documentale
    var picker = document.getElementById("src-picker");
    if (picker) picker.style.display = free ? "none" : "flex";
  }
  function selectedSource() {
    var r = document.querySelector('input[name="datasource"]:checked');
    return r ? r.value : "";
  }
  engineSel.addEventListener("change", refreshStatus);
  if (modeSel) modeSel.addEventListener("change", refreshMode);
  refreshStatus(); refreshMode();

  // ── Rendering messaggi ──────────────────────────────────
  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function renderMarkdownish(text) {
    let html = escapeHtml(text);
    html = html.replace(/```([\s\S]*?)```/g, function (_m, code) {
      return "<pre><code>" + code.replace(/^\n/, "") + "</code></pre>"; });
    html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    return html;
  }

  function addMessage(role, text) {
    const wrap = document.createElement("div");
    wrap.className = "msg " + role;
    const roleEl = document.createElement("div");
    roleEl.className = "role";
    roleEl.textContent = role === "user" ? (I18N.you || "Tu") : (I18N.assistant || "Assistente");
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML = renderMarkdownish(text);
    wrap.appendChild(roleEl);
    wrap.appendChild(bubble);
    messagesEl.appendChild(wrap);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return { wrap: wrap, bubble: bubble };
  }

  // Barra pollice su/giù su una risposta dell'assistente.
  function addFeedback(wrap, question, answer) {
    if (wrap.querySelector(".fb-bar")) return;
    const bar = document.createElement("div");
    bar.className = "fb-bar";
    const up = document.createElement("button");
    up.className = "fb-btn"; up.type = "button";
    up.title = I18N.good || "Risposta utile"; up.textContent = "👍";
    const down = document.createElement("button");
    down.className = "fb-btn"; down.type = "button";
    down.title = I18N.bad || "Risposta non utile"; down.textContent = "👎";
    const note = document.createElement("span");
    note.className = "fb-note";
    up.addEventListener("click", function () {
      sendFeedback("good", question, answer);
      up.classList.add("active"); down.classList.remove("active");
      note.textContent = I18N.saved || "Salvata come esempio";
    });
    down.addEventListener("click", function () {
      sendFeedback("bad", question, answer);
      down.classList.add("active"); up.classList.remove("active");
      note.textContent = I18N.noted || "Segnalazione registrata";
    });
    bar.appendChild(up); bar.appendChild(down); bar.appendChild(note);
    wrap.appendChild(bar);
  }

  // Sezione "Fonti" con link cliccabili sotto una risposta (come il desktop).
  function addSources(wrap, items) {
    if (!items || !items.length) return;
    var old = wrap.querySelector(".src-box");
    if (old) old.remove();
    var box = document.createElement("div");
    box.className = "src-box";
    var head = document.createElement("div");
    head.className = "src-head";
    head.textContent = (I18N.sources || "Fonti") + " (" + items.length + ")";
    box.appendChild(head);
    items.forEach(function (s) {
      var a = document.createElement("a");
      a.className = "src-link";
      a.href = s.url; a.target = "_blank"; a.rel = "noopener noreferrer";
      var icon = (s.kind === "report") ? "📊 " : (s.kind === "download") ? "⬇️ " : "📄 ";
      a.textContent = icon + (s.name || s.url);
      box.appendChild(a);
    });
    wrap.appendChild(box);
  }

  function sendFeedback(kind, question, answer) {
    fetch("/api/feedback/" + kind, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: question || "", answer: answer || "" }),
    }).catch(function () {});
  }

  function setBusy(busy) {
    sendBtn.disabled = busy;
    inputEl.disabled = busy;
    stState.textContent = busy ? "Generazione…" : "Pronto";
    stConn.className = busy ? "dot live" : "dot on";
  }

  // ── Invio messaggio + streaming ─────────────────────────
  async function send() {
    const text = inputEl.value.trim();
    if (!text) return;
    // In Documentale serve UNA fonte dati selezionata: popup e stop.
    if (modeSel && modeSel.value !== "free" && !selectedSource()) {
      alert(I18N.pickSource || "Seleziona una fonte dati (Conoscenza, Cartelle, OneDrive o Dynamics 365) prima di inviare, oppure passa alla modalità AI libera.");
      return;
    }

    addMessage("user", text);
    history.push({ role: "user", content: text });
    inputEl.value = "";
    inputEl.style.height = "auto";
    setBusy(true);

    const m = addMessage("assistant", "");
    let acc = "";

    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: history,
          engine: engineSel.value,
          tone: toneSel.value,
          reply_lang: langSel.value,
          free_mode: modeSel && modeSel.value === "free",
          source: selectedSource(),
          session_id: sessionId,
          attachments: attachments.filter(function(a){return a.text;})
                                  .map(function(a){return {name:a.name, text:a.text};}),
        }),
      });

      if (!resp.ok) {
        var detail = "";
        try { detail = await resp.text(); } catch (e2) {}
        var looksHtml = /^\s*</.test(detail) || detail.indexOf("<!DOCTYPE") !== -1 || detail.toLowerCase().indexOf("<html") !== -1;
        if (looksHtml) {
          // pagina di cortesia di un apparato di rete (proxy/VPN): mai mostrarla grezza
          detail = I18N.proxyErr || "risposta da un apparato di rete (proxy/VPN), non da ISEOPilot: la richiesta è stata interrotta prima di arrivare. Riprova; se persiste avvisa l'amministratore.";
        } else {
          detail = (detail || "").slice(0, 300);
        }
        m.bubble.innerHTML = renderMarkdownish("⚠️ Errore " + resp.status + ": " + detail);
        setBusy(false);
        return;
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      while (true) {
        const r = await reader.read();
        if (r.done) break;
        buf += decoder.decode(r.value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop();
        for (const part of parts) {
          const line = part.trim();
          if (!line.startsWith("data:")) continue;
          let evt;
          try { evt = JSON.parse(line.slice(5).trim()); } catch (e) { continue; }
          if (evt.type === "delta") {
            acc += evt.text;
            m.bubble.innerHTML = renderMarkdownish(acc);
            messagesEl.scrollTop = messagesEl.scrollHeight;
          } else if (evt.type === "sources") {
            addSources(m.wrap, evt.items);
            messagesEl.scrollTop = messagesEl.scrollHeight;
          } else if (evt.type === "error") {
            acc += "\n\n⚠️ " + evt.text;
            m.bubble.innerHTML = renderMarkdownish(acc);
          }
        }
      }
    } catch (e) {
      m.bubble.innerHTML = renderMarkdownish("⚠️ Errore di rete: " + e.message);
    }

    if (acc.trim()) {
      history.push({ role: "assistant", content: acc });
      addFeedback(m.wrap, text, acc);
      saveSession(); // salvataggio automatico dopo ogni risposta
    }
    setBusy(false);
    inputEl.focus();
  }

  // ── Cronologia conversazioni ────────────────────────────
  async function saveSession() {
    try {
      const r = await fetch("/api/chat/save", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, history: history }),
      });
      if (r.ok) {
        const data = await r.json();
        if (data.session_id) sessionId = data.session_id;
        loadList();
      }
    } catch (e) { /* salvataggio non bloccante */ }
  }

  async function loadList() {
    try {
      const r = await fetch("/api/chat/list");
      if (!r.ok) return;
      const data = await r.json();
      renderList(data.sessions || []);
    } catch (e) { /* lista non bloccante */ }
  }

  function renderList(sessions) {
    chatListEl.innerHTML = "";
    if (!sessions.length) {
      const e = document.createElement("div");
      e.className = "chat-empty";
      e.textContent = (chatEmptyEl && chatEmptyEl.textContent) || "Nessuna conversazione salvata";
      chatListEl.appendChild(e);
      return;
    }
    sessions.forEach(function (s) {
      const row = document.createElement("div");
      row.className = "chat-item" + (s.id === sessionId ? " active" : "");
      const t = document.createElement("button");
      t.className = "chat-item-title"; t.type = "button";
      t.textContent = s.title || "Chat";
      t.addEventListener("click", function () { selectChat(s.id); });
      const ren = document.createElement("button");
      ren.className = "chat-act"; ren.type = "button"; ren.title = "Rinomina"; ren.textContent = "✎";
      ren.addEventListener("click", function (ev) { ev.stopPropagation(); renameChat(s.id, s.title); });
      const del = document.createElement("button");
      del.className = "chat-act"; del.type = "button"; del.title = "Elimina"; del.textContent = "🗑";
      del.addEventListener("click", function (ev) { ev.stopPropagation(); deleteChat(s.id); });
      row.appendChild(t); row.appendChild(ren); row.appendChild(del);
      chatListEl.appendChild(row);
    });
  }

  async function selectChat(sid) {
    try {
      const r = await fetch("/api/chat/get?sid=" + encodeURIComponent(sid));
      if (!r.ok) return;
      const data = await r.json();
      sessionId = data.session_id;
      history = Array.isArray(data.history) ? data.history : [];
      messagesEl.innerHTML = systemWelcomeHTML;
      let lastUser = "";
      history.forEach(function (msg) {
        if (msg.role === "user" && !String(msg.content || "").startsWith("[CONTESTO")) {
          lastUser = msg.content;
          addMessage("user", msg.content);
        } else if (msg.role === "assistant") {
          const mm = addMessage("assistant", msg.content);
          addFeedback(mm.wrap, lastUser, msg.content);
        }
      });
      loadList();
      messagesEl.scrollTop = messagesEl.scrollHeight;
    } catch (e) { /* niente */ }
  }

  function newChat() {
    history = [];
    sessionId = null;
    attachments = [];
    renderChips();
    messagesEl.innerHTML = systemWelcomeHTML;
    loadList();
    inputEl.focus();
  }

  async function deleteChat(sid) {
    if (!window.confirm(I18N.confirmDel || "Eliminare questa conversazione?")) return;
    try {
      await fetch("/api/chat/delete", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sid }),
      });
      if (sid === sessionId) newChat();
      else loadList();
    } catch (e) { /* niente */ }
  }

  async function renameChat(sid, current) {
    const title = window.prompt(I18N.renamePrompt || "Nuovo titolo:", current || "");
    if (title === null) return;
    try {
      await fetch("/api/chat/rename", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sid, title: title }),
      });
      loadList();
    } catch (e) { /* niente */ }
  }

  if (newChatBtn) newChatBtn.addEventListener("click", newChat);

  // ── Input ───────────────────────────────────────────────
  sendBtn.addEventListener("click", send);
  inputEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  inputEl.addEventListener("input", function () {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + "px";
  });

  // Carica la cronologia all'avvio
  loadList();
})();
