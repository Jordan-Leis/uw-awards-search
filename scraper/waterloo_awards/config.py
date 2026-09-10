ENTRY_PAGE_URL = "https://uwaterloo.ca/awards-directory/"

GUEST_TILE_URL = (
    "https://quest.pecs.uwaterloo.ca/psc/PB/ACADEMIC/SA/c/"
    "NUI_FRAMEWORK.PT_AGSTARTPAGE_NUI.GBL"
    "?CONTEXTIDPARAMS=TEMPLATE_ID:PTPPNAVCOL"
    "&scname=G_NCOL_FA_AWD_MANAGEMENT_PUB"
    "&PanelCollapsible=Y"
    "&PTPPB_GROUPLET_ID=G_TILE_FA_AWD_MANAGEMENT"
    "&CRefName=G_TILE_FA_AWD_MANAGEMENT"
    "&AJAXTransfer=Y&"
)

# Confirmed live: "Copy Award Link" on a detail page produces this exact
# deep-link format, keyed by the award's numeric UW_AWARD_ID (== the grid's
# "Award profile ID" column). It resolves directly, cold, no prior guest
# session bootstrap needed via a real browser (plain HTTP without JS hits a
# PeopleSoft sign-in redirect instead, so this must be fetched with Playwright).
AWARD_DEEPLINK_TEMPLATE = (
    "https://quest.pecs.uwaterloo.ca/psc/PB/ACADEMIC/SA/c/"
    "G_PUBLIC_FRAME.G_PUB_SS_SEARCH_FL.GBL?"
    "&G3SEARCHGRP=FA_AWD_MANAGEMENT"
    "&G3FORM_TYPE=FA_AWD_DIR"
    "&G3FORM_CONDITION=Default"
    "&G3FORM_TASK=ADD"
    "&UW_AWARD_ID={award_id}"
)

CAREER_CODES = {"GRD": "Graduate", "UG": "Undergraduate"}
LEVEL_CODES = {
    "GD": "Doctoral",
    "GMS": "Master's",
    "UEN": "UG Entering Year 1",
    "UY1": "UG Year 1",
    "UY2": "UG Year 2",
    "UY3": "UG Year 3",
    "UY4": "UG Year 4",
}
# Award type codes seen in the search form's dropdown options (used as a
# further split dimension if a Career x Level combo is still over the cap).
AWARD_TYPE_CODES = {
    "ASP": "Awards/Scholarships/Prizes",
    "FIN": "Bursaries/Financial need",
    "ENT": "Entrepreneurial",
    "INT": "International experience",
    "OTH": "Other experiential awards",
    "VAT": "Varsity/Athletics",
}
SELECTION_PROCESS_CODES = {
    "APPR": "Application required",
    "STSA": "Student selected automatically",
}

# Valid (Career, Level) pairs — Doctoral/Master's only exist under Graduate,
# the UG levels only exist under Undergraduate. Confirmed live: some single
# Career/Level combos alone still exceed ROW_CAP (e.g. UG+UY4 = 617 total),
# so the index enumeration further splits by Award type, then Selection
# process, when a combo comes back capped. Citizenship status and Affiliation
# were tested and rejected as split dimensions: awards with no value set for
# either are silently excluded when filtering on any single non-blank code
# (confirmed: Citizenship CP+ISP summed to 309 against a real total of 480
# for one combo — a ~35% gap from awards with no citizenship restriction).
CAREER_LEVEL_PAIRS = [
    ("GRD", "GD"), ("GRD", "GMS"),
    ("UG", "UEN"), ("UG", "UY1"), ("UG", "UY2"), ("UG", "UY3"), ("UG", "UY4"),
]

# The results grid appears to hard-truncate at this many rows per search
# (confirmed live: an unfiltered search reported bare "300 rows" with no
# "of possible Y" total, i.e. truncated). Any combo reporting a bare "N rows"
# reading at or above this needs to be split further.
ROW_CAP = 300

DB_PATH = "awards.db"
CSV_PATH = "awards.csv"
