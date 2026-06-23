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
  const systemWelcomeHTML = messagesEl.innerHTML; // per "Nuova chat"

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
          session_id: sessionId,
        }),
      });

      if (!resp.ok) {
        const detail = await resp.text();
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
