"""
MetadataManager/OKFStore.py — Open Knowledge Format (OKF) concepts for table search

Loads an OKF bundle (a directory of Markdown concept files with YAML
frontmatter, spec v0.2), embeds each concept into the 'okf_concepts'
ChromaDB collection, and at query time maps the concepts that match the
user's question to PolyQL tables.

A concept points at tables in two ways:
  - it IS a table concept (its `type` mentions "table" or "view"); the table
    name comes from a `table` / `table_name` frontmatter key, the `resource`
    URI, the title or the file name;
  - it LINKS to table concepts (e.g. a Metric whose body says
    "Joins on [orders](/tables/orders.md)"); one hop is followed.

Candidate names are stored at import time and resolved against the tables
PolyQL knows at query time, so tables ingested after the bundle still match.

Parsing is dependency-free (PyYAML only). Indexing and search take the
embedding model and ChromaDB client from the caller, so the table-search
step reuses the ones it already has loaded.
"""

import logging
import posixpath
import re
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

logger = logging.getLogger(__name__)

# ChromaDB collection name for OKF concept embeddings
_COLLECTION = "okf_concepts"

# OKF §3.1 reserved file names — never concept documents
_RESERVED_FILES = {"index.md", "log.md"}

# Characters of the concept body included in the embedded document
_BODY_CHARS = 2000

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_LINK_RE = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")


# ---------------------------------------------------------------------------
# Bundle parsing
# ---------------------------------------------------------------------------

def _concept_id(path: Path, root: Path) -> str:
    """Concept ID per OKF §3: bundle-relative path without '.md', e.g. '/tables/orders'."""
    rel = path.relative_to(root).as_posix()
    return "/" + rel[:-3] if rel.endswith(".md") else "/" + rel


def _extract_links(body: str, concept_id: str) -> list:
    """Return the concept IDs this body links to (external, anchor and non-.md links skipped)."""
    links = []
    base_dir = posixpath.dirname(concept_id)
    for target in _LINK_RE.findall(body):
        target = target.split("#", 1)[0]
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        if not target.endswith(".md"):
            continue
        resolved = target if target.startswith("/") else posixpath.join(base_dir, target)
        resolved = posixpath.normpath(resolved)[:-3]
        if resolved not in links:
            links.append(resolved)
    return links


