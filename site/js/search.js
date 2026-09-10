// Fuse.js setup + the combined search/filter/profile pipeline.
const AwardSearch = (() => {
  let fuse = null;
  let allAwards = [];

  function init(awards) {
    allAwards = awards;
    fuse = new Fuse(awards, {
      includeScore: false,
      threshold: 0.32,
      ignoreLocation: true,
      keys: [
        { name: "award_name", weight: 0.4 },
        { name: "award_description", weight: 0.2 },
        { name: "eligibility_selection_criteria", weight: 0.2 },
        { name: "area_of_study", weight: 0.15 },
        { name: "affiliation", weight: 0.05 },
      ],
    });
  }

  function textSearch(query) {
    const q = query.trim();
    if (!q) return allAwards;
    return fuse.search(q).map((r) => r.item);
  }

  function passesStructuredFilters(award, filters) {
    if (filters.career && award.career !== filters.career) return false;
    if (filters.level && !award.levels.includes(filters.level)) return false;
    if (filters.awardType && !award.award_types.includes(filters.awardType)) return false;
    if (filters.term && !award.terms.includes(filters.term)) return false;
    if (filters.affiliation && !award.affiliations.includes(filters.affiliation)) return false;
    if (filters.areaOfStudy && !award.areas_of_study.includes(filters.areaOfStudy)) return false;
    return true;
  }

  function run({ query, filters, profile, matchModeEnabled }) {
    let results = textSearch(query || "");
    results = results.filter((a) => passesStructuredFilters(a, filters || {}));
    if (matchModeEnabled && profile) {
      results = results.filter((a) => Profile.matchesAward(a, profile));
    }
    return results;
  }

  function uniqueValues(arrayKey) {
    const set = new Set();
    for (const award of allAwards) {
      for (const v of award[arrayKey] || []) set.add(v);
    }
    return Array.from(set).sort((a, b) => a.localeCompare(b));
  }

  return { init, run, uniqueValues };
})();
