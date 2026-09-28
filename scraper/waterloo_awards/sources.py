"""
Source registry.

Every source the database ingests — and every source it deliberately does
NOT ingest — is declared as one YAML file under sources/. This module loads
them at build time and compiles them into site/data/sources.json, which is
the runtime contract for the frontend and tools/find_awards.py. Nothing
downstream of export_data.py parses YAML, so those consumers stay
dependency-free.

Blocked sources are first-class registry entries rather than deletions. A
source we skipped for robots.txt reasons is a decision worth keeping visible:
it stays listed, with the reason, so the UI can tell a student "also check
these portals by hand" and so the call can be revisited without redoing the
research.
"""
from pathlib import Path

import yaml

SOURCES_DIR = Path(__file__).parent.parent.parent / "sources"

VALID_STATUSES = {"active", "blocked", "planned"}

#: How a source's awards reach the browser.
#:   core   in data/awards.json, fetched on every page load
#:   shard  in data/sources/<id>.json, fetched only when the visitor asks
#: Sharding exists because awards.json costs ~2.4 KB per award and the whole
#: file is fetched on load: the AcademicWorks estate alone would take it past
#: 25 MB. It is also the semantically right split -- an award restricted to one
#: institution's students is only relevant to them, so relevance gating and
#: load gating are the same gate.
VALID_DELIVERY = {"core", "shard"}

#: Deliberately "shard". A source that forgets to declare this stays out of the
#: always-loaded payload, so the failure mode of forgetting is a source nobody
#: sees until they ask for it -- not a 25 MB first paint for every visitor.
DEFAULT_DELIVERY = "shard"

# Fields every entry must carry, whatever its status.
REQUIRED_FIELDS = ("id", "name", "status")


class SourceRegistryError(Exception):
    """Raised for a malformed registry. Always fatal — a silently dropped or
    mis-declared source is exactly the failure this registry exists to prevent."""


def _validate(entry, path):
    for field in REQUIRED_FIELDS:
        if not entry.get(field):
            raise SourceRegistryError(f"{path.name}: missing required field '{field}'")

    status = entry["status"]
    if status not in VALID_STATUSES:
        raise SourceRegistryError(
            f"{path.name}: status '{status}' is not one of {sorted(VALID_STATUSES)}"
        )

    delivery = entry.get("delivery", DEFAULT_DELIVERY)
    if delivery not in VALID_DELIVERY:
        raise SourceRegistryError(
            f"{path.name}: delivery '{delivery}' is not one of {sorted(VALID_DELIVERY)}"
        )
    entry["delivery"] = delivery

    if status == "blocked" and not entry.get("blocked_reason"):
        raise SourceRegistryError(
            f"{path.name}: status is 'blocked' but no 'blocked_reason' given. "
            f"A source excluded without a recorded reason is indistinguishable "
            f"from one forgotten about."
        )

    if status == "active":
        if not entry.get("adapter"):
            raise SourceRegistryError(f"{path.name}: active source needs an 'adapter'")
        if not entry.get("min_awards"):
            raise SourceRegistryError(
                f"{path.name}: active source needs 'min_awards' — the publish gate "
                f"that stops a broken adapter quietly shrinking the corpus."
            )

    return entry


def load_all(sources_dir=SOURCES_DIR):
    """Every registry entry, keyed by id, in filename order."""
    if not sources_dir.is_dir():
        raise SourceRegistryError(f"No source registry directory at {sources_dir}")

    registry = {}
    for path in sorted(sources_dir.rglob("*.yaml")):
        with open(path, encoding="utf-8") as f:
            entry = yaml.safe_load(f)
        if not isinstance(entry, dict):
            raise SourceRegistryError(f"{path.name}: expected a YAML mapping")

        _validate(entry, path)
        source_id = entry["id"]
        if source_id in registry:
            raise SourceRegistryError(
                f"{path.name}: duplicate source id '{source_id}' "
                f"(already defined in {registry[source_id]['_path']})"
            )
        entry["_path"] = path.name
        registry[source_id] = entry

    if not registry:
        raise SourceRegistryError(f"Source registry at {sources_dir} is empty")

    return registry


def active(registry):
    return {sid: e for sid, e in registry.items() if e["status"] == "active"}


def blocked(registry):
    return {sid: e for sid, e in registry.items() if e["status"] == "blocked"}


def get(registry, source_id):
    try:
        return registry[source_id]
    except KeyError:
        raise SourceRegistryError(
            f"Unknown source id '{source_id}'. Known: {sorted(registry)}"
        ) from None


def to_public_json(registry):
    """The compiled runtime view written to site/data/sources.json.

    Drops build-only bookkeeping (_path) and keeps blocked entries, which the
    UI surfaces as 'check these manually'.
    """
    out = {}
    for source_id, entry in sorted(registry.items()):
        public = {
            "id": source_id,
            "name": entry["name"],
            "short_name": entry.get("short_name", entry["name"]),
            "status": entry["status"],
            "source_url": entry.get("source_url") or entry.get("url"),
            "delivery": entry["delivery"],
        }
        if entry["status"] == "blocked":
            public["blocked_reason"] = entry["blocked_reason"]
            public["manual_check_url"] = entry.get("manual_check_url") or public["source_url"]
        if entry.get("defaults"):
            public["defaults"] = entry["defaults"]
        if entry.get("disclaimer"):
            public["disclaimer"] = entry["disclaimer"]
        out[source_id] = public
    return out


def core_ids(registry):
    """Active sources whose awards ship in awards.json."""
    return {sid for sid, e in registry.items()
            if e["status"] == "active" and e["delivery"] == "core"}