def parse_concept(text: str, concept_id: str) -> dict | None:
    """
    Parse one OKF concept document.

    Returns None when the document does not conform (no frontmatter, YAML
    that does not parse to a mapping, or an empty `type`).
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict) or not str(meta.get("type") or "").strip():
        return None

    body = text[match.end():].strip()
    tags = meta.get("tags") or []
    if not isinstance(tags, list):
        tags = [tags]

    return {
        "concept_id":  concept_id,
        "type":        str(meta["type"]).strip(),
        "title":       str(meta.get("title") or posixpath.basename(concept_id)).strip(),
        "description": str(meta.get("description") or "").strip(),
        "resource":    str(meta.get("resource") or "").strip(),
        "tags":        [str(t) for t in tags],
        "status":      str(meta.get("status") or "stable").strip().lower(),
        "table_name":  str(meta.get("table") or meta.get("table_name") or "").strip(),
        "body":        body,
        "links":       _extract_links(body, concept_id),
    }


def load_bundle(bundle_path: str) -> tuple:
    """
    Load every concept in the bundle at *bundle_path*.

    Reserved files (index.md, log.md) and hidden directories (e.g. .git) are
    skipped. Non-conforming documents are skipped and reported.

    Returns:
        (concepts, skipped) — list of concept dicts, list of skipped concept IDs.
    """
    root = Path(bundle_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"OKF bundle path is not a directory: {bundle_path}")

    concepts, skipped = [], []
    for path in sorted(root.rglob("*.md")):
        rel_parts = path.relative_to(root).parts
        if path.name.lower() in _RESERVED_FILES or any(p.startswith(".") for p in rel_parts):
            continue
        cid = _concept_id(path, root)
        try:
            concept = parse_concept(path.read_text(encoding="utf-8-sig"), cid)
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("OKFStore: could not read %s — %s", path, exc)
            concept = None
        if concept is None:
            skipped.append(cid)
        else:
            concepts.append(concept)
    return concepts, skipped


# ---------------------------------------------------------------------------
# Concept → table mapping
# ---------------------------------------------------------------------------

def is_table_concept(concept: dict) -> bool:
    """True when the concept describes a table or view (e.g. 'BigQuery Table', 'Table', 'View')."""
    kind = concept.get("type", "").lower()
    return "table" in kind or "view" in kind


def _normalize_name(name: str) -> str:
    name = name.strip().strip("`\"'[]").lower()
    return re.sub(r"[\s\-]+", "_", name)


def _names_from_resource(resource: str) -> list:
    """Table names a resource URI may carry: ?t= / ?table= params, then the last path segment."""
    if not resource:
        return []
    parsed = urlparse(resource)
    names = []
    query = parse_qs(parsed.query)
    for key in ("table", "t"):
        names.extend(query.get(key, []))
    segment = (parsed.path or "").rstrip("/").rsplit("/", 1)[-1]
    if segment and not segment.endswith(".md"):
        names.append(segment)
    return names


def table_candidates(concept: dict) -> list:
    """Normalised candidate table names for a table concept (most specific first)."""
    if not is_table_concept(concept):
        return []
    raw = [concept.get("table_name", "")]
    raw += _names_from_resource(concept.get("resource", ""))
    raw += [concept.get("title", ""), posixpath.basename(concept["concept_id"])]
    names = []
    for name in raw:
        norm = _normalize_name(name) if name else ""
        if norm and norm not in names:
            names.append(norm)
    return names


def concept_table_candidates(concept: dict, by_id: dict) -> list:
    """Candidates from the concept itself plus the table concepts it links to (one hop)."""
    names = list(table_candidates(concept))
    for link in concept.get("links", []):
        linked = by_id.get(link)
        if linked:
            names += [n for n in table_candidates(linked) if n not in names]
    return names


def resolve_tables(candidates: list, known_tables: list) -> list:
    """
    Map candidate names to tables PolyQL knows about.

    A candidate matches a known table exactly (case-insensitive), or by the
    last dotted segment so 'orders' matches 'sales.public.orders' and
    'project.dataset.orders' matches 'orders'. Unmatched candidates are dropped.
    """
    exact = {t.lower(): t for t in known_tables}
    by_short = {}
    for t in known_tables:
        by_short.setdefault(t.lower().rsplit(".", 1)[-1], t)

    resolved = []
    for cand in candidates:
        cand = cand.lower()
        table = exact.get(cand) or by_short.get(cand) or by_short.get(cand.rsplit(".", 1)[-1])
        if table and table not in resolved:
            resolved.append(table)
    return resolved


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def _build_embed_document(concept: dict) -> str:
    parts = [concept["title"], concept["type"]]
    if concept["description"]:
        parts.append(concept["description"])
    if concept["tags"]:
        parts.append("Tags: " + ", ".join(concept["tags"]))
    if concept["body"]:
        parts.append(concept["body"][:_BODY_CHARS])
    return "\n".join(parts)


def import_bundle(
    bundle_path: str,
    embedding_model,
    chroma_client,
    instance_name: str = "default",
) -> dict:
    """
    Embed every active concept of an OKF bundle into the 'okf_concepts' collection.

    Concepts with status 'deprecated' are not indexed. Re-importing a bundle
    upserts concepts by (instance_name, concept_id).

    Returns:
        dict: {indexed, skipped, deprecated, table_concepts}
    """
    from MetadataManager.MetadataStore.vdb.Chroma import addData

    concepts, skipped = load_bundle(bundle_path)
    by_id = {c["concept_id"]: c for c in concepts}

    data, deprecated, table_concepts = [], 0, 0
    for concept in concepts:
        if concept["status"] == "deprecated":
            deprecated += 1
            continue
        table_concepts += is_table_concept(concept)
        document = _build_embed_document(concept)
        data.append({
            "documents": document,
            "embedding": embedding_model.encode(document).tolist(),
            "metadata": {
                "concept_id":    concept["concept_id"],
                "type":          concept["type"],
                "title":         concept["title"],
                "description":   concept["description"][:500],
                # ChromaDB metadata values must be scalars
                "tables":        ",".join(concept_table_candidates(concept, by_id)),
                "instance_name": instance_name,
            },
            "id": str(uuid.uuid3(
                uuid.NAMESPACE_URL, f"okf:{instance_name}:{concept['concept_id']}"
            )),
        })

    if data:
        addData(chroma_client, data, {"collection_name": _COLLECTION, "sim_metric": "cosine"})

    logger.info(
        "OKFStore: imported %s into instance %r — %d indexed, %d skipped, %d deprecated",
        bundle_path, instance_name, len(data), len(skipped), deprecated,
    )
    return {
        "indexed": len(data),
        "skipped": skipped,
        "deprecated": deprecated,
        "table_concepts": table_concepts,
    }


# ---------------------------------------------------------------------------
# Query-time table search
# ---------------------------------------------------------------------------

def get_okf_tables(
    query: str,
    known_tables: list,
    embedding_model,
    chroma_client,
    instance_name: str = "default",
    top_k: int = 3,
    max_distance: float = 0.5,
) -> list:
    """
    Find tables that OKF concepts relevant to *query* point at.

    Returns a list of {table, concept_id, title, type, description, distance}
    ordered by concept distance, one entry per table (best concept wins).
    Falls back to [] on any error (no bundle imported, collection missing, etc.).
    """
    from MetadataManager.MetadataStore.vdb.Chroma import getData

    try:
        extra = {}
        if instance_name and instance_name != "default":
            extra["instance_name"] = instance_name
        raw = getData(
            chroma_client,
            embedding_model.encode(query).tolist(),
            {"collection_name": _COLLECTION, "n_chunks": top_k},
            **extra,
        )
    except Exception as exc:
        logger.debug("OKFStore.get_okf_tables: no OKF context — %s", exc)
        return []

    metadatas = (raw.get("metadatas") or [[]])[0]
    distances = (raw.get("distances") or [[]])[0]

    hits, seen = [], set()
    for meta, dist in sorted(zip(metadatas, distances, strict=False), key=lambda x: x[1]):
        if dist > max_distance:
            continue
        candidates = [c for c in (meta.get("tables") or "").split(",") if c]
        for table in resolve_tables(candidates, known_tables):
            if table in seen:
                continue
            seen.add(table)
            hits.append({
                "table":       table,
                "concept_id":  meta.get("concept_id", ""),
                "title":       meta.get("title", ""),
                "type":        meta.get("type", ""),
                "description": meta.get("description", ""),
                "distance":    dist,
            })
    return hits
