#!/usr/bin/env python3
"""
Find Blackbaud Award Management ("AcademicWorks") tenants worth ingesting.

    python3 tools/probe_academicworks.py                 # the default slug list
    python3 tools/probe_academicworks.py ubc uvic mcgill # specific slugs
    python3 tools/probe_academicworks.py --count         # also count every award
    python3 tools/probe_academicworks.py --yaml          # emit registry stubs

WHY THIS EXISTS
One adapter covers every tenant, so each new one is a YAML file and nothing
else — the cheapest awards in the project by a wide margin. This probe found
six tenants that months of manual research had missed, four of them Alberta
institutions, in about four minutes.

WHY IT PROBES OVER HTTP AND NOT DNS
`*.academicworks.ca` and `*.academicworks.com` both answer for *every*
subdomain. A DNS sweep of 100 slugs resolved all 100, including a deliberate
nonsense control, so "the name resolves" says nothing at all. The real
signal is where the request lands: a live tenant serves /opportunities from its
own host and titles it "All Opportunities - <Institution>", while everything
else redirects to www.blackbaud.com.

CONTROLS is kept in the default run for that reason — if a nonsense slug ever
reports as a tenant, the discriminator has broken and every other row in the
output is suspect.

Stdlib only, like the rest of tools/.
"""
import argparse
import concurrent.futures
import re
import sys
import urllib.error
import urllib.request

UA = "uw-awards-search tenant probe (+https://github.com/Jordan-Leis/uw-awards-search)"
TIMEOUT = 30
NOT_A_TENANT_HOST = "www.blackbaud.com"

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_OPPORTUNITY = re.compile(r"/opportunities/(\d+)")
_TITLE_INSTITUTION = re.compile(r"Opportunities\s*-\s*(.+)\s*$", re.I)

#: Slugs that must NOT resolve to a tenant. Their whole job is to fail.
CONTROLS = ["redderrer", "zzzznotreal"]

#: Known-good tenants (measured), then plausible Canadian post-secondary slugs.
#: Being on this list is a guess; only the probe's output is evidence.
KNOWN = ["alberta", "umanitoba", "trentu", "usask", "concordia",
         "uleth", "mtroyal", "nait", "norquest", "uwinnipeg", "langara",
         "sfu", "selkirk"]

CANDIDATES = """
ualberta mcgill dal dalhousie uvic queensu queens ubc york carleton uottawa
ottawa laurier wlu waterloo uwaterloo western uwo mcmaster ryerson torontomu
utoronto toronto ucalgary calgary lethbridge unb stfx smu acadia mta mun
memorial upei stu brandon ubrandon uregina regina nipissing lakehead brocku
brock guelph uoguelph windsor uwindsor ontariotechu uoit algomau ocadu
lakeheadu unbc macewan mtroyalu ambrose burmanu concordiaab kingsu
tru ufv kpu langaracollege douglascollege capu vcc camosun selkirkcollege
jibc nvit coastmountain viu okanagan okanagancollege bcit
sait olds lakelandcollege keyano gprc nwpolytech portagecollege
niagaracollege senecacollege seneca humber georgian conestoga fanshawe
sheridan mohawk stclair durhamcollege loyalist cambrian canadorecollege
saskpolytech cumberlandcollege assiniboine rrc redrivercollege ucn
nscc holland nbcc ccnb cna keyin
""".split()

DEFAULT_SLUGS = CONTROLS + KNOWN + CANDIDATES


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.geturl(), r.read().decode("utf-8", "replace")


