(function () {
  const PAGE_SIZE = 50;
  let visibleCount = PAGE_SIZE;
  let currentResults = [];

  const el = {
    searchInput: document.getElementById("search-input"),
    career: document.getElementById("filter-career"),
    level: document.getElementById("filter-level"),
    awardType: document.getElementById("filter-award-type"),
    term: document.getElementById("filter-term"),
    affiliation: document.getElementById("filter-affiliation"),
    areaOfStudy: document.getElementById("filter-area-of-study"),
    clearFilters: document.getElementById("clear-filters"),
    matchToggle: document.getElementById("match-mode-toggle"),
    editProfileBtn: document.getElementById("edit-profile-btn"),
    profileEditor: document.getElementById("profile-editor"),
    profileCareer: document.getElementById("profile-career"),
    profileLevel: document.getElementById("profile-level"),
    profileArea: document.getElementById("profile-area"),
    saveProfileBtn: document.getElementById("save-profile-btn"),
    resultSummary: document.getElementById("result-summary"),
    cardGrid: document.getElementById("card-grid"),
    loadMoreRow: document.getElementById("load-more-row"),
    loadMoreBtn: document.getElementById("load-more-btn"),
    modalBackdrop: document.getElementById("modal-backdrop"),
    modalBody: document.getElementById("modal-body"),
    freshnessNote: document.getElementById("freshness-note"),
  };

  function populateSelect(selectEl, values, placeholder) {
    selectEl.innerHTML = "";
    const optAll = document.createElement("option");
    optAll.value = "";
    optAll.textContent = placeholder;
    selectEl.appendChild(optAll);
    for (const v of values) {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      selectEl.appendChild(opt);
    }
  }

  function currentFilters() {
    return {
      career: el.career.value,
      level: el.level.value,
      awardType: el.awardType.value,
      term: el.term.value,
      affiliation: el.affiliation.value,
      areaOfStudy: el.areaOfStudy.value,
    };
  }

  function snippetFor(award) {
    const text = award.award_description || award.eligibility_selection_criteria || "";
    // Detail text is multi-line (list items become their own lines); flatten
    // it for the card preview so the clamped snippet reads as one paragraph.
    return text.replace(/\s*\n\s*/g, " ");
  }

  function renderResults(reset) {
    if (reset) visibleCount = PAGE_SIZE;
    const toShow = currentResults.slice(0, visibleCount);

    el.resultSummary.textContent = currentResults.length
      ? `${currentResults.length} award${currentResults.length === 1 ? "" : "s"} found`
      : "No awards match your search/filters.";

    el.cardGrid.innerHTML = "";
    if (!toShow.length) {
      el.loadMoreRow.hidden = true;
      return;
    }

    for (const award of toShow) {
      const card = document.createElement("article");
      card.className = "award-card";
      card.tabIndex = 0;
      card.setAttribute("role", "button");
      card.setAttribute("aria-label", `View details for ${award.award_name}`);

      const tags = [award.career, ...(award.levels || []).slice(0, 2), ...(award.terms || [])]
        .filter(Boolean);

      card.innerHTML = `
        <h3>${escapeHtml(award.award_name || "Untitled award")}</h3>
        <div class="tag-row">${tags.map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("")}</div>
        <p class="snippet">${escapeHtml(snippetFor(award))}</p>
      `;
      card.addEventListener("click", () => openModal(award));
      card.addEventListener("keypress", (e) => {
        if (e.key === "Enter" || e.key === " ") openModal(award);
      });
      el.cardGrid.appendChild(card);
    }

    el.loadMoreRow.hidden = visibleCount >= currentResults.length;
  }

  function escapeHtml(str) {
    return String(str || "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function fieldRow(label, value) {
    if (!value) return "";
    return `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`;
  }

  function openModal(award) {
    el.modalBody.innerHTML = `
      <div class="modal-header">
        <h2>${escapeHtml(award.award_name || "Untitled award")}</h2>
        <button class="modal-close" id="modal-close-x" aria-label="Close">&times;</button>
      </div>
      <div class="tag-row">
        ${[award.career, ...(award.levels || []), ...(award.terms || [])]
          .filter(Boolean).map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("")}
      </div>
      <dl>
        ${fieldRow("Award type", award.award_type)}
        ${fieldRow("Description", award.award_description)}
        ${fieldRow("Value", award.award_value_description)}
        ${fieldRow("Eligibility / selection criteria", award.eligibility_selection_criteria)}
        ${fieldRow("Area of study", award.area_of_study)}
        ${fieldRow("Affiliation", award.affiliation)}
        ${fieldRow("Application details", award.application_details)}
        ${fieldRow("Required supporting documents", award.required_supporting_documents)}
        ${fieldRow("Additional instructions", award.additional_instructions)}
        ${fieldRow("Contact", award.contact_detail)}
      </dl>
    `;
    document.getElementById("modal-close-x").addEventListener("click", closeModal);
    el.modalBackdrop.hidden = false;
    if (history.pushState) {
      history.pushState({ award: award.award_id }, "", `?award=${encodeURIComponent(award.award_id)}`);
    }
  }

  function closeModal(skipHistory) {
    el.modalBackdrop.hidden = true;
    if (!skipHistory && history.pushState) {
      history.pushState({}, "", window.location.pathname);
    }
  }

  function openAwardFromUrl(awards) {
    const params = new URLSearchParams(window.location.search);
    const id = params.get("award");
    if (!id) return;
    const award = awards.find((a) => a.award_id === id);
    if (award) openModal(award);
  }

  function applyFiltersAndSearch(reset) {
    const profile = Profile.get();
    currentResults = AwardSearch.run({
      query: el.searchInput.value,
      filters: currentFilters(),
      profile,
      matchModeEnabled: el.matchToggle.checked,
    });
    renderResults(reset !== false);
  }

  function debounce(fn, ms) {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), ms);
    };
  }

  function setupProfileEditor() {
    const profile = Profile.get();
    if (profile) {
      el.profileCareer.value = profile.career || "";
      el.profileLevel.value = profile.level || "";
      el.profileArea.value = (profile.areasOfStudy && profile.areasOfStudy[0]) || "";
    }
    el.matchToggle.checked = Profile.isMatchModeEnabled() && !!profile;

    el.editProfileBtn.addEventListener("click", () => {
      el.profileEditor.classList.toggle("open");
    });

    el.saveProfileBtn.addEventListener("click", () => {
      Profile.save({
        career: el.profileCareer.value,
        level: el.profileLevel.value,
        areasOfStudy: el.profileArea.value ? [el.profileArea.value] : [],
      });
      el.matchToggle.checked = true;
      Profile.setMatchModeEnabled(true);
      el.profileEditor.classList.remove("open");
      applyFiltersAndSearch(true);
    });

    el.matchToggle.addEventListener("change", () => {
      Profile.setMatchModeEnabled(el.matchToggle.checked);
      if (el.matchToggle.checked && !Profile.get()) {
        el.profileEditor.classList.add("open");
      }
      applyFiltersAndSearch(true);
    });
  }

  async function main() {
    let awards, meta;
    try {
      [awards, meta] = await Promise.all([
        fetch("data/awards.json").then((r) => r.json()),
        fetch("data/meta.json").then((r) => r.json()),
      ]);
    } catch (e) {
      el.resultSummary.textContent = "Couldn't load award data. Please try refreshing the page.";
      return;
    }

    AwardSearch.init(awards);

    const careerValues = [...new Set(awards.map((a) => a.career).filter(Boolean))].sort();
    populateSelect(el.career, careerValues, "All careers");
    populateSelect(el.level, AwardSearch.uniqueValues("levels"), "All levels");
    populateSelect(el.awardType, AwardSearch.uniqueValues("award_types"), "All award types");
    populateSelect(el.term, AwardSearch.uniqueValues("terms"), "All terms");
    populateSelect(el.affiliation, AwardSearch.uniqueValues("affiliations"), "All affiliations");
    populateSelect(el.areaOfStudy, AwardSearch.uniqueValues("areas_of_study"), "All areas of study");

    populateSelect(el.profileCareer, careerValues, "No preference");
    populateSelect(el.profileLevel, AwardSearch.uniqueValues("levels"), "No preference");
    populateSelect(el.profileArea, AwardSearch.uniqueValues("areas_of_study"), "No preference");

    setupProfileEditor();

    el.searchInput.addEventListener("input", debounce(() => applyFiltersAndSearch(true), 150));
    [el.career, el.level, el.awardType, el.term, el.affiliation, el.areaOfStudy].forEach((sel) => {
      sel.addEventListener("change", () => applyFiltersAndSearch(true));
    });
    el.clearFilters.addEventListener("click", () => {
      el.searchInput.value = "";
      [el.career, el.level, el.awardType, el.term, el.affiliation, el.areaOfStudy].forEach((sel) => sel.value = "");
      applyFiltersAndSearch(true);
    });
    el.loadMoreBtn.addEventListener("click", () => {
      visibleCount += PAGE_SIZE;
      renderResults(false);
    });
    el.modalBackdrop.addEventListener("click", (e) => {
      if (e.target === el.modalBackdrop) closeModal();
    });
    window.addEventListener("popstate", () => {
      const params = new URLSearchParams(window.location.search);
      if (!params.get("award")) closeModal(true);
    });

    if (meta && meta.last_updated) {
      el.freshnessNote.textContent = `Data last refreshed: ${meta.last_updated} · ${meta.total_awards} awards indexed`;
    }

    applyFiltersAndSearch(true);
    openAwardFromUrl(awards);
  }

  main();
})();
