/* Croot v2 — chat shell.
 * Holds the conversation client-side and resends it each turn, so /api/chat
 * stays stateless. Intake is stubbed server-side until ANTHROPIC_API_KEY is
 * wired; this surfaces the 501/503 gracefully for now.
 */
(function () {
  "use strict";

  const messagesEl = document.getElementById("messages");
  const form = document.getElementById("composer");
  const input = document.getElementById("input");
  const criteriaEl = document.getElementById("criteria");

  /** @type {{role: string, content: string}[]} */
  const conversation = [];

  const URL_RE = /^https?:\/\/\S+$/i;

  function addMessage(role, text) {
    const div = document.createElement("div");
    div.className = "msg " + role;
    div.textContent = text;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function renderCriteria(criteria) {
    const entries = Object.entries(criteria).filter(([, v]) => {
      if (Array.isArray(v)) return v.length;
      if (v && typeof v === "object") return Object.keys(v).length;
      return v !== "" && v !== null && v !== false;
    });
    if (!entries.length) return;
    criteriaEl.innerHTML = "<h2>Criteria</h2>";
    const dl = document.createElement("dl");
    for (const [k, v] of entries) {
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      dd.textContent = Array.isArray(v) ? v.join(", ") : JSON.stringify(v);
      dl.append(dt, dd);
    }
    criteriaEl.appendChild(dl);
  }

  async function send(text) {
    conversation.push({ role: "user", content: text });
    addMessage("user", text);

    const payload = { messages: conversation };
    if (URL_RE.test(text.trim())) payload.url = text.trim();

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) {
        addMessage("assistant", data.error || "Something went wrong.");
        return;
      }
      conversation.push({ role: "assistant", content: data.reply });
      addMessage("assistant", data.reply);
      if (data.criteria) renderCriteria(data.criteria);
    } catch (err) {
      addMessage("assistant", "Network error — please try again.");
    }
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    send(text);
  });
})();