def page_title(html):
    m = _TITLE.search(html)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def probe(slug, per_page=5):
    """One tenant's verdict: (slug, is_tenant, host, institution, ids, note)."""
    url = f"https://{slug}.academicworks.ca/opportunities?per_page={per_page}&page=1"
    try:
        final, html = fetch(url)
    except urllib.error.HTTPError as e:
        return slug, False, "", "", 0, f"HTTP {e.code}"
    except Exception as e:                      # DNS, TLS, timeout, reset
        return slug, False, "", "", 0, type(e).__name__

    host = final.split("/")[2] if "//" in final else final
    title = page_title(html)
    if host == NOT_A_TENANT_HOST or not host.startswith(f"{slug}."):
        return slug, False, host, "", 0, "redirected off-host"

    ids = len(set(_OPPORTUNITY.findall(html)))
    m = _TITLE_INSTITUTION.search(title)
    institution = m.group(1).strip() if m else title
    # A real host serving no rows is still a real tenant -- SFU and Selkirk both
    # do this -- so it is reported rather than dropped. It just needs a look
    # before anyone writes a YAML entry for it.
    note = "" if ids else "no rows on page 1"
    return slug, True, host, institution, ids, note


def count_awards(slug, per_page=500, max_pages=60):
    """Total distinct opportunities, by paging the listing the way the adapter
    does. One request per 500 awards."""
    seen = set()
    for page in range(1, max_pages + 1):
        url = f"https://{slug}.academicworks.ca/opportunities?per_page={per_page}&page={page}"
        try:
            _, html = fetch(url)
        except Exception:
            break
        ids = set(_OPPORTUNITY.findall(html))
        if not ids or ids <= seen:
            # Some tenants clamp `page` instead of 404ing, so a page that adds
            # nothing new means the pager is looping, not that data remains.
            break
        seen |= ids
        if len(ids) < per_page:
            break
    return len(seen)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slugs", nargs="*", help="slugs to probe (default: built-in list)")
    ap.add_argument("--count", action="store_true",
                    help="count every award per tenant (slower: 1 request per 500)")
    ap.add_argument("--yaml", action="store_true",
                    help="print sources/*.yaml stubs for the tenants found")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    slugs = args.slugs or DEFAULT_SLUGS
    with concurrent.futures.ThreadPoolExecutor(args.workers) as ex:
        rows = list(ex.map(probe, slugs))

    tenants = [r for r in rows if r[1]]
    bad_controls = [r[0] for r in tenants if r[0] in CONTROLS]

    print(f"{'slug':16s} {'rows':>5s}  institution")
    print("-" * 64)
    for slug, ok, host, institution, ids, note in sorted(tenants):
        flag = "  <-- CONTROL, SHOULD NOT BE A TENANT" if slug in CONTROLS else ""
        suffix = f" ({note})" if note else ""
        print(f"{slug:16s} {ids:5d}  {institution}{suffix}{flag}")

    controls_probed = [r[0] for r in rows if r[0] in CONTROLS]
    real = [r for r in tenants if r[0] not in CONTROLS]
    print(f"\n{len(real)} tenant(s) of {len(slugs) - len(controls_probed)} candidate(s); "
          f"{len(controls_probed)} control(s) correctly rejected."
          if not bad_controls else
          f"\n{len(real)} tenant(s) found, but the controls FAILED.")

    if bad_controls:
        print(f"\nDISCRIMINATOR BROKEN: control slug(s) {bad_controls} reported as "
              f"tenants. Every row above is suspect -- AcademicWorks has probably "
              f"changed how it handles unknown subdomains.", file=sys.stderr)
        return 1

    if args.count:
        print(f"\n{'slug':16s} {'awards':>7s}  institution")
        print("-" * 64)
        total = 0
        for slug, _, _, institution, _, _ in sorted(tenants):
            if slug in CONTROLS:
                continue
            n = count_awards(slug)
            total += n
            print(f"{slug:16s} {n:7d}  {institution}")
        print(f"{'TOTAL':16s} {total:7d}")

    if args.yaml:
        for slug, _, host, institution, _, _ in sorted(tenants):
            if slug in CONTROLS:
                continue
            print(f"""
# --- sources/{slug}.yaml ---
id: {slug}
name: {institution} Awards
status: planned  # promote to active only after a detail crawl
adapter: academicworks
academicworks_host: {host}
url: https://{host}/opportunities
source_url: https://{host}/opportunities
robots: {{checked: TODO, allowed: true, note: "verify with PoliteFetcher"}}
refresh: triannual
min_awards: TODO""".rstrip())

    return 0


if __name__ == "__main__":
    sys.exit(main())
