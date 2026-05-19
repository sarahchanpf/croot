(() => {
  const state = {
    employers: [],
    must_have_skills: [],
    nice_to_have_skills: [],
    project_keywords: [],
    from_tiers: [],
    from_companies: [],
    // Advanced filters — multi-select pill groups
    function_areas: [],
    lines_of_defense: [],
    industries: [],
    exclude_seniority: [],
    work_preference: [],
    // Advanced filters — chip inputs
    career_arc: [],
    exclude_titles: [],
    exclude_companies: [],
    exclude_skills: [],
  };

  // ---------- Access flow ----------
  // Every visitor gets FREE_SEARCH_LIMIT searches. After that the search
  // button opens a "join Croot" modal; submitting unlocks unlimited searches.
  // All tracked client-side via localStorage.
  const FREE_SEARCH_LIMIT = 3;
  const LS = {
    USED_COUNT: "croot_searches_used",  // integer
    UNLOCKED: "croot_unlocked",         // "1" once they submit their email
    // legacy keys retained only to read during migration
    LEGACY_USED: "croot_used",
    LEGACY_WAITLISTED: "croot_waitlisted",
  };

  const access = {
    searchesUsed() {
      try {
        const v = parseInt(localStorage.getItem(LS.USED_COUNT) || "0", 10);
        return Number.isFinite(v) && v > 0 ? v : 0;
      } catch { return 0; }
    },
    remaining() {
      return Math.max(0, FREE_SEARCH_LIMIT - this.searchesUsed());
    },
    isUnlocked() {
      try { return localStorage.getItem(LS.UNLOCKED) === "1"; } catch { return false; }
    },
    canSearch() {
      return this.isUnlocked() || this.remaining() > 0;
    },
    bumpUsed() {
      try {
        const next = this.searchesUsed() + 1;
        localStorage.setItem(LS.USED_COUNT, String(next));
      } catch {}
    },
    unlock() {
      try { localStorage.setItem(LS.UNLOCKED, "1"); } catch {}
    },
  };

  // Migrate the older single-search flag schema so returning visitors don't
  // start fresh or get unexpectedly gated.
  (function migrateAccess() {
    try {
      if (localStorage.getItem(LS.UNLOCKED) || localStorage.getItem(LS.USED_COUNT)) return;
      if (localStorage.getItem(LS.LEGACY_WAITLISTED) === "1") {
        // Previously joined the old waitlist → unlock outright.
        localStorage.setItem(LS.UNLOCKED, "1");
      } else if (localStorage.getItem(LS.LEGACY_USED) === "1") {
        // Burned the old single free search → give them 2 more under the new
        // 3-search budget rather than gating immediately, which would feel
        // punitive on their first visit under the new rules.
        localStorage.setItem(LS.USED_COUNT, "1");
      }
      localStorage.removeItem(LS.LEGACY_USED);
      localStorage.removeItem(LS.LEGACY_WAITLISTED);
    } catch {}
  })();

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // ---------- Employer blocks ----------
  const employersList = $("#employers");
  const employerTemplate = $("#employer-template");

  function renderEmployers() {
    employersList.innerHTML = "";
    state.employers.forEach((employer, i) => {
      const node = employerTemplate.content.firstElementChild.cloneNode(true);
      node.dataset.id = employer.id;
      node.dataset.tenure = employer.tenure || "either";
      const leadEl = node.querySelector("[data-lead]");
      if (leadEl) leadEl.textContent = i === 0 ? "First worked at" : "Then at";

      const companyInput = node.querySelector('[data-name="company"]');
      const startInput = node.querySelector('[data-name="start_year"]');
      const endInput = node.querySelector('[data-name="end_year"]');

      companyInput.value = employer.company;
      startInput.value = employer.start_year;
      endInput.value = employer.end_year;

      companyInput.addEventListener("input", (e) => (employer.company = e.target.value));
      startInput.addEventListener("input", (e) => (employer.start_year = e.target.value));
      endInput.addEventListener("input", (e) => (employer.end_year = e.target.value));

      // Tenure segmented control — visually mirror state and update on click.
      const tenureBtns = node.querySelectorAll("[data-tenure-value]");
      const syncTenureUI = () => {
        const active = employer.tenure || "either";
        tenureBtns.forEach((b) => {
          const on = b.dataset.tenureValue === active;
          b.classList.toggle("is-selected", on);
          b.setAttribute("aria-pressed", on ? "true" : "false");
        });
        node.dataset.tenure = active;
      };
      syncTenureUI();
      tenureBtns.forEach((btn) => {
        btn.addEventListener("click", () => {
          const next = btn.dataset.tenureValue;
          if (!next || next === employer.tenure) return;
          employer.tenure = next;
          syncTenureUI();
          if (typeof schedulePreview === "function") schedulePreview();
        });
      });

      // Per-row company-size dropdown.
      const sizeSelect = node.querySelector('[data-name="company_size"]');
      if (sizeSelect) {
        sizeSelect.value = employer.company_size || "";
        sizeSelect.addEventListener("change", (e) => {
          employer.company_size = e.target.value;
        });
      }

      node.querySelector("[data-remove]").addEventListener("click", () => {
        state.employers = state.employers.filter((e) => e.id !== employer.id);
        renderEmployers();
        if (typeof schedulePreview === "function") schedulePreview();
      });

      employersList.appendChild(node);
    });
  }

  function addEmployer() {
    state.employers.push({
      id: crypto.randomUUID(),
      company: "",
      start_year: "",
      end_year: "",
      tenure: "either",
      company_size: "",
    });
    renderEmployers();
  }

  $("#add-employer").addEventListener("click", addEmployer);
  addEmployer();

  // ---------- "They came from..." tier pills ----------
  $$(".tier-pill").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tier = btn.dataset.tier;
      if (!tier) return;
      const idx = state.from_tiers.indexOf(tier);
      if (idx >= 0) {
        state.from_tiers.splice(idx, 1);
        btn.classList.remove("is-selected");
        btn.setAttribute("aria-pressed", "false");
      } else {
        state.from_tiers.push(tier);
        btn.classList.add("is-selected");
        btn.setAttribute("aria-pressed", "true");
      }
      if (typeof schedulePreview === "function") schedulePreview();
    });
  });

  // ---------- Generic pill groups (advanced filters multi-selects) ----------
  $$(".pill-group").forEach((group) => {
    const key = group.dataset.multiKey;
    if (!key) return;
    if (!Array.isArray(state[key])) state[key] = [];
    group.querySelectorAll(".filter-pill").forEach((btn) => {
      btn.addEventListener("click", () => {
        const value = btn.dataset.value;
        if (!value) return;
        const arr = state[key];
        const idx = arr.indexOf(value);
        if (idx >= 0) {
          arr.splice(idx, 1);
          btn.classList.remove("is-selected");
          btn.setAttribute("aria-pressed", "false");
        } else {
          arr.push(value);
          btn.classList.add("is-selected");
          btn.setAttribute("aria-pressed", "true");
        }
        if (typeof schedulePreview === "function") schedulePreview();
      });
    });
  });

  // ---------- Chip inputs ----------
  $$(".chip-input").forEach((wrap) => {
    const key = wrap.dataset.key;
    const list = wrap.querySelector("[data-chips]");
    const input = wrap.querySelector("[data-chip-input]");
    const template = $("#chip-template");

    function render() {
      list.innerHTML = "";
      state[key].forEach((value, idx) => {
        const node = template.content.firstElementChild.cloneNode(true);
        node.querySelector("[data-chip-label]").textContent = value;
        node.querySelector("[data-chip-remove]").addEventListener("click", () => {
          state[key].splice(idx, 1);
          render();
          if (typeof schedulePreview === "function") schedulePreview();
        });
        list.appendChild(node);
      });
    }

    function commit() {
      const raw = input.value;
      const parts = raw.split(",").map((s) => s.trim()).filter(Boolean);
      if (!parts.length) return;
      for (const p of parts) {
        if (!state[key].includes(p)) state[key].push(p);
      }
      input.value = "";
      render();
      if (typeof schedulePreview === "function") schedulePreview();
    }

    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === ",") {
        e.preventDefault();
        commit();
      } else if (e.key === "Backspace" && !input.value && state[key].length) {
        state[key].pop();
        render();
      }
    });
    input.addEventListener("blur", commit);

    // expose for resetting if needed
    wrap._render = render;
  });

  // Active search mode — Similar is the sensible default (the same operator
  // the skill recommends with a tiny bit of leniency on title matching).
  let currentMode = "similar";

  // ---------- Criteria collection ----------
  // Pulled out so /api/search, /api/preview, and the auto-fill flow all
  // read the same shape.
  function collectCriteria() {
    const fd = new FormData(form);
    const checked = (name) => !!(form.elements[name] && form.elements[name].checked);
    return {
      current_title: fd.get("current_title") || "",
      location: fd.get("location") || "",
      location_radius_miles: fd.get("location_radius_miles") || "",
      work_preference: state.work_preference.slice(),
      seniority: fd.get("seniority") || "",
      years_experience_min: fd.get("years_experience_min") || "",
      years_experience_max: fd.get("years_experience_max") || "",
      school: fd.get("school") || "",
      must_have_skills: state.must_have_skills.slice(),
      nice_to_have_skills: state.nice_to_have_skills.slice(),
      project_keywords: state.project_keywords.slice(),
      from_tiers: state.from_tiers.slice(),
      from_companies: state.from_companies.slice(),
      came_from_size: fd.get("came_from_size") || "",
      mode: currentMode,
      employers: state.employers
        .map((e) => ({
          company: e.company.trim(),
          start_year: e.start_year.toString().trim(),
          end_year: e.end_year.toString().trim(),
          tenure: e.tenure || "either",
          company_size: e.company_size || "",
        }))
        .filter((e) => e.company || e.start_year || e.end_year),
      // Advanced filters
      exclude_overly_senior: checked("exclude_overly_senior"),
      hands_on_leader: checked("hands_on_leader"),
      team_size_led: fd.get("team_size_led") || "",
      function_areas: state.function_areas.slice(),
      lines_of_defense: state.lines_of_defense.slice(),
      industries: state.industries.slice(),
      stakeholder_range: fd.get("stakeholder_range") || "",
      technical_depth: fd.get("technical_depth") || "",
      signal_recently_changed: checked("signal_recently_changed"),
      signal_likely_mobile: checked("signal_likely_mobile"),
      weight_recent_experience: checked("weight_recent_experience"),
      career_arc: state.career_arc.slice(),
      exclude_titles: state.exclude_titles.slice(),
      exclude_companies: state.exclude_companies.slice(),
      exclude_skills: state.exclude_skills.slice(),
      exclude_seniority: state.exclude_seniority.slice(),
    };
  }

  function hasAnyCriteria(c) {
    const arr = (a) => Array.isArray(a) && a.length > 0;
    return !!(
      c.current_title || c.location || c.seniority ||
      c.years_experience_min || c.years_experience_max || c.school ||
      arr(c.must_have_skills) || arr(c.nice_to_have_skills) ||
      arr(c.project_keywords) || arr(c.employers) ||
      arr(c.from_tiers) || arr(c.from_companies) ||
      c.exclude_overly_senior || c.hands_on_leader ||
      c.team_size_led || c.stakeholder_range || c.technical_depth ||
      c.signal_recently_changed || c.signal_likely_mobile ||
      c.weight_recent_experience ||
      arr(c.function_areas) || arr(c.lines_of_defense) || arr(c.industries) ||
      arr(c.work_preference) ||
      arr(c.career_arc) || arr(c.exclude_titles) ||
      arr(c.exclude_companies) || arr(c.exclude_skills) ||
      arr(c.exclude_seniority)
    );
  }

  // ---------- Submit ----------
  const form = $("#search-form");
  const statusEl = $("#status");
  const submitBtn = $("#submit");

  function setStatus(text, tone = "") {
    statusEl.textContent = text;
    if (tone) statusEl.dataset.tone = tone;
    else delete statusEl.dataset.tone;
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();

    // Gate: out of free searches and not unlocked → open the join modal.
    // Unlocked users always pass through.
    if (!access.canSearch()) {
      openWaitlistModal({ pendingSearch: true });
      return;
    }

    const criteria = collectCriteria();

    if (!hasAnyCriteria(criteria)) {
      setStatus("Add at least one criterion to search.", "error");
      return;
    }

    submitBtn.disabled = true;
    setStatus("Searching Crustdata…");

    try {
      const res = await fetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(criteria),
      });
      const body = await res.json();

      if (!res.ok) {
        const detail = body?.details ? ` — ${truncate(body.details, 240)}` : "";
        setStatus(`${body?.error || "Search failed."}${detail}`, "error");
        return;
      }

      const profiles = extractProfiles(body.results);
      renderResults(profiles, body);
      setStatus(
        body.from_cache
          ? `Loaded ${profiles.length} candidate${profiles.length === 1 ? "" : "s"} from cache.`
          : `Loaded ${profiles.length} candidate${profiles.length === 1 ? "" : "s"} from Crustdata.`,
        "ok",
      );
      // Count this against the free quota — only if the user isn't already
      // unlocked. Cached responses also count; the gate is per search action,
      // not per Crustdata call.
      if (!access.isUnlocked()) {
        access.bumpUsed();
      }
      refreshAccessUI();
      loadHistory();
      revealSaveSearch();
    } catch (err) {
      setStatus(`Network error: ${err.message || err}`, "error");
    } finally {
      submitBtn.disabled = false;
    }
  });

  // ---------- Results rendering ----------
  const resultsList = $("#results-list");
  const resultsMeta = $("#results-meta");
  const resultTemplate = $("#result-template");

  function extractProfiles(payload) {
    if (!payload) return [];
    if (Array.isArray(payload)) return payload;
    const candidates = [
      payload.profiles,
      payload.results,
      payload.data,
      payload.hits,
      payload.persons,
      payload.candidates,
    ];
    for (const c of candidates) {
      if (Array.isArray(c)) return c;
    }
    return [];
  }

  function pick(obj, paths) {
    for (const path of paths) {
      const parts = path.split(".");
      let cur = obj;
      let ok = true;
      for (const p of parts) {
        if (cur == null || typeof cur !== "object" || !(p in cur)) {
          ok = false;
          break;
        }
        cur = cur[p];
      }
      if (ok && cur != null && cur !== "") return cur;
    }
    return "";
  }

  function firstEmployer(p) {
    const arr = p && (p.current_employers || p.past_employers || p.all_employers);
    return Array.isArray(arr) && arr.length ? arr[0] : null;
  }

  function profileFields(p) {
    const name =
      pick(p, ["name", "full_name", "display_name"]) ||
      [pick(p, ["first_name"]), pick(p, ["last_name"])].filter(Boolean).join(" ");
    const emp = firstEmployer(p) || {};
    const title =
      pick(emp, ["title", "employee_title"]) ||
      pick(p, ["current_title", "headline"]);
    const company =
      pick(emp, ["name", "employer_name", "company_name"]) ||
      pick(p, ["current_company", "company_name", "company"]);
    const location =
      pick(p, [
        "region",
        "location",
        "location_details.city",
        "location_details.country",
        "location_name",
      ]);
    const link = pick(p, [
      "flagship_profile_url",
      "linkedin_flagship_url",
      "linkedin_profile_url",
      "linkedin_url",
      "profile_url",
      "url",
    ]);
    return { name, title, company, location, link };
  }

  const MODE_LABELS = {
    exact: "Exact mode",
    similar: "Similar mode",
    broad: "Broad mode",
  };

  function renderResultsHeader(body, profiles) {
    const modeEl = document.getElementById("results-mode");
    const relaxedEl = document.getElementById("results-relaxed");
    const mode = body.mode || (body.results && body.results._mode) || "similar";
    const relaxed = body.relaxed || (body.results && body.results._relaxed) || [];

    if (profiles.length || mode) {
      modeEl.hidden = false;
      modeEl.dataset.mode = mode;
      modeEl.textContent = MODE_LABELS[mode] || mode;
    } else {
      modeEl.hidden = true;
    }

    const autoSwitchedFrom = body.auto_mode_switch_from;
    const messages = [];
    if (autoSwitchedFrom === "exact" && mode === "similar") {
      messages.push("Switched to Similar — you had a lot of filters active, Exact would have zeroed out.");
    }
    if (relaxed && relaxed.length) {
      messages.push(`Broad mode relaxed ${relaxed.join(", ")} to surface this pool.`);
    }
    if (messages.length) {
      relaxedEl.hidden = false;
      relaxedEl.textContent = messages.join(" ");
    } else {
      relaxedEl.hidden = true;
      relaxedEl.textContent = "";
    }
  }

  function applyMatchBadge(node, match) {
    const badge = node.querySelector("[data-match]");
    if (!badge) return;
    if (!match || typeof match.score !== "number") {
      badge.hidden = true;
      return;
    }
    badge.hidden = false;
    const label = match.label || "Match";
    const tier = label.startsWith("Strong") ? "strong"
      : label.startsWith("Good") ? "good"
      : "partial";
    badge.dataset.tier = tier;
    badge.querySelector("[data-match-label]").textContent = `${label} · ${match.score}%`;
  }

  function renderResults(profiles, body) {
    resultsList.innerHTML = "";

    if (!profiles.length) {
      const li = document.createElement("li");
      li.className = "results__empty";
      const suggestions = body.suggestions || (body.results && body.results._suggestions) || [];
      if (suggestions.length) {
        const list = suggestions.map((s) => `<li>${escapeHtml(s)}</li>`).join("");
        li.innerHTML = `
          <p class="results__empty-title">No results found.</p>
          <p class="results__empty-sub">Try:</p>
          <ul class="results__empty-list">${list}</ul>
        `;
      } else {
        li.textContent = "No candidates matched. Try loosening a criterion or switching to Broad mode.";
      }
      resultsList.appendChild(li);
      resultsMeta.textContent = body.from_cache ? "Cached · 0 candidates" : "0 candidates";
      renderResultsHeader(body, profiles);
      refreshExportUI();
      return;
    }

    profiles.forEach((p) => {
      const fields = profileFields(p);
      const node = resultTemplate.content.firstElementChild.cloneNode(true);
      node.querySelector("[data-name]").textContent = fields.name || "Unknown candidate";
      node.querySelector("[data-title]").textContent = fields.title || "";
      node.querySelector("[data-company]").textContent = fields.company || "";
      node.querySelector("[data-location]").textContent = fields.location || "—";

      // Stash plain text on the card so CSV export can read it without
      // re-deriving from the DOM hierarchy.
      node.dataset.name = fields.name || "";
      node.dataset.title = fields.title || "";
      node.dataset.company = fields.company || "";

      const sep = node.querySelector("[data-sep]");
      if (!(fields.title && fields.company)) {
        sep.dataset.hidden = "true";
      }
      if (!fields.title && !fields.company) {
        node.querySelector(".card__role").remove();
      }

      applyMatchBadge(node, p._match);

      const link = node.querySelector("[data-link]");
      const copyBtn = node.querySelector("[data-copy-linkedin]");
      if (fields.link) {
        link.href = fields.link;
        // Stash the URL on the card itself so the click-to-open-panel handler
        // can find it without having to walk the inner anchor.
        node.dataset.linkedinUrl = fields.link;
      } else {
        link.remove();
        if (copyBtn) copyBtn.remove();
      }

      resultsList.appendChild(node);
    });

    resultsMeta.textContent = `${body.from_cache ? "Cached" : "Live"} · ${profiles.length} candidate${profiles.length === 1 ? "" : "s"}`;
    renderResultsHeader(body, profiles);
    refreshExportUI();
  }

  function truncate(s, n) {
    if (!s) return "";
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  // ---------- History ----------
  const historyEl = $("#history");

  function relativeTime(iso) {
    const t = new Date(iso).getTime();
    const diff = (Date.now() - t) / 1000;
    if (diff < 60) return "just now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  }

  async function loadHistory() {
    try {
      const res = await fetch("/api/history");
      if (!res.ok) return;
      const rows = await res.json();
      historyEl.innerHTML = "";
      if (!rows.length) {
        const empty = document.createElement("li");
        empty.className = "history__empty";
        empty.textContent = "Your last 25 searches will land here.";
        historyEl.appendChild(empty);
        return;
      }
      rows.forEach((row) => {
        const li = document.createElement("li");
        li.className = "history__item";
        const summary = document.createElement("span");
        summary.className = "history__summary";
        summary.textContent = row.summary;
        const time = document.createElement("span");
        time.className = "history__time";
        time.textContent = relativeTime(row.created_at);
        li.append(summary, time);
        historyEl.appendChild(li);
      });
    } catch {
      /* silent */
    }
  }

  loadHistory();

  // ---------- Semantic search ----------
  const semanticForm = $("#semantic-form");
  const semanticInput = $("#semantic-input");
  const semanticStatus = $("#semantic-status");

  function setSemanticStatus(text, tone = "") {
    semanticStatus.textContent = text;
    if (tone) semanticStatus.dataset.tone = tone;
    else delete semanticStatus.dataset.tone;
  }

  function applyCriteria(c) {
    if (!c || typeof c !== "object") return [];
    const touched = [];
    const scalarFields = [
      "current_title",
      "location",
      "school",
      "seniority",
      "years_experience_min",
      "years_experience_max",
      "location_radius_miles",
      "team_size_led",
      "stakeholder_range",
      "technical_depth",
      "came_from_size",
    ];
    for (const key of scalarFields) {
      if (c[key] != null && c[key] !== "") {
        const el = form.querySelector(`[name="${key}"]`);
        if (el) {
          el.value = c[key];
          touched.push(key);
        }
      }
    }
    const toggleFields = [
      "exclude_overly_senior",
      "hands_on_leader",
      "signal_recently_changed",
      "signal_likely_mobile",
      "weight_recent_experience",
    ];
    for (const key of toggleFields) {
      if (c[key] === undefined) continue;
      const el = form.querySelector(`[name="${key}"]`);
      if (el) {
        el.checked = !!c[key];
        if (c[key]) touched.push(key);
      }
    }
    // Legacy back-compat: saved searches stored before the rename used
    // `recently_changed_jobs`. Map onto the new toggle if present.
    if (c.recently_changed_jobs && !c.signal_recently_changed) {
      const el = form.querySelector('[name="signal_recently_changed"]');
      if (el) { el.checked = true; touched.push("signal_recently_changed"); }
    }
    // Skills come back split into two buckets. Legacy `skills` falls
    // through into nice-to-have so the search doesn't over-restrict on a
    // pre-split JD reply.
    const fillChip = (key, values) => {
      if (!Array.isArray(values) || !values.length) return;
      state[key] = values.slice();
      const wrap = document.querySelector(`.chip-input[data-key="${key}"]`);
      if (wrap && wrap._render) wrap._render();
      touched.push(key);
    };
    fillChip("must_have_skills", c.must_have_skills);
    fillChip("nice_to_have_skills", c.nice_to_have_skills);
    // Adjacent/related titles inferred from the JD title — surfaced as
    // "Title: X" chips in the same Good-to-have input so the recruiter can
    // delete any they don't want before searching.
    if (Array.isArray(c.title_cluster) && c.title_cluster.length) {
      if (!Array.isArray(state.nice_to_have_skills)) state.nice_to_have_skills = [];
      for (const t of c.title_cluster) {
        const chip = `Title: ${t}`;
        if (!state.nice_to_have_skills.includes(chip)) {
          state.nice_to_have_skills.push(chip);
        }
      }
      const wrap = document.querySelector('.chip-input[data-key="nice_to_have_skills"]');
      if (wrap && wrap._render) wrap._render();
      touched.push("nice_to_have_skills");
    }
    fillChip("project_keywords", c.project_keywords);
    fillChip("from_companies", c.from_companies);
    fillChip("career_arc", c.career_arc);
    fillChip("exclude_titles", c.exclude_titles);
    fillChip("exclude_companies", c.exclude_companies);
    fillChip("exclude_skills", c.exclude_skills);
    if ((!c.must_have_skills || !c.must_have_skills.length) &&
        (!c.nice_to_have_skills || !c.nice_to_have_skills.length) &&
        Array.isArray(c.skills) && c.skills.length) {
      fillChip("nice_to_have_skills", c.skills);
    }

    const fillPillGroup = (key, values, selector) => {
      if (!Array.isArray(values)) return;
      state[key] = values.slice();
      const group = document.querySelector(selector);
      if (!group) return;
      group.querySelectorAll(".filter-pill, .tier-pill").forEach((btn) => {
        const v = btn.dataset.value || btn.dataset.tier;
        const on = values.indexOf(v) >= 0;
        btn.classList.toggle("is-selected", on);
        btn.setAttribute("aria-pressed", on ? "true" : "false");
      });
      if (values.length) touched.push(key);
    };
    fillPillGroup("from_tiers", c.from_tiers, "#tier-grid");
    fillPillGroup("function_areas", c.function_areas, '[data-multi-key="function_areas"]');
    fillPillGroup("lines_of_defense", c.lines_of_defense, '[data-multi-key="lines_of_defense"]');
    fillPillGroup("industries", c.industries, '[data-multi-key="industries"]');
    fillPillGroup("exclude_seniority", c.exclude_seniority, '[data-multi-key="exclude_seniority"]');
    fillPillGroup("work_preference", c.work_preference, '[data-multi-key="work_preference"]');

    if (Array.isArray(c.employers) && c.employers.length) {
      state.employers = c.employers.map((e) => ({
        id: crypto.randomUUID(),
        company: e.company || "",
        start_year: e.start_year || "",
        end_year: e.end_year || "",
        tenure: e.tenure || "either",
        company_size: e.company_size || "",
      }));
      renderEmployers();
      touched.push("employers");
    }

    if (c.mode && typeof setMode === "function") {
      setMode(c.mode);
    }

    return touched;
  }

  semanticForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = (semanticInput.value || "").trim();
    if (!text) {
      setSemanticStatus("Type a query first.", "error");
      return;
    }
    setSemanticStatus("Parsing…");
    try {
      const res = await fetch("/api/parse", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      const body = await res.json();
      if (!res.ok) {
        setSemanticStatus(body?.error || "Parse failed.", "error");
        return;
      }
      const touched = applyCriteria(body.criteria || {});
      if (!touched.length) {
        setSemanticStatus(
          "Couldn’t extract anything I recognise — try refining the structured fields below.",
          "error",
        );
        return;
      }
      setSemanticStatus(`Filled: ${touched.join(", ")}. Switching to the form to review.`);
      scrollToCompose();
      schedulePreview();
    } catch (err) {
      setSemanticStatus(`Network error: ${err.message || err}`, "error");
    }
  });

  // All three entry sections (Brief / Describe / Compose) live on the page
  // simultaneously now — no tabs to switch. After Brief or Describe parses a
  // query into criteria, we smooth-scroll to the Compose section so the
  // recruiter sees the populated form ready to search.
  function scrollToCompose() {
    const heading = document.querySelector(
      '.entry__anchor:nth-of-type(3), [data-builder-anchor]',
    );
    const target = document.querySelector('.builder') || heading;
    if (target && typeof target.scrollIntoView === "function") {
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }
  // ---------- Context upload (paste / file → extract) ----------
  const contextForm = $("#context-form");
  const contextText = $("#context-text");
  const contextFile = $("#context-file");
  const contextFileName = $("#context-file-name");
  const contextStatus = $("#context-status");
  const contextSummary = $("#context-summary");
  const contextSubmit = $("#context-submit");

  function setContextStatus(text, tone = "") {
    contextStatus.textContent = text;
    if (tone) contextStatus.dataset.tone = tone;
    else delete contextStatus.dataset.tone;
  }

  contextFile.addEventListener("change", () => {
    const f = contextFile.files && contextFile.files[0];
    contextFileName.textContent = f ? f.name : "";
  });

  function renderExtractedSummary(c, sources) {
    sources = sources || {};
    if (!c || !Object.keys(c).length) {
      contextSummary.hidden = true;
      contextSummary.innerHTML = "";
      return;
    }
    const rows = [];
    const push = (label, value, sourceKey) => {
      if (value === undefined || value === null || value === "" || (Array.isArray(value) && !value.length)) return;
      const origin = sources[sourceKey];
      const originPart = origin ? `<span class="context__summary-origin">from ${escapeHtml(origin)}</span>` : "";
      rows.push(`<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)} ${originPart}</dd>`);
    };
    push("Title", c.current_title, "current_title");
    push("Location", c.location, "location");
    push("School", c.school, "school");
    push("Seniority", c.seniority, "seniority");
    const yoe = [c.years_experience_min, c.years_experience_max].filter((v) => v !== "" && v != null);
    if (yoe.length) push("Years", yoe.join("–"), "years_experience_min");
    if (Array.isArray(c.skills) && c.skills.length) push("Skills", c.skills.join(", "), "skills");
    if (Array.isArray(c.project_keywords) && c.project_keywords.length) push("Keywords", c.project_keywords.join(", "), "project_keywords");
    // We never populate employers from a JD anymore — surface that explicitly
    // when the parser hands us an empty list so recruiters know to fill it
    // manually.
    if (Array.isArray(c.employers) && c.employers.length === 0) {
      rows.push(`<dt>Employers</dt><dd class="context__summary-empty">not extracted — JDs don’t tell you where candidates worked. Add them below if you want to anchor on past employers.</dd>`);
    }
    if (!rows.length) {
      contextSummary.hidden = true;
      contextSummary.innerHTML = "";
      return;
    }
    contextSummary.hidden = false;
    contextSummary.innerHTML = `
      <p class="context__summary-title">Extracted — review and adjust below before searching</p>
      <dl class="context__summary-list">${rows.join("")}</dl>
    `;
  }

  contextForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const file = contextFile.files && contextFile.files[0];
    const text = (contextText.value || "").trim();
    if (!file && !text) {
      setContextStatus("Paste a brief or attach a file first.", "error");
      return;
    }
    contextSubmit.disabled = true;
    setContextStatus("Reading and extracting…");

    try {
      let res;
      if (file) {
        const fd = new FormData();
        fd.append("file", file);
        res = await fetch("/api/extract", { method: "POST", body: fd });
      } else {
        res = await fetch("/api/extract", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
        });
      }
      const body = await res.json();
      if (!res.ok) {
        setContextStatus(body?.error || "Couldn’t extract criteria.", "error");
        return;
      }
      const criteria = body.criteria || {};
      const touched = applyCriteria(criteria);
      // JD extraction never populates employers — clear any prior selection
      // so a stale "ex-Apple" from a previous Describe doesn't survive.
      state.employers = [];
      renderEmployers();
      renderExtractedSummary(criteria, body.sources || {});
      if (!touched.length) {
        setContextStatus(
          "Nothing recognisable extracted — fill the structured fields manually.",
          "error",
        );
        return;
      }
      const label = body.source === "pdf" ? "PDF"
        : body.source === "docx" ? "DOCX"
        : "text";
      setContextStatus(`Pulled ${touched.length} field${touched.length === 1 ? "" : "s"} from your ${label}. Review and Search below.`);
      scrollToCompose();
      schedulePreview();
    } catch (err) {
      setContextStatus(`Upload failed: ${err.message || err}`, "error");
    } finally {
      contextSubmit.disabled = false;
    }
  });

  // ---------- Preview count (debounced) ----------
  const previewEl = $("#preview-count");

  function setPreview(text, tone = "") {
    previewEl.innerHTML = text;
    if (tone) previewEl.dataset.tone = tone;
    else delete previewEl.dataset.tone;
  }

  let previewTimer = null;
  let previewSeq = 0;
  let lastPreviewCount = null;

  // Bucket the count into the four-tone band the recruiter sees on the gauge.
  // Both ends — too-few and too-many — surface as warnings, with the empty
  // case escalated to danger.
  function previewTone(n) {
    if (n <= 10) return "red";
    if (n <= 50) return "yellow";
    if (n <= 500) return "green";
    return "yellow";
  }

  async function runPreview() {
    const criteria = collectCriteria();
    if (!hasAnyCriteria(criteria)) {
      lastPreviewCount = null;
      setPreview("Tune the filters — count appears here before you commit.");
      return;
    }
    const seq = ++previewSeq;
    // Keep the previous number visible during the refresh — replace just the
    // label with a spinner so the recruiter sees the gauge is updating without
    // losing context.
    if (lastPreviewCount != null) {
      const formatted = lastPreviewCount.toLocaleString();
      setPreview(
        `<span class="preview-count__spinner" aria-hidden="true"></span>~<span class="preview-count__big">${formatted}</span> candidates`,
        "loading",
      );
    } else {
      setPreview(
        `<span class="preview-count__spinner" aria-hidden="true"></span>Checking pool size…`,
        "loading",
      );
    }
    try {
      const res = await fetch("/api/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(criteria),
      });
      if (seq !== previewSeq) return; // a newer request superseded this one
      const body = await res.json();
      if (!res.ok) {
        setPreview(body?.error || "Preview failed.", "empty");
        return;
      }
      const n = body.total_count || 0;
      if (n === 0) {
        const fallback = body.fallback_count || 0;
        const fallbackMode = body.fallback_mode || "similar";
        lastPreviewCount = 0;
        if (fallback > 0) {
          const formatted = fallback.toLocaleString();
          const modeLabel = fallbackMode.charAt(0).toUpperCase() + fallbackMode.slice(1);
          setPreview(
            `~<span class="preview-count__big">0</span> candidates — switch to <strong>${modeLabel}</strong> for ~${formatted}`,
            "red",
          );
        } else {
          setPreview("~0 candidates — loosen a filter.", "red");
        }
      } else {
        lastPreviewCount = n;
        const formatted = n.toLocaleString();
        setPreview(`~<span class="preview-count__big">${formatted}</span> candidates`, previewTone(n));
      }
    } catch (err) {
      if (seq !== previewSeq) return;
      setPreview(`Preview failed: ${err.message || err}`, "empty");
    }
  }

  function schedulePreview() {
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(runPreview, 300);
  }

  // Trigger preview on any form change. `input` covers text, number, select.
  form.addEventListener("input", schedulePreview);
  form.addEventListener("change", schedulePreview);

  // ---------- Mode toggle ----------
  const modeButtons = $$(".mode-toggle__option");

  function setMode(next) {
    if (!next || next === currentMode) return;
    if (!["exact", "similar", "broad"].includes(next)) return;
    currentMode = next;
    modeButtons.forEach((b) => {
      const active = b.dataset.mode === currentMode;
      b.classList.toggle("is-active", active);
      b.setAttribute("aria-checked", active ? "true" : "false");
    });
    // Preview reflects the active mode too — re-run it on the new operator.
    schedulePreview();
    // Title disclosure also depends on mode — refresh the "Also matching"
    // line so Similar/Broad's wider variants surface immediately.
    refreshTitleVariants();
  }

  modeButtons.forEach((btn) => {
    btn.addEventListener("click", () => setMode(btn.dataset.mode));
  });

  // ---------- Title-variant disclosure ----------
  const titleInput = form.querySelector('input[name="current_title"]');
  const variantsEl = $("[data-title-variants]");

  async function refreshTitleVariants() {
    if (!titleInput || !variantsEl) return;
    const val = (titleInput.value || "").trim();
    if (!val) {
      variantsEl.hidden = true;
      variantsEl.textContent = "";
      return;
    }
    try {
      const res = await fetch(
        `/api/title-variants?title=${encodeURIComponent(val)}&mode=${encodeURIComponent(currentMode)}`,
      );
      if (!res.ok) {
        variantsEl.hidden = true;
        return;
      }
      const body = await res.json();
      const variants = Array.isArray(body.variants) ? body.variants : [];
      if (!variants.length) {
        variantsEl.hidden = true;
        variantsEl.textContent = "";
        return;
      }
      const shown = variants.slice(0, 2).join(", ");
      const extra = variants.length > 2 ? ` (+${variants.length - 2} more)` : "";
      variantsEl.hidden = false;
      variantsEl.textContent = `Also matching: ${shown}${extra}`;
    } catch {
      variantsEl.hidden = true;
    }
  }

  if (titleInput) {
    titleInput.addEventListener("blur", refreshTitleVariants);
  }

  // ---------- Join-Croot modal ----------
  const PERSONAL_EMAIL_DOMAINS = new Set([
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "aol.com", "proton.me", "protonmail.com",
    "live.com", "msn.com", "me.com", "mac.com", "fastmail.com",
    "yandex.com", "zoho.com",
  ]);

  const modalEl = $("#waitlist-modal");
  const waitlistForm = $("#waitlist-form");
  const waitlistStatus = $("#waitlist-status");
  const waitlistFormEl = modalEl.querySelector("[data-waitlist-form]");
  const waitlistSuccessEl = modalEl.querySelector("[data-waitlist-success]");
  const waitlistSubmit = $("#waitlist-submit");
  const submitLabel = submitBtn.querySelector(".primary__label");
  const searchesCounter = $("#searches-counter");

  // Holds whether the user opened the modal mid-submit, so we can re-fire
  // their search the moment they unlock instead of asking them to click again.
  let modalPendingSearch = false;

  function openWaitlistModal(opts = {}) {
    modalPendingSearch = !!opts.pendingSearch;
    waitlistFormEl.hidden = false;
    waitlistSuccessEl.hidden = true;
    modalEl.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    setTimeout(() => {
      const firstInput = waitlistForm.querySelector('input[name="name"]');
      if (firstInput) firstInput.focus();
    }, 80);
  }

  function closeWaitlistModal() {
    modalEl.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
  }

  function showWaitlistSuccess() {
    waitlistFormEl.hidden = true;
    waitlistSuccessEl.hidden = false;
  }

  modalEl.addEventListener("click", (e) => {
    if (e.target.matches("[data-modal-close]")) closeWaitlistModal();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (modalEl.getAttribute("aria-hidden") === "false") closeWaitlistModal();
    if (panelEl.getAttribute("aria-hidden") === "false") closeProfilePanel();
  });

  function setWaitlistStatus(text, tone = "") {
    waitlistStatus.textContent = text;
    if (tone) waitlistStatus.dataset.tone = tone;
    else delete waitlistStatus.dataset.tone;
  }

  // Soft work-email nudge — visible as the user types. Doesn't block submit.
  const emailInput = waitlistForm.querySelector('input[name="email"]');
  const emailHint = $("#waitlist-email-hint");
  function updateEmailHint() {
    const v = (emailInput.value || "").trim().toLowerCase();
    if (!v || !v.includes("@")) {
      if (emailHint) emailHint.hidden = true;
      return;
    }
    const domain = v.split("@")[1] || "";
    if (emailHint) {
      if (PERSONAL_EMAIL_DOMAINS.has(domain)) {
        emailHint.hidden = false;
        emailHint.textContent =
          "Tip: a work email helps us route you to early access faster — but personal is welcome too.";
      } else {
        emailHint.hidden = true;
      }
    }
  }
  if (emailInput) emailInput.addEventListener("input", updateEmailHint);

  waitlistForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(waitlistForm);
    const name = (fd.get("name") || "").toString().trim();
    const email = (fd.get("email") || "").toString().trim();
    if (!name) {
      setWaitlistStatus("Name is required.", "error");
      return;
    }
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
      setWaitlistStatus("Use a valid email.", "error");
      return;
    }
    waitlistSubmit.disabled = true;
    setWaitlistStatus("Setting you up…");
    try {
      const res = await fetch("/api/waitlist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, email }),
      });
      const body = await res.json();
      if (!res.ok) {
        setWaitlistStatus(body?.error || "Couldn’t submit. Try again.", "error");
        return;
      }
      access.unlock();
      refreshAccessUI();
      showWaitlistSuccess();

      // If the user opened the modal mid-search, re-fire it for them so they
      // don't have to click Search again.
      if (modalPendingSearch) {
        modalPendingSearch = false;
        setTimeout(() => {
          closeWaitlistModal();
          form.requestSubmit();
        }, 900);
      }
    } catch (err) {
      setWaitlistStatus(`Network error: ${err.message || err}`, "error");
    } finally {
      waitlistSubmit.disabled = false;
    }
  });

  function refreshAccessUI() {
    submitLabel.textContent = "Search Crustdata";
    submitBtn.disabled = false;

    if (access.isUnlocked()) {
      // Unlocked users: counter hidden.
      if (searchesCounter) searchesCounter.hidden = true;
      return;
    }
    const used = access.searchesUsed();
    const remaining = access.remaining();
    if (searchesCounter) {
      searchesCounter.hidden = false;
      searchesCounter.dataset.tone =
        remaining === 0 ? "out" : remaining === 1 ? "low" : "ok";
      if (remaining === 0) {
        searchesCounter.textContent = `${FREE_SEARCH_LIMIT} of ${FREE_SEARCH_LIMIT} free searches used — join Croot to keep going`;
      } else {
        searchesCounter.textContent = `${used} of ${FREE_SEARCH_LIMIT} free searches used`;
      }
    }
  }
  refreshAccessUI();

  // ---------- Profile slide-out panel ----------
  const panelEl = $("#profile-panel");
  const panelBody = panelEl.querySelector("[data-panel-body]");
  const panelLinkedin = panelEl.querySelector("[data-panel-linkedin]");
  const profileCacheMem = new Map();

  function openProfilePanel() {
    panelEl.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
  }

  function closeProfilePanel() {
    panelEl.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
  }

  panelEl.addEventListener("click", (e) => {
    if (e.target.matches("[data-panel-close]")) closeProfilePanel();
  });

  // Delegate card clicks. Skip clicks on the inline LinkedIn link so it
  // still works as a real anchor, and skip the per-card select checkbox and
  // copy-LinkedIn button so they don't open the profile panel.
  resultsList.addEventListener("click", async (e) => {
    if (e.target.closest(".card__link")) return;
    if (e.target.closest(".card__select")) return;
    if (e.target.closest("[data-copy-linkedin]")) return;
    const card = e.target.closest(".card");
    if (!card) return;
    const linkedin = card.dataset.linkedinUrl;
    if (!linkedin) {
      panelBody.innerHTML =
        '<p class="panel__error">No LinkedIn URL available for this candidate.</p>';
      panelLinkedin.hidden = true;
      openProfilePanel();
      return;
    }
    loadProfile(linkedin);
  });

  // ---------- Outreach Phase 1 — Copy LinkedIn + CSV export ----------
  const exportBar = $("#export-bar");
  const exportCountEl = $("[data-export-count]");
  const exportCsvBtn = $("#export-csv");
  const selectAllWrap = $("[data-results-select-all]");
  const selectAllBox = $("#results-select-all");
  const selectedLinkedins = new Set();

  function refreshExportUI() {
    const cards = resultsList.querySelectorAll(".card");
    const selectable = Array.from(cards).filter((c) => c.dataset.linkedinUrl);
    // Prune dropped LinkedIns from the selection set so a new search doesn't
    // carry over stale picks.
    const present = new Set(selectable.map((c) => c.dataset.linkedinUrl));
    for (const url of Array.from(selectedLinkedins)) {
      if (!present.has(url)) selectedLinkedins.delete(url);
    }
    // Sync checkboxes with the set.
    selectable.forEach((card) => {
      const cb = card.querySelector("[data-card-select]");
      if (cb) cb.checked = selectedLinkedins.has(card.dataset.linkedinUrl);
    });
    if (selectAllWrap) selectAllWrap.hidden = selectable.length === 0;
    if (selectAllBox) {
      selectAllBox.checked = selectable.length > 0 && selectedLinkedins.size === selectable.length;
      selectAllBox.indeterminate =
        selectedLinkedins.size > 0 && selectedLinkedins.size < selectable.length;
    }
    const n = selectedLinkedins.size;
    if (exportBar) exportBar.hidden = n === 0;
    if (exportCountEl) exportCountEl.textContent = `${n} selected`;
  }

  resultsList.addEventListener("change", (e) => {
    const cb = e.target.closest("[data-card-select]");
    if (!cb) return;
    const card = cb.closest(".card");
    const url = card && card.dataset.linkedinUrl;
    if (!url) return;
    if (cb.checked) selectedLinkedins.add(url);
    else selectedLinkedins.delete(url);
    refreshExportUI();
  });

  resultsList.addEventListener("click", (e) => {
    const copyBtn = e.target.closest("[data-copy-linkedin]");
    if (!copyBtn) return;
    e.preventDefault();
    e.stopPropagation();
    const card = copyBtn.closest(".card");
    const url = card && card.dataset.linkedinUrl;
    if (!url) return;
    const restore = copyBtn.getAttribute("title") || "";
    const flash = (text) => {
      copyBtn.setAttribute("title", text);
      copyBtn.classList.add("is-flash");
      setTimeout(() => {
        copyBtn.setAttribute("title", restore);
        copyBtn.classList.remove("is-flash");
      }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(
        () => flash("Copied"),
        () => flash("Copy failed"),
      );
    } else {
      flash("Clipboard unavailable");
    }
  });

  if (selectAllBox) {
    selectAllBox.addEventListener("change", () => {
      const cards = resultsList.querySelectorAll(".card");
      if (selectAllBox.checked) {
        cards.forEach((c) => {
          if (c.dataset.linkedinUrl) selectedLinkedins.add(c.dataset.linkedinUrl);
        });
      } else {
        selectedLinkedins.clear();
      }
      refreshExportUI();
    });
  }

  function csvEscape(value) {
    const s = value == null ? "" : String(value);
    if (s.includes(",") || s.includes("\n") || s.includes('"')) {
      return `"${s.replace(/"/g, '""')}"`;
    }
    return s;
  }

  if (exportCsvBtn) {
    exportCsvBtn.addEventListener("click", () => {
      const cards = Array.from(resultsList.querySelectorAll(".card"))
        .filter((c) => c.dataset.linkedinUrl && selectedLinkedins.has(c.dataset.linkedinUrl));
      if (!cards.length) return;
      const header = ["name", "current_title", "current_company", "linkedin_url"];
      const lines = [header.join(",")];
      for (const c of cards) {
        lines.push([
          csvEscape(c.dataset.name),
          csvEscape(c.dataset.title),
          csvEscape(c.dataset.company),
          csvEscape(c.dataset.linkedinUrl),
        ].join(","));
      }
      const csv = lines.join("\n");
      const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `croot-export-${new Date().toISOString().slice(0, 10)}.csv`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });
  }

  // Keyboard activation — Enter or Space on a focused card.
  resultsList.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const card = e.target.closest(".card");
    if (!card) return;
    e.preventDefault();
    card.click();
  });

  async function loadProfile(linkedinUrl) {
    panelLinkedin.href = linkedinUrl;
    panelLinkedin.hidden = false;
    panelBody.innerHTML = '<p class="panel__loading">Loading profile…</p>';
    openProfilePanel();

    if (profileCacheMem.has(linkedinUrl)) {
      renderProfile(profileCacheMem.get(linkedinUrl));
      return;
    }

    try {
      const res = await fetch(
        `/api/profile?linkedin_url=${encodeURIComponent(linkedinUrl)}`,
      );
      const body = await res.json();
      if (!res.ok) {
        panelBody.innerHTML = `<p class="panel__error">${
          escapeHtml(body?.error || "Couldn’t load profile.")
        }</p>`;
        return;
      }
      profileCacheMem.set(linkedinUrl, body.profile);
      renderProfile(body.profile);
    } catch (err) {
      panelBody.innerHTML = `<p class="panel__error">Network error: ${
        escapeHtml(err.message || String(err))
      }</p>`;
    }
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  }

  function fmtDate(d) {
    if (!d) return "";
    const m = /^(\d{4})-(\d{2})/.exec(d);
    if (!m) return "";
    const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    return `${months[parseInt(m[2], 10) - 1] || ""} ${m[1]}`.trim();
  }

  function fmtRange(start, end) {
    const s = fmtDate(start);
    const e = end ? fmtDate(end) : "Present";
    if (!s && !e) return "";
    return `${s || "?"} – ${e || "?"}`;
  }

  function combineEmployers(profile) {
    // Crustdata's enrich response splits current/past; some plans only return
    // `all_employers`. Merge into a single list and de-dupe by position_id.
    const seen = new Set();
    const out = [];
    const push = (arr) => {
      if (!Array.isArray(arr)) return;
      for (const e of arr) {
        const id = e.position_id || `${e.employer_name || e.name}-${e.start_date}`;
        if (seen.has(id)) continue;
        seen.add(id);
        out.push(e);
      }
    };
    push(profile.current_employers);
    push(profile.past_employers);
    push(profile.all_employers);
    // Sort by start_date desc.
    out.sort((a, b) => (b.start_date || "").localeCompare(a.start_date || ""));
    return out;
  }

  function employerName(e) {
    return e.employer_name || e.name || e.company_name || "";
  }

  function employerTitle(e) {
    return e.employee_title || e.title || "";
  }

  function employerLocation(e) {
    return e.employee_location || e.location || "";
  }

  function employerDescription(e) {
    return e.employee_description || e.description || "";
  }

  function renderProfile(p) {
    if (!p) {
      panelBody.innerHTML = '<p class="panel__error">No profile data.</p>';
      return;
    }
    const name = p.name || [p.first_name, p.last_name].filter(Boolean).join(" ") || "Unknown";
    const headline = p.headline || p.title || "";
    const region = p.region || p.location || "";
    const summary = p.summary || "";

    const employers = combineEmployers(p);
    const education = Array.isArray(p.education_background) ? p.education_background : [];
    const skills = Array.isArray(p.skills) ? p.skills.filter(Boolean) : [];
    const certifications = Array.isArray(p.certifications) ? p.certifications : [];
    const honors = Array.isArray(p.honors) ? p.honors : [];
    const businessEmails = Array.isArray(p.business_email)
      ? p.business_email.filter(Boolean)
      : (typeof p.business_email === "string" && p.business_email ? [p.business_email] : []);

    const parts = [];
    parts.push(`<h2 class="panel__name" id="panel-name">${escapeHtml(name)}</h2>`);
    if (headline) parts.push(`<p class="panel__headline">${escapeHtml(headline)}</p>`);
    if (region) parts.push(`<p class="panel__meta">${escapeHtml(region)}</p>`);

    if (summary) {
      parts.push(`
        <section class="panel__section">
          <h3 class="panel__section-title">About</h3>
          <p class="panel__summary">${escapeHtml(summary)}</p>
        </section>
      `);
    }

    if (employers.length) {
      const items = employers.map((e) => `
        <li class="timeline__item">
          <p class="timeline__role">${escapeHtml(employerTitle(e) || "Role")}</p>
          <p class="timeline__company">${escapeHtml(employerName(e) || "")}${
            employerLocation(e) ? ` · ${escapeHtml(employerLocation(e))}` : ""
          }</p>
          <p class="timeline__dates">${escapeHtml(fmtRange(e.start_date, e.end_date))}</p>
          ${employerDescription(e) ? `<p class="timeline__desc">${escapeHtml(employerDescription(e))}</p>` : ""}
        </li>
      `).join("");
      parts.push(`
        <section class="panel__section">
          <h3 class="panel__section-title">Experience</h3>
          <ol class="timeline">${items}</ol>
        </section>
      `);
    }

    if (education.length) {
      const items = education.map((ed) => {
        const inst = ed.institute_name || ed.school || "";
        const degree = ed.degree_name || "";
        const field = ed.field_of_study || "";
        const sub = [degree, field].filter(Boolean).join(" · ");
        const dates = fmtRange(ed.start_date, ed.end_date);
        return `
          <li class="kv-list__row">
            <span class="kv-list__row-title">${escapeHtml(inst)}</span>
            ${sub ? `<span class="kv-list__row-sub">${escapeHtml(sub)}</span>` : ""}
            ${dates ? `<span class="kv-list__row-sub">${escapeHtml(dates)}</span>` : ""}
          </li>
        `;
      }).join("");
      parts.push(`
        <section class="panel__section">
          <h3 class="panel__section-title">Education</h3>
          <ul class="kv-list">${items}</ul>
        </section>
      `);
    }

    if (skills.length) {
      const items = skills.slice(0, 40).map((s) => `<li>${escapeHtml(s)}</li>`).join("");
      parts.push(`
        <section class="panel__section">
          <h3 class="panel__section-title">Skills</h3>
          <ul class="tag-list">${items}</ul>
        </section>
      `);
    }

    if (certifications.length) {
      const items = certifications.map((c) => {
        const title = c.name || c.title || "";
        const issuer = c.issuer || c.issuer_organization || "";
        const issued = fmtDate(c.issued_date || c.date_issued || "");
        const sub = [issuer, issued].filter(Boolean).join(" · ");
        return `
          <li class="kv-list__row">
            <span class="kv-list__row-title">${escapeHtml(title)}</span>
            ${sub ? `<span class="kv-list__row-sub">${escapeHtml(sub)}</span>` : ""}
          </li>
        `;
      }).join("");
      parts.push(`
        <section class="panel__section">
          <h3 class="panel__section-title">Certifications</h3>
          <ul class="kv-list">${items}</ul>
        </section>
      `);
    }

    if (honors.length) {
      const items = honors.map((h) => {
        const title = h.title || h.name || "";
        const issuer = h.issuer || h.issuer_organization || "";
        const sub = [issuer, fmtDate(h.issued_date || "")].filter(Boolean).join(" · ");
        return `
          <li class="kv-list__row">
            <span class="kv-list__row-title">${escapeHtml(title)}</span>
            ${sub ? `<span class="kv-list__row-sub">${escapeHtml(sub)}</span>` : ""}
          </li>
        `;
      }).join("");
      parts.push(`
        <section class="panel__section">
          <h3 class="panel__section-title">Honors</h3>
          <ul class="kv-list">${items}</ul>
        </section>
      `);
    }

    if (businessEmails.length) {
      const items = businessEmails.map((e) =>
        `<a class="email-link" href="mailto:${escapeHtml(e)}">${escapeHtml(e)}</a>`,
      ).join(" ");
      parts.push(`
        <section class="panel__section">
          <h3 class="panel__section-title">Business email</h3>
          <div>${items}</div>
        </section>
      `);
    }

    panelBody.innerHTML = parts.join("");
  }

  // ---------- Saved searches ----------
  const saveSearchWrap = $("[data-save-search]");
  const saveSearchOpen = $("[data-save-search-open]");
  const saveSearchForm = $("[data-save-search-form]");
  const saveSearchInput = $("[data-save-search-name]");
  const saveSearchCancel = $("[data-save-search-cancel]");
  const savedPanel = $("#saved-searches-panel");
  const savedList = $("#saved-searches-list");
  const savedCountEl = $("[data-saved-count]");

  function revealSaveSearch() {
    if (saveSearchWrap) saveSearchWrap.hidden = false;
  }
  function resetSaveSearchPrompt() {
    if (saveSearchOpen) saveSearchOpen.hidden = false;
    if (saveSearchForm) saveSearchForm.hidden = true;
    if (saveSearchInput) saveSearchInput.value = "";
  }
  if (saveSearchOpen) {
    saveSearchOpen.addEventListener("click", () => {
      saveSearchOpen.hidden = true;
      if (saveSearchForm) {
        saveSearchForm.hidden = false;
        saveSearchInput.focus();
      }
    });
  }
  if (saveSearchCancel) {
    saveSearchCancel.addEventListener("click", resetSaveSearchPrompt);
  }
  if (saveSearchForm) {
    saveSearchForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const name = (saveSearchInput.value || "").trim();
      if (!name) {
        saveSearchInput.focus();
        return;
      }
      const criteria = collectCriteria();
      try {
        const res = await fetch("/api/saved-searches", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, criteria, mode: currentMode }),
        });
        const body = await res.json();
        if (!res.ok) {
          setStatus(body?.error || "Couldn’t save.", "error");
          return;
        }
        setStatus(`Saved "${body.name}".`, "ok");
        resetSaveSearchPrompt();
        loadSavedSearches();
      } catch (err) {
        setStatus(`Network error: ${err.message || err}`, "error");
      }
    });
  }

  async function loadSavedSearches() {
    if (!savedList) return;
    try {
      const res = await fetch("/api/saved-searches");
      if (!res.ok) return;
      const rows = await res.json();
      savedList.innerHTML = "";
      if (savedCountEl) {
        savedCountEl.textContent = rows.length ? `${rows.length} saved` : "none yet";
      }
      if (!rows.length) {
        const li = document.createElement("li");
        li.className = "saved-searches__empty";
        li.textContent = "Save a search above to see it here.";
        savedList.appendChild(li);
        return;
      }
      for (const row of rows) {
        const li = document.createElement("li");
        li.className = "saved-search";
        li.dataset.savedId = row.id;
        const name = document.createElement("span");
        name.className = "saved-search__name";
        name.textContent = row.name;
        const modeBadge = document.createElement("span");
        modeBadge.className = "saved-search__mode";
        modeBadge.textContent = row.mode || "similar";
        const runBtn = document.createElement("button");
        runBtn.type = "button";
        runBtn.className = "saved-search__run";
        runBtn.textContent = "Run";
        runBtn.addEventListener("click", () => runSavedSearch(row));
        const delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.className = "saved-search__delete";
        delBtn.setAttribute("aria-label", `Delete saved search "${row.name}"`);
        delBtn.textContent = "×";
        delBtn.addEventListener("click", () => deleteSavedSearch(row.id, li));
        li.append(name, modeBadge, runBtn, delBtn);
        savedList.appendChild(li);
      }
    } catch {
      /* silent */
    }
  }

  function runSavedSearch(row) {
    if (!row || !row.criteria) return;
    applyCriteria(row.criteria);
    if (row.mode) setMode(row.mode);
    scrollToCompose();
    form.requestSubmit();
  }

  async function deleteSavedSearch(id, listItem) {
    if (!id) return;
    try {
      const res = await fetch(`/api/saved-searches/${id}`, { method: "DELETE" });
      if (!res.ok) return;
      if (listItem && listItem.parentNode) listItem.parentNode.removeChild(listItem);
      loadSavedSearches();
    } catch {
      /* silent */
    }
  }

  // Lazily fetch the list the first time the panel is opened; refresh on
  // every subsequent open so the count stays current.
  if (savedPanel) {
    savedPanel.addEventListener("toggle", () => {
      if (savedPanel.open) loadSavedSearches();
    });
    // Prime the count badge so the user sees "(none yet)" / "(3 saved)" even
    // before they expand the panel.
    loadSavedSearches();
  }
})();
