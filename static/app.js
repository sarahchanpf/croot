/* Croot v2 — Candidate Finder UI.
 *
 * Hybrid intake: the form's "Execute Search" sends describe + JD + notes to
 * /api/chat once. If the model says ready_to_search, we search immediately;
 * otherwise we show the extracted criteria and let the user add to Extra terms
 * before pressing Execute Search again.
 * Advanced Search builds criteria directly from form fields (no LLM) and is the
 * fallback whenever the conversational extraction is unavailable.
 *
 * The working criteria object follows the backend Criteria contract and is
 * shared between the chat flow and the Advanced modal.
 */
(function () {
  "use strict";

  // ---- shared state ----
  const state = {
    conversation: [],   // [{role, content}]
    criteria: {},       // Criteria-shaped working object
    jdText: "",         // extracted JD text (file/link)
    results: [],        // last ranked candidates
    accessPassword: "",
    criteriaSummaryText: "",
    lastParsedBriefKey: "",
    searchLimit: 5,
    searchesUsed: 0,
    searchesRemaining: 5,
    joinedWaitlist: false,
    aiFocusEnabled: false,
    suggestedAiFocus: "",
  };

  const $ = (id) => document.getElementById(id);
  const els = {
    accessGate: $("access-gate"),
    appShell: Array.from(document.querySelectorAll("[data-app-shell]")),
    accessPasswordForm: $("access-password-form"),
    accessPassword: $("access-password"),
    accessPasswordSubmit: $("access-password-submit"),
    accessPasswordStatus: $("access-password-status"),
    accessProfileForm: $("access-profile-form"),
    accessName: $("access-name"),
    accessEmail: $("access-email"),
    accessProfileSubmit: $("access-profile-submit"),
    accessProfileStatus: $("access-profile-status"),
    describe: $("describe"), notes: $("notes"), jdLink: $("jd-link"),
    criteriaSummaryWrap: $("criteria-summary-wrap"), criteriaSummary: $("criteria-summary"),
    jdFile: $("jd-file"), jdFileName: $("jd-file-name"),
    search: $("search-candidates"), status: $("status"),
    searchLimitTitle: $("search-limit-title"), searchLimitDetail: $("search-limit-detail"),
    joinWaitlist: $("join-waitlist"),
    results: $("results"), resultsTitle: $("results-title"),
    cards: $("cards"), relaxedNote: $("relaxed-note"), exportCsv: $("export-csv"),
    openAdv: $("open-advanced"), closeAdv: $("close-advanced"),
    resetSearch: $("reset-search"),
    advModal: $("advanced-modal"), advFields: $("adv-fields"),
    estimate: $("estimate-count"), applyFilters: $("apply-filters"),
  };

  // The intake card's resting status line (mirrors templates/index.html).
  const INITIAL_STATUS =
    "Enter a candidate description, paste a link, or upload a file above to find matching profiles.";

  // ---- tiny helpers ----
  async function api(path, opts) {
    const res = await fetch(path, opts);
    let data = {};
    try { data = await res.json(); } catch (e) { /* non-json (e.g. CSV) */ }
    return { ok: res.ok, status: res.status, data, res };
  }
  const splitList = (s) => (s || "").split(",").map((x) => x.trim()).filter(Boolean);
  const AI_FOCUS_LABELS = {
    research: "Research",
    model_engineering: "Model Engineering",
    infrastructure_systems: "Infrastructure and Systems",
  };
  const AI_FOCUS_OPTIONS = [
    { value: "", label: "Choose focus" },
    { value: "research", label: "Research" },
    { value: "model_engineering", label: "Model Engineering" },
    { value: "infrastructure_systems", label: "Infrastructure and Systems" },
  ];
  const gatedCriteria = (criteria) => {
    const c = Object.assign({}, criteria || {});
    if (!state.aiFocusEnabled) {
      if (c.ai_focus) state.suggestedAiFocus = c.ai_focus;
      c.ai_focus = "";
    }
    return c;
  };
  const withoutAiReview = (candidate) => {
    const c = Object.assign({}, candidate || {});
    [
      "ai_focus", "ai_focus_label", "ai_focus_confidence", "ai_company_evidence",
      "target_ai_focus", "target_ai_focus_label", "ai_fit_score", "ai_fit_rationale",
    ].forEach((key) => { delete c[key]; });
    return c;
  };
  // When `loading` is true, show an animated spinner beside the message so it's
  // obvious work is in progress (search/extraction can take a few seconds).
  const setStatus = (msg, loading = false) => {
    els.status.classList.toggle("loading", !!loading);
    els.status.innerHTML = loading
      ? `<span class="spinner" aria-hidden="true"></span><span>${esc(msg)}</span>`
      : esc(msg);
    els.status.hidden = false;
  };

  function hasCriteria(c) {
    if (!c) return false;
    const education = c.education || {};
    return Boolean(
      c.title || (c.title_variants || []).length || c.location || c.location_country ||
      (c.must_have_skills || []).length || (c.domain_signals || []).length ||
      (c.anchor_companies || []).length || (c.anchor_industries || []).length ||
      (education.schools || []).length || (education.majors || []).length ||
      c.seniority || c.yoe_min != null || c.yoe_max != null
    );
  }

  function currentBriefKey() {
    const file = els.jdFile.files && els.jdFile.files[0];
    return JSON.stringify({
      describe: els.describe.value.trim(),
      notes: els.notes.value.trim(),
      jdLink: els.jdLink.value.trim(),
      file: file ? `${file.name}:${file.size}` : "",
    });
  }

  function extraTermsText() {
    if (els.criteriaSummaryWrap.hidden) return "";
    return els.criteriaSummary.value.trim();
  }

  // =====================================================================
  // Alpha access gate
  // =====================================================================
  const ACCESS_KEY = "croot.alpha.access.v2";

  function syncQuotaControls() {
    const exhausted = state.searchesRemaining <= 0;
    els.search.disabled = exhausted;
    els.applyFilters.disabled = exhausted;
  }

  function updateUsage(data = {}) {
    if (Number.isFinite(data.search_limit)) state.searchLimit = data.search_limit;
    if (Number.isFinite(data.searches_used)) state.searchesUsed = data.searches_used;
    if (Number.isFinite(data.searches_remaining)) state.searchesRemaining = data.searches_remaining;
    if (Object.prototype.hasOwnProperty.call(data, "joined_waitlist")) {
      state.joinedWaitlist = Boolean(data.joined_waitlist);
    }

    if (state.joinedWaitlist) {
      els.searchLimitTitle.textContent = "You're on the waitlist";
      els.searchLimitDetail.textContent = "We'll follow up using the email you provided.";
      els.joinWaitlist.hidden = true;
    } else if (state.searchesRemaining <= 0) {
      els.searchLimitTitle.textContent = `You've used your ${state.searchLimit} free searches`;
      els.searchLimitDetail.textContent = "Join the waitlist to hear when more access opens.";
      els.joinWaitlist.hidden = false;
    } else {
      els.searchLimitTitle.textContent =
        `Try ${state.searchLimit} free searches and then join the waitlist`;
      els.searchLimitDetail.textContent =
        `${state.searchesRemaining} ${state.searchesRemaining === 1 ? "search" : "searches"} remaining`;
      els.joinWaitlist.hidden = true;
    }
    syncQuotaControls();
  }

  function showApp() {
    if (els.accessGate) els.accessGate.hidden = true;
    els.appShell.forEach((el) => { el.hidden = false; });
  }

  function showGate() {
    if (els.accessGate) els.accessGate.hidden = false;
    els.appShell.forEach((el) => { el.hidden = true; });
    els.accessPasswordForm.hidden = false;
    els.accessProfileForm.hidden = true;
    setTimeout(() => {
      const target = els.accessProfileForm.hidden ? els.accessPassword : els.accessName;
      if (target) target.focus();
    }, 50);
  }

  function setAccessStatus(el, msg) {
    if (el) el.textContent = msg || "";
  }

  async function restoreAccess() {
    if (!localStorage.getItem(ACCESS_KEY)) {
      showGate();
      return;
    }
    try {
      const result = await api("/api/access/status");
      if (result.ok && result.data.authenticated) {
        updateUsage(result.data);
        showApp();
        return;
      }
    } catch (err) {
      // Fall through to the gate when the saved session cannot be verified.
    }
    localStorage.removeItem(ACCESS_KEY);
    showGate();
  }

  restoreAccess();

  async function submitAccessPassword(e) {
    e.preventDefault();
    const password = els.accessPassword.value;
    if (!password) {
      setAccessStatus(els.accessPasswordStatus, "Enter the alpha password.");
      return;
    }
    els.accessPasswordSubmit.disabled = true;
    setAccessStatus(els.accessPasswordStatus, "");
    let result;
    try {
      result = await api("/api/access", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
    } catch (err) {
      setAccessStatus(els.accessPasswordStatus, "Could not check the password. Try again.");
      els.accessPasswordSubmit.disabled = false;
      return;
    }
    els.accessPasswordSubmit.disabled = false;
    if (!result.ok) {
      setAccessStatus(els.accessPasswordStatus, result.data.error || "Password check failed.");
      return;
    }
    state.accessPassword = password;
    els.accessPasswordForm.hidden = true;
    els.accessProfileForm.hidden = false;
    els.accessName.focus();
  }

  async function submitAccessProfile(e) {
    e.preventDefault();
    const name = els.accessName.value.trim();
    const email = els.accessEmail.value.trim();
    if (!name || !email) {
      setAccessStatus(els.accessProfileStatus, "Enter your name and email.");
      return;
    }
    els.accessProfileSubmit.disabled = true;
    setAccessStatus(els.accessProfileStatus, "");
    let result;
    try {
      result = await api("/api/access", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: state.accessPassword, name, email }),
      });
    } catch (err) {
      setAccessStatus(els.accessProfileStatus, "Could not save access details. Try again.");
      els.accessProfileSubmit.disabled = false;
      return;
    }
    els.accessProfileSubmit.disabled = false;
    if (!result.ok) {
      setAccessStatus(els.accessProfileStatus, result.data.error || "Could not save access details.");
      return;
    }
    localStorage.setItem(ACCESS_KEY, JSON.stringify({ name, email, at: Date.now() }));
    updateUsage(result.data);
    state.accessPassword = "";
    showApp();
  }

  async function joinWaitlist() {
    els.joinWaitlist.disabled = true;
    const result = await api("/api/access/waitlist", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    els.joinWaitlist.disabled = false;
    if (!result.ok) {
      setStatus(result.data.error || "Could not join the waitlist.");
      return;
    }
    updateUsage(result.data);
    setStatus("You're on the waitlist. We'll follow up by email.");
  }

  // =====================================================================
  // Advanced Search — field definitions. `key`/`map` wire to the Criteria
  // contract; `soon: true` marks fields the backend doesn't map yet (rendered
  // to match the prototype but inert — see docs/FILTER_BACKLOG.md).
  // =====================================================================
  const ADV = [
    { section: "Role" },
    { key: "title", label: "Job title", type: "text", ph: "e.g. Backend Engineer" },
    { key: "ai_focus", label: "AI focus", type: "ai-focus", options: AI_FOCUS_OPTIONS },
    { key: "seniority", label: "Seniority level", type: "select",
      options: ["", "Junior", "Mid", "Senior", "Lead", "Staff", "Principal", "Director", "VP", "C-level"] },
    { key: "yoe", label: "Years of experience", type: "range" },
    { key: "tenure_years", label: "Minimum tenure (years)", type: "number", ph: "e.g. 1" },
    { key: "workplace", label: "Workplace type", type: "select", options: ["Any", "Remote", "Hybrid", "On-site"] },
    { label: "Job function", type: "text", soon: true },
    { label: "Employment type", type: "select", options: ["Any", "Full-time", "Part-time", "Contract", "Internship"], soon: true },

    { section: "Location" },
    { key: "location", label: "City / region / country", type: "text", ph: "e.g. New York" },
    { label: "Postal/zip code radius", type: "text", soon: true },
    { label: "Network relationship degree", type: "select", options: ["Any", "1st", "2nd", "3rd+"], soon: true },

    { section: "Company" },
    { key: "anchor_companies", label: "Company", type: "list", ph: "comma separated, e.g. Stripe, Plaid" },
    { label: "Company size", type: "select", options: ["Any", "1-10", "11-50", "51-200", "201-1000", "1000+"], soon: true },
    { label: "Company type", type: "select", options: ["Any", "Startup", "Public", "Non-profit", "Government"], soon: true },

    { section: "Skills & keywords" },
    { key: "must_have_skills", label: "Skills used", type: "list", ph: "comma separated · must-have" },
    { key: "nice_to_have_skills", label: "Skills & assessments", type: "list", ph: "comma separated · nice-to-have" },
    { key: "domain_signals", label: "Keywords (Boolean)", type: "list", ph: "comma separated · e.g. fintech, payments" },
    { label: "Tags", type: "text", soon: true },
    { label: "Project", type: "text", soon: true },
    { label: "Project status", type: "select", options: ["Any", "Active", "Completed"], soon: true },

    { section: "Education" },
    { key: "majors", label: "Field of study", type: "list", ph: "comma separated, e.g. Computer Science" },
    { key: "schools", label: "School", type: "list", ph: "comma separated, e.g. MIT" },
    { label: "Degree", type: "select", options: ["Any", "Bachelor's", "Master's", "PhD"], soon: true },
    { label: "Year of graduation", type: "text", soon: true },
    { label: "Spoken languages", type: "text", soon: true },
  ];

  function buildAdvancedFields() {
    const html = ADV.map((f) => {
      if (f.section) return `<div class="adv-section">${f.section}</div>`;
      const cls = "adv-field" + (f.soon ? " soon" : "") + (f.type === "range" ? " full" : "");
      const id = f.key ? `adv-${f.key}` : "";
      let control;
      if (f.type === "ai-focus") {
        control = `<div class="ai-focus-control">
          <button id="adv-ai-focus-toggle" class="btn-ghost sm" type="button" aria-pressed="false">Turn On</button>
          <select class="input" id="adv-ai_focus" disabled>` +
          f.options.map((o) => `<option value="${o.value}">${o.label}</option>`).join("") +
          `</select>
        </div>`;
      } else if (f.type === "select") {
        control = `<select class="input" ${id ? `id="${id}"` : "disabled"}>` +
          f.options.map((o) => {
            const value = typeof o === "object" ? o.value : o;
            const label = typeof o === "object" ? o.label : (o || "Any");
            return `<option value="${value}">${label}</option>`;
          }).join("") + `</select>`;
      } else if (f.type === "range") {
        control = `<div class="adv-range">
          <input class="input" id="adv-yoe-min" type="number" min="0" placeholder="min" />
          <span class="muted">to</span>
          <input class="input" id="adv-yoe-max" type="number" min="0" placeholder="max" />
        </div>`;
      } else {
        const t = f.type === "number" ? "number" : "text";
        control = `<input class="input" type="${t}" ${id ? `id="${id}"` : "disabled"} placeholder="${f.ph || ""}" />`;
      }
      return `<div class="${cls}"><label>${f.label}</label>${control}</div>`;
    }).join("");
    els.advFields.innerHTML = html;

    // Live estimate on any wired-field change (debounced).
    els.advFields.querySelectorAll("input,select").forEach((el) => {
      el.addEventListener("input", debouncedEstimate);
      el.addEventListener("change", debouncedEstimate);
    });
    const aiToggle = $("adv-ai-focus-toggle");
    if (aiToggle) {
      aiToggle.addEventListener("click", () => {
        state.aiFocusEnabled = !state.aiFocusEnabled;
        const select = $("adv-ai_focus");
        if (state.aiFocusEnabled && select && !select.value) {
          select.value = state.criteria.ai_focus || state.suggestedAiFocus || "";
        }
        if (!state.aiFocusEnabled && select) select.value = "";
        syncAiFocusControl();
        debouncedEstimate();
      });
    }
    syncAiFocusControl();
  }

  function syncAiFocusControl() {
    const toggle = $("adv-ai-focus-toggle");
    const select = $("adv-ai_focus");
    if (!toggle || !select) return;
    select.disabled = !state.aiFocusEnabled;
    toggle.setAttribute("aria-pressed", String(state.aiFocusEnabled));
    toggle.textContent = state.aiFocusEnabled ? "On" : "Turn On";
  }

  function criteriaToModal() {
    const c = state.criteria;
    const setv = (id, v) => { const el = $(id); if (el) el.value = v == null ? "" : v; };
    setv("adv-title", c.title);
    if (c.ai_focus) state.aiFocusEnabled = true;
    setv("adv-ai_focus", state.aiFocusEnabled ? (c.ai_focus || state.suggestedAiFocus) : "");
    syncAiFocusControl();
    setv("adv-seniority", c.seniority);
    setv("adv-yoe-min", c.yoe_min);
    setv("adv-yoe-max", c.yoe_max);
    setv("adv-tenure_years", c.tenure_floor_months ? Math.round(c.tenure_floor_months / 12) : "");
    setv("adv-location", c.location);
    setv("adv-workplace", c.remote_ok ? "Remote" : "Any");
    setv("adv-anchor_companies", (c.anchor_companies || []).join(", "));
    setv("adv-must_have_skills", (c.must_have_skills || []).join(", "));
    setv("adv-nice_to_have_skills", (c.nice_to_have_skills || []).join(", "));
    setv("adv-domain_signals", (c.domain_signals || []).join(", "));
    setv("adv-majors", ((c.education || {}).majors || []).join(", "));
    setv("adv-schools", ((c.education || {}).schools || []).join(", "));
  }

  function modalToCriteria() {
    const val = (id) => { const el = $(id); return el ? el.value.trim() : ""; };
    const num = (id) => { const v = val(id); return v === "" ? null : Number(v); };
    const companies = splitList(val("adv-anchor_companies"));
    const aiFocus = state.aiFocusEnabled ? val("adv-ai_focus") : "";
    const c = {
      title: val("adv-title"),
      ai_focus: aiFocus,
      seniority: val("adv-seniority"),
      yoe_min: num("adv-yoe-min"),
      yoe_max: num("adv-yoe-max"),
      location: val("adv-location"),
      remote_ok: val("adv-workplace") === "Remote",
      anchor_companies: companies,
      anchor_strategy: companies.length ? "companies" : "none",
      must_have_skills: splitList(val("adv-must_have_skills")),
      nice_to_have_skills: splitList(val("adv-nice_to_have_skills")),
      domain_signals: splitList(val("adv-domain_signals")),
      education: { majors: splitList(val("adv-majors")), schools: splitList(val("adv-schools")), degrees: [] },
    };
    const tenure = num("adv-tenure_years");
    if (tenure != null) c.tenure_floor_months = Math.round(tenure * 12);
    // Carry over fields the modal doesn't expose.
    return gatedCriteria(Object.assign({}, state.criteria, c));
  }

  let estimateTimer = null;
  function debouncedEstimate() {
    clearTimeout(estimateTimer);
    els.estimate.textContent = "…";
    estimateTimer = setTimeout(async () => {
      const crit = modalToCriteria();
      const { ok, data } = await api("/api/preview", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(crit),
      });
      els.estimate.textContent = ok ? (data.total_count ?? "—") : "—";
    }, 500);
  }

  // ---- modal open/close ----
  function openModal() {
    criteriaToModal();
    els.advModal.hidden = false;
    debouncedEstimate();
  }
  function closeModal() { els.advModal.hidden = true; }

  function renderCriteriaSummary(criteria) {
    const c = criteria || {};
    const education = c.education || {};
    const lines = [];
    const add = (label, value) => {
      if (value == null || value === "") return;
      if (Array.isArray(value) && !value.length) return;
      lines.push(`${label}: ${Array.isArray(value) ? value.join(", ") : value}`);
    };

    add("Role", c.title);
    add("AI focus", state.aiFocusEnabled ? (AI_FOCUS_LABELS[c.ai_focus] || c.ai_focus) : "");
    add("Title variants", c.title_variants);
    add("Seniority", c.seniority);
    if (c.yoe_min != null || c.yoe_max != null) {
      add("Years", [c.yoe_min ?? "any", c.yoe_max ?? "any"].join(" to "));
    }
    add("Location", c.remote_ok ? "Remote ok"
      : (c.location || c.location_country
         || (c.location_region ? c.location_region.replace(/_/g, " ") : "")));
    add("Must-have skills", c.must_have_skills);
    add("Nice-to-have skills", c.nice_to_have_skills);
    add("Domain", c.domain_signals);
    add("Source companies", c.anchor_companies);
    add("Company cluster", c.cluster_hint);
    add("Industries", c.anchor_industries);
    add("Schools", education.schools);
    add("Majors", education.majors);
    add("Exclude employers", c.exclude_employers);
    add("Exclude titles", c.title_excludes);
    add("Hiring company", c.hiring_company);

    if (!lines.length) {
      els.criteriaSummaryWrap.hidden = true;
      els.criteriaSummary.value = "";
      state.criteriaSummaryText = "";
      return;
    }
    state.criteriaSummaryText = lines.join("\n");
    els.criteriaSummary.value = state.criteriaSummaryText;
    els.criteriaSummaryWrap.hidden = false;
  }

  // =====================================================================
  // JD extraction (file / link) -> text
  // =====================================================================
  async function extractJd() {
    if (els.jdFile.files && els.jdFile.files[0]) {
      const fd = new FormData();
      fd.append("file", els.jdFile.files[0]);
      const { ok, data } = await api("/api/extract", { method: "POST", body: fd });
      if (!ok) throw new Error(data.error || "Couldn't read that file.");
      return data.text;
    }
    const link = els.jdLink.value.trim();
    if (link) {
      const { ok, data } = await api("/api/extract", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: link }),
      });
      if (!ok) throw new Error(data.error || "Couldn't fetch that link.");
      return data.text;
    }
    return "";
  }

  // =====================================================================
  // Hybrid intake flow
  // =====================================================================
  async function executeSearch() {
    if (state.searchesRemaining <= 0) {
      setStatus("You've used all 5 free searches. Join the waitlist to continue.");
      return;
    }
    const briefChanged = state.lastParsedBriefKey && currentBriefKey() !== state.lastParsedBriefKey;
    if (!hasCriteria(state.criteria) || briefChanged) {
      await startSearch();
      return;
    }

    const extraTerms = extraTermsText();
    if (extraTerms && extraTerms !== state.criteriaSummaryText) {
      els.search.disabled = true;
      try {
        state.conversation.push({
          role: "user",
          content: `Update the criteria with these Extra terms, then search now:\n\n${extraTerms}`,
        });
        await chatTurn({ forceSearch: true });
      } finally {
        syncQuotaControls();
      }
      return;
    }

    els.search.disabled = true;
    try {
      await runSearch();
    } finally {
      syncQuotaControls();
    }
  }

  async function startSearch() {
    const describe = els.describe.value.trim();
    const notes = els.notes.value.trim();
    const extraTerms = extraTermsText();
    if (!describe && !notes && !els.jdLink.value.trim() && !(els.jdFile.files && els.jdFile.files[0])) {
      setStatus("Add a description, link, or file first — or use Advanced Search.");
      return;
    }
    els.search.disabled = true;
    renderCriteriaSummary({});
    setStatus("Reading your brief…", true);
    try {
      state.jdText = await extractJd();
    } catch (e) {
      setStatus(e.message);
      syncQuotaControls();
      return;
    }

    state.lastParsedBriefKey = currentBriefKey();
    const userMsg = [
      describe,
      notes && `Notes: ${notes}`,
      extraTerms && `Extra terms:\n${extraTerms}`,
    ].filter(Boolean).join("\n\n") || "(see job description)";
    state.conversation = [{ role: "user", content: userMsg }];
    await chatTurn();
    syncQuotaControls();
  }

  async function chatTurn(options = {}) {
    setStatus("Understanding your requirements…", true);
    const { ok, status, data } = await api("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: state.conversation, jd_text: state.jdText }),
    });
    state.jdText = "";  // only send JD once

    if (!ok) {
      // 503 = no Anthropic key yet. Steer to Advanced Search.
      if (status === 503) {
        setStatus("Conversational extraction isn't available yet — use Advanced Search to set criteria, then search.");
      } else {
        setStatus(data.error || "Something went wrong reading your brief.");
      }
      return;
    }

    state.criteria = gatedCriteria(data.criteria || {});
    renderCriteriaSummary(state.criteria);
    state.conversation.push({ role: "assistant", content: data.reply || "" });

    if (data.ready_to_search || options.forceSearch) {
      await runSearch();
    } else {
      setStatus("Updated the criteria. Add more in Extra terms or press Execute Search to continue.");
    }
  }

  // =====================================================================
  // Search + results
  // =====================================================================
  function clearResults() {
    state.results = [];
    els.results.hidden = true;
    els.resultsTitle.textContent = "Top Matches";
    els.relaxedNote.hidden = true;
    els.relaxedNote.textContent = "";
    els.cards.innerHTML = "";
  }

  // Wipe everything back to a blank slate — inputs, criteria, conversation, and
  // results — so the user can start a fresh search.
  function resetSearch() {
    state.conversation = [];
    state.criteria = {};
    state.jdText = "";
    state.criteriaSummaryText = "";
    state.lastParsedBriefKey = "";
    state.aiFocusEnabled = false;
    state.suggestedAiFocus = "";

    els.describe.value = "";
    els.notes.value = "";
    els.jdLink.value = "";
    els.jdFile.value = "";
    els.jdFileName.textContent = "No file chosen";

    renderCriteriaSummary({});   // clears + hides the Extra terms box
    clearResults();              // hides the results section
    closeModal();                // in case Advanced Search was open
    setStatus(INITIAL_STATUS);
    els.describe.focus();
  }

  async function runSearch(criteriaOverride) {
    if (state.searchesRemaining <= 0) {
      setStatus("You've used all 5 free searches. Join the waitlist to continue.");
      return;
    }
    const crit = gatedCriteria(criteriaOverride || state.criteria);
    clearResults();
    setStatus("Searching candidates…", true);
    const { ok, data } = await api("/api/search", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(crit),
    });
    if (!ok) {
      updateUsage(data);
      if (data.reauthenticate) {
        localStorage.removeItem(ACCESS_KEY);
        showGate();
        return;
      }
      setStatus(data.error || "Search failed.");
      return;
    }
    updateUsage(data);
    const aiFocusActive = Boolean((data.criteria || crit || {}).ai_focus);
    state.results = (data.candidates || []).map((c) => aiFocusActive ? c : withoutAiReview(c));
    renderResults(Object.assign({}, data, { criteria: crit }));
  }

  function scoreClass(s) { return s >= 85 ? "strong" : s >= 60 ? "good" : "partial"; }
  function scoreLabel(s) { return s >= 85 ? "Strong" : s >= 60 ? "Good" : "Partial"; }

  function renderResults(data) {
    els.status.hidden = true;
    els.results.hidden = false;
    const n = state.results.length;
    const showAiReview = Boolean((data.criteria || {}).ai_focus);
    els.resultsTitle.textContent = n ? `Top Matches (${n})` : "No matches";
    if (data.relaxed && data.relaxed.length) {
      els.relaxedNote.hidden = false;
      els.relaxedNote.textContent = "Loosened: " + data.relaxed.join(", ");
    } else {
      els.relaxedNote.hidden = true;
    }

    if (!n) {
      els.cards.innerHTML = `<p class="muted">No candidates matched — try loosening a filter in Advanced Search.</p>`;
      return;
    }

    els.cards.innerHTML = state.results.map((c, i) => {
      const skills = (c.top_skills || []).slice(0, 6)
        .map((s) => `<span class="chip">${esc(s)}</span>`).join("");
      const flags = (c.flags || []).map((f) => `<span class="chip flag">${esc(f)}</span>`).join("");
      const aiEvidence = showAiReview ? (c.ai_company_evidence || []).slice(0, 3)
        .map((s) => `<span class="chip ai">${esc(s)}</span>`).join("") : "";
      const aiReview = showAiReview ? [
        c.ai_focus_label && `Lane: ${c.ai_focus_label}`,
        c.target_ai_focus_label && c.ai_fit_score != null && `Target ${c.target_ai_focus_label}: ${c.ai_fit_score}`,
      ].filter(Boolean).join(" · ") : "";
      const prior = (c.prior_employers || []).slice(0, 3).join(" · ");
      const sub = [c.current_title, c.current_company && `@ ${c.current_company}`,
        c.region, c.yoe != null && `${c.yoe} yrs`].filter(Boolean).join(" · ");
      return `<article class="cand" data-i="${i}">
        <div class="cand-main">
          <div class="cand-name">${esc(c.name || "Unnamed")}</div>
          <div class="cand-sub">${esc(sub)}</div>
          ${prior ? `<div class="cand-sub">Previously: ${esc(prior)}</div>` : ""}
          ${aiReview ? `<div class="cand-ai">${esc(aiReview)}</div>` : ""}
          ${c.ai_fit_rationale ? `<div class="cand-sub">AI evidence: ${esc(c.ai_fit_rationale)}</div>` : ""}
          <div class="cand-rationale">${esc(c.rationale || "")}</div>
          <div class="cand-skills">${skills}${aiEvidence}${flags}</div>
        </div>
        <div class="cand-side">
          <div class="score ${scoreClass(c.score)}">${c.score}</div>
          <div class="score-label">${scoreLabel(c.score)} match</div>
          <div class="cand-links">
            ${c.linkedin_url ? `<a class="link" href="${esc(c.linkedin_url)}" target="_blank" rel="noopener">LinkedIn</a>` : ""}
            <a class="link reveal" href="#" data-i="${i}">Reveal contact</a>
          </div>
          <div class="contact-out" id="contact-${i}"></div>
        </div>
      </article>`;
    }).join("");

    els.cards.querySelectorAll(".reveal").forEach((a) => {
      a.addEventListener("click", (e) => { e.preventDefault(); revealContact(+a.dataset.i); });
    });
  }

  async function revealContact(i) {
    const c = state.results[i];
    const out = $("contact-" + i);
    if (!c || !c.linkedin_url) { if (out) out.textContent = "No LinkedIn URL."; return; }
    if (out) out.textContent = "Revealing…";
    const { ok, data } = await api("/api/profile?linkedin_url=" + encodeURIComponent(c.linkedin_url));
    if (!ok) { if (out) out.textContent = data.error || "Couldn't enrich."; return; }
    const p = (data.profiles || [])[0] || {};
    const info = p.personal_contact_info || {};
    const emails = (info.personal_emails || []).join(", ");
    const phones = (info.phone_numbers || []).join(", ");
    c.personal_email = (info.personal_emails || [])[0] || "";
    c.personal_phone = (info.phone_numbers || [])[0] || "";
    if (out) out.innerHTML = (emails || phones)
      ? `${esc(emails)}${emails && phones ? "<br/>" : ""}${esc(phones)}`
      : "No contact info found.";
  }

  async function exportCsv() {
    if (!state.results.length) return;
    const res = await fetch("/api/export", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        kind: "csv", candidates: state.results,
        meta: { role: state.criteria.title || "candidates" },
      }),
    });
    if (!res.ok) { setStatus("Export failed."); return; }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "croot-candidates.csv";
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ---- wire up ----
  els.jdFile.addEventListener("change", () => {
    els.jdFileName.textContent = els.jdFile.files && els.jdFile.files[0]
      ? els.jdFile.files[0].name : "No file chosen";
  });
  els.search.addEventListener("click", executeSearch);
  els.joinWaitlist.addEventListener("click", joinWaitlist);
  els.resetSearch.addEventListener("click", resetSearch);
  els.openAdv.addEventListener("click", openModal);
  els.closeAdv.addEventListener("click", closeModal);
  els.advModal.addEventListener("click", (e) => { if (e.target === els.advModal) closeModal(); });
  els.applyFilters.addEventListener("click", () => {
    state.criteria = modalToCriteria();
    renderCriteriaSummary(state.criteria);
    closeModal();
    runSearch();
  });
  els.exportCsv.addEventListener("click", exportCsv);
  els.accessPasswordForm.addEventListener("submit", submitAccessPassword);
  els.accessProfileForm.addEventListener("submit", submitAccessProfile);

  buildAdvancedFields();
})();
