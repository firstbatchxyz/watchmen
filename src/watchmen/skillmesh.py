"""Skill mesh + distillation planner.

This is the structural counterpart to ``watchmen prune``. Prune asks an LLM
judge which individual skills look stale or low-value. The distill pass inspects
the already-created skills, asks a semantic rubric whether candidate skills are
really mergeable, and proposes fewer shared replacement skills where overlap is
high.

The output is intentionally review-gated. ``build_distill_plan`` writes a JSON
plan; ``stage_distilled_candidates`` can create merged drafts under
``bundles/<project>/_pending/`` so the existing ``watchmen review`` flow remains
the approval gate.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from rich import box
from rich.align import Align
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from watchmen.paths import BUNDLES_DIR


_STOPWORDS = {
    "a",
    "about",
    "above",
    "after",
    "again",
    "all",
    "also",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "because",
    "before",
    "between",
    "but",
    "by",
    "can",
    "do",
    "does",
    "doing",
    "for",
    "from",
    "has",
    "have",
    "how",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "just",
    "like",
    "may",
    "more",
    "must",
    "need",
    "not",
    "of",
    "on",
    "once",
    "only",
    "or",
    "other",
    "out",
    "over",
    "read",
    "run",
    "same",
    "should",
    "so",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "this",
    "to",
    "use",
    "used",
    "user",
    "using",
    "when",
    "where",
    "which",
    "with",
    "without",
    "you",
    "your",
}

_MAX_DISTILL_CLUSTER_SIZE = 4
_MIN_CLUSTER_LINK_FRACTION = 0.66
_COHESION_MARGIN = 0.02
_SOURCE_SCOPES = {"metadata", "skill-md", "folder"}
_EXTRA_FILE_SUFFIXES = {
    ".md",
    ".rst",
    ".txt",
    ".py",
    ".sh",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
}
_CODE_FILE_SUFFIXES = {".py", ".sh", ".js", ".jsx", ".ts", ".tsx"}
_MAX_FOLDER_FILES = 24
_MAX_EXTRA_FILE_BYTES = 6_000


@dataclass
class SkillNode:
    slug: str
    name: str
    description: str
    when_to_use: list[str]
    when_not_to_use: list[str]
    token_count: int
    byte_size: int
    keywords: list[str]


@dataclass
class SkillEdge:
    a: str
    b: str
    similarity: float
    shared_keywords: list[str]
    a_only: list[str]
    b_only: list[str]
    semantic_judgment: SkillSimilarityJudgment | None = None


@dataclass
class SkillSimilarityJudgment:
    similarity: float
    relationship: str
    merge_decision: str
    trigger_overlap: int
    procedure_overlap: int
    boundary_compatibility: int
    context_rot_reduction: int
    risk: str
    rationale: str
    preserve: list[str]


@dataclass
class SkillCluster:
    id: str
    members: list[str]
    score: float
    shared_keywords: list[str]
    differences: dict[str, list[str]]
    proposed_slug: str
    proposed_description: str
    reduction_hint: str
    semantic_judgment: SkillSimilarityJudgment | None = None


@dataclass
class DistillPlan:
    project_key: str
    created_at: str
    skill_count: int
    edge_count: int
    cluster_count: int
    context_rot_score: int
    total_skill_bytes: int
    min_similarity: float
    source_scope: str
    nodes: list[SkillNode]
    edges: list[SkillEdge]
    clusters: list[SkillCluster]
    standalone: list[str]
    semantic_model: str | None = None
    semantic_judgments: list[SkillEdge] = field(default_factory=list)


@dataclass
class DistillApplyResult:
    promoted: list[str]
    archived_sources: list[str]
    blocklisted_sources: list[str]
    archive_dir: str | None
    audit_path: str | None


@dataclass
class _SemanticProgressState:
    accepted_edges: int = 0
    strongest_pair: tuple[str, str] | None = None
    strongest_score: float | None = None


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse the small YAML-ish frontmatter emitted in SKILL.md files."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    block = text[3:end]
    out: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[str] = []
    for raw in block.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", line)
        if match:
            if current_key and current_list:
                out[current_key] = current_list
            current_list = []
            key, value = match.group(1), match.group(2).strip()
            if value:
                if value.startswith("[") and value.endswith("]"):
                    inner = value[1:-1]
                    out[key] = [x.strip().strip("'\"") for x in inner.split(",") if x.strip()]
                else:
                    out[key] = value.strip("'\"")
                current_key = None
            else:
                current_key = key
            continue
        item = re.match(r"^\s*-\s+(.*)$", line)
        if item and current_key:
            current_list.append(item.group(1).strip())
    if current_key and current_list:
        out[current_key] = current_list
    return out


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return []
    lines = []
    for line in text.splitlines():
        cleaned = line.strip().lstrip("-").strip()
        if cleaned:
            lines.append(cleaned)
    return lines or [text]


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text.lower())
    cleaned = []
    for word in words:
        word = word.strip("-_")
        if not word or word in _STOPWORDS:
            continue
        cleaned.append(word)
    return cleaned


def _top_keywords(tokens: Iterable[str], limit: int = 12) -> list[str]:
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [word for word, _ in ranked[:limit]]


def _metadata_text(slug: str, fm: dict[str, Any]) -> str:
    parts = [
        slug.replace("-", " "),
        str(fm.get("name") or ""),
        str(fm.get("description") or ""),
        "\n".join(_as_list(fm.get("when_to_use") or fm.get("trigger_phrases"))),
        "\n".join(_as_list(fm.get("when_not_to_use"))),
    ]
    return "\n".join(parts)


def _skill_md_text(slug: str, body: str, fm: dict[str, Any]) -> str:
    parts = [_metadata_text(slug, fm)]
    # Body headings are useful signal, but including the whole body can make
    # every skill look similar because they share template section names.
    headings = re.findall(r"^#{1,4}\s+(.+)$", body, flags=re.MULTILINE)
    bullets = re.findall(r"^\s*-\s+(.+)$", body, flags=re.MULTILINE)
    parts.extend(headings)
    parts.extend(bullets[:30])
    return "\n".join(parts)


def _code_digest(text: str) -> str:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("#", "//", "/*", "*", '"""', "'''")):
            lines.append(line)
            continue
        match = re.match(
            r"^(?:async\s+def|def|class|function|const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)",
            line,
        )
        if match:
            lines.append(match.group(1))
    return "\n".join(lines[:80])


def _folder_digest_text(skill_dir: Path) -> str:
    chunks: list[str] = []
    files_seen = 0
    for path in sorted(skill_dir.rglob("*")):
        if files_seen >= _MAX_FOLDER_FILES:
            break
        if not path.is_file() or path.name == "SKILL.md":
            continue
        relative = path.relative_to(skill_dir)
        if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
            continue
        if path.suffix.lower() not in _EXTRA_FILE_SUFFIXES:
            continue
        files_seen += 1
        chunks.append(relative.as_posix().replace("/", " ").replace(".", " "))
        text = path.read_text(encoding="utf-8", errors="replace")[:_MAX_EXTRA_FILE_BYTES]
        if path.suffix.lower() in _CODE_FILE_SUFFIXES:
            chunks.append(_code_digest(text))
        else:
            headings = re.findall(r"^#{1,4}\s+(.+)$", text, flags=re.MULTILINE)
            bullets = re.findall(r"^\s*-\s+(.+)$", text, flags=re.MULTILINE)
            chunks.extend(headings[:20])
            chunks.extend(bullets[:30])
    return "\n".join(chunks)


def _node_text(slug: str, body: str, fm: dict[str, Any], skill_dir: Path, source_scope: str) -> str:
    if source_scope == "metadata":
        return _metadata_text(slug, fm)
    if source_scope == "skill-md":
        return _skill_md_text(slug, body, fm)
    if source_scope == "folder":
        return "\n".join([_skill_md_text(slug, body, fm), _folder_digest_text(skill_dir)])
    raise ValueError(f"unknown skill distill source scope: {source_scope}")


def load_skill_nodes(project_key: str, *, source_scope: str = "metadata") -> list[SkillNode]:
    """Read ``bundles/<project>/skills/*`` into comparable nodes."""
    if source_scope not in _SOURCE_SCOPES:
        allowed = ", ".join(sorted(_SOURCE_SCOPES))
        raise ValueError(f"unknown skill distill source scope: {source_scope}; choose one of: {allowed}")
    skills_dir = BUNDLES_DIR / project_key / "skills"
    if not skills_dir.exists():
        raise FileNotFoundError(f"no skills directory at {skills_dir}")

    nodes: list[SkillNode] = []
    for skill_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        body = skill_md.read_text(encoding="utf-8", errors="replace")
        fm = _parse_frontmatter(body)
        token_stream = _tokens(_node_text(skill_dir.name, body, fm, skill_dir, source_scope))
        nodes.append(
            SkillNode(
                slug=skill_dir.name,
                name=str(fm.get("name") or skill_dir.name),
                description=str(fm.get("description") or "").strip(),
                when_to_use=_as_list(fm.get("when_to_use") or fm.get("trigger_phrases")),
                when_not_to_use=_as_list(fm.get("when_not_to_use")),
                token_count=len(token_stream),
                byte_size=len(body.encode("utf-8")),
                keywords=_top_keywords(token_stream),
            )
        )
    return nodes


def _similarity(a: SkillNode, b: SkillNode) -> tuple[float, list[str], list[str], list[str]]:
    a_set = set(a.keywords)
    b_set = set(b.keywords)
    if not a_set or not b_set:
        return 0.0, [], sorted(a_set), sorted(b_set)
    shared = sorted(a_set & b_set)
    union = a_set | b_set
    jaccard = len(shared) / len(union)

    # Slug/name token overlap is a strong duplicate hint, so let it lift a
    # borderline pair without swamping semantic keyword overlap.
    a_slug = set(_tokens(a.slug.replace("-", " ") + " " + a.name))
    b_slug = set(_tokens(b.slug.replace("-", " ") + " " + b.name))
    slug_overlap = len(a_slug & b_slug) / max(1, len(a_slug | b_slug))
    score = min(1.0, (jaccard * 0.82) + (slug_overlap * 0.18))
    return score, shared[:10], sorted(a_set - b_set)[:8], sorted(b_set - a_set)[:8]


def _slug_pieces(slug: str) -> list[str]:
    pieces: list[str] = []
    for piece in re.split(r"[-_]+", slug.lower()):
        cleaned = re.sub(r"[^a-z0-9]+", "", piece)
        if cleaned:
            pieces.append(cleaned)
    return pieces


def _keyword_forms(word: str) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9]+", "", word.lower())
    if not cleaned:
        return set()
    forms = {cleaned}
    if cleaned.endswith("s") and len(cleaned) > 3:
        forms.add(cleaned[:-1])
    else:
        forms.add(f"{cleaned}s")
    return forms


def _proposed_slug(members: list[SkillNode], shared: list[str]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    slug_counts: dict[str, int] = {}
    ordered_slug_pieces: list[str] = []
    first_slug_pieces = _slug_pieces(members[0].slug) if members else []
    for member in members:
        for piece in _slug_pieces(member.slug):
            slug_counts[piece] = slug_counts.get(piece, 0) + 1
            if piece not in ordered_slug_pieces:
                ordered_slug_pieces.append(piece)
    shared_forms = {form for word in shared for form in _keyword_forms(word)}
    candidates = [
        piece
        for piece in ordered_slug_pieces
        if piece in shared_forms or slug_counts.get(piece, 0) > 1
    ]
    if len(candidates) >= 2:
        candidates.extend(piece for piece in first_slug_pieces if piece not in candidates)
    candidates.extend(word for word in shared if word not in candidates)
    candidates.extend(piece for piece in ordered_slug_pieces if piece not in candidates)
    for candidate in candidates:
        for piece in re.split(r"[-_]+", candidate.lower()):
            cleaned = re.sub(r"[^a-z0-9]+", "", piece)
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                parts.append(cleaned)
            if len(parts) >= 3:
                break
        if len(parts) >= 3:
            break
    core = "-".join(parts)
    return f"distilled-{core or 'skill'}"


def _context_rot_score(nodes: list[SkillNode], edges: list[SkillEdge], clusters: list[SkillCluster]) -> int:
    if not nodes:
        return 0
    avg_similarity = sum(e.similarity for e in edges) / max(1, len(edges))
    duplicate_pressure = sum(max(0, len(c.members) - 1) for c in clusters) / max(1, len(nodes))
    byte_pressure = min(1.0, sum(n.byte_size for n in nodes) / 120_000)
    skill_pressure = min(1.0, len(nodes) / 24)
    score = avg_similarity * 32 + duplicate_pressure * 34 + byte_pressure * 18 + skill_pressure * 16
    return int(round(max(0, min(100, score))))


def _edge_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _cluster_candidate_score(
    cluster: list[str],
    candidate: str,
    edge_lookup: dict[tuple[str, str], SkillEdge],
    min_similarity: float,
) -> float | None:
    scores = [
        edge.similarity
        for member in cluster
        if (edge := edge_lookup.get(_edge_key(member, candidate))) is not None
    ]
    if not scores:
        return None
    required_links = min(
        len(cluster),
        max(2, math.ceil(len(cluster) * _MIN_CLUSTER_LINK_FRACTION)),
    )
    if len(scores) < required_links:
        return None
    linked_avg = sum(scores) / len(scores)
    if linked_avg < min(1.0, min_similarity + _COHESION_MARGIN):
        return None
    return linked_avg


def _find_tight_clusters(
    nodes: list[SkillNode],
    edges: list[SkillEdge],
    *,
    min_similarity: float,
) -> list[list[str]]:
    """Find small cohesive groups without connected-component bridge collapse."""
    slugs = {node.slug for node in nodes}
    edge_lookup = {_edge_key(edge.a, edge.b): edge for edge in edges}
    ordered_edges = sorted(edges, key=lambda e: (-e.similarity, e.a, e.b))
    used: set[str] = set()
    clusters: list[list[str]] = []

    for edge in ordered_edges:
        if edge.a in used or edge.b in used:
            continue
        cluster = [edge.a, edge.b]
        while len(cluster) < _MAX_DISTILL_CLUSTER_SIZE:
            choices: list[tuple[float, str]] = []
            for slug in sorted(slugs - used - set(cluster)):
                score = _cluster_candidate_score(cluster, slug, edge_lookup, min_similarity)
                if score is not None:
                    choices.append((score, slug))
            if not choices:
                break
            _, chosen = max(choices, key=lambda item: (item[0], item[1]))
            cluster.append(chosen)
        clusters.append(sorted(cluster))
        used.update(cluster)

    return clusters


def _build_cluster(
    cluster_id: int,
    member_slugs: list[str],
    by_slug: dict[str, SkillNode],
    edges: list[SkillEdge],
) -> SkillCluster:
    members = [by_slug[s] for s in sorted(member_slugs)]
    member_set = {m.slug for m in members}
    member_keyword_sets = [set(m.keywords) for m in members if m.keywords]
    shared = sorted(set.intersection(*member_keyword_sets))[:10] if member_keyword_sets else []
    cluster_edges = [e for e in edges if e.a in member_set and e.b in member_set]
    possible_edges = max(1, len(members) * (len(members) - 1) // 2)
    score = sum(e.similarity for e in cluster_edges) / possible_edges
    differences = {m.slug: sorted(set(m.keywords) - set(shared))[:8] for m in members}
    proposed = _proposed_slug(members, shared)
    return SkillCluster(
        id=f"cluster-{cluster_id}",
        members=[m.slug for m in members],
        score=round(score, 3),
        shared_keywords=shared,
        differences=differences,
        proposed_slug=proposed,
        proposed_description=f"Shared workflow distilled from {', '.join(m.slug for m in members)}",
        reduction_hint=f"{len(members)} skills -> 1 pending shared skill; review originals after approval",
    )


def build_distill_plan(
    project_key: str,
    *,
    min_similarity: float = 0.28,
    source_scope: str = "metadata",
) -> DistillPlan:
    """Build a deterministic skill similarity graph and merge plan."""
    nodes = load_skill_nodes(project_key, source_scope=source_scope)
    by_slug = {n.slug: n for n in nodes}
    edges: list[SkillEdge] = []

    for i, a in enumerate(nodes):
        for b in nodes[i + 1 :]:
            score, shared, a_only, b_only = _similarity(a, b)
            if score >= min_similarity:
                edges.append(
                    SkillEdge(
                        a=a.slug,
                        b=b.slug,
                        similarity=round(score, 3),
                        shared_keywords=shared,
                        a_only=a_only,
                        b_only=b_only,
                    )
                )

    cluster_groups = _find_tight_clusters(nodes, edges, min_similarity=min_similarity)
    clusters = [
        _build_cluster(idx, group, by_slug, edges)
        for idx, group in enumerate(cluster_groups, start=1)
    ]

    clustered = {slug for c in clusters for slug in c.members}
    standalone = [n.slug for n in nodes if n.slug not in clustered]
    context_rot = _context_rot_score(nodes, edges, clusters)
    return DistillPlan(
        project_key=project_key,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        skill_count=len(nodes),
        edge_count=len(edges),
        cluster_count=len(clusters),
        context_rot_score=context_rot,
        total_skill_bytes=sum(n.byte_size for n in nodes),
        min_similarity=min_similarity,
        source_scope=source_scope,
        nodes=nodes,
        edges=edges,
        clusters=clusters,
        standalone=standalone,
    )


def write_distill_plan(plan: DistillPlan) -> Path:
    """Persist ``_distill_plan.json`` beside the project's bundle."""
    path = BUNDLES_DIR / plan.project_key / "_distill_plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(plan), indent=2), encoding="utf-8")
    return path


def _extract_json_object(text: str) -> dict[str, Any]:
    """Best-effort parse for rubric JSON returned by mixed providers."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("LLM rubric response did not contain a JSON object")
    parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM rubric response was not a JSON object")
    return parsed


def _clamp_float(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = low
    return max(low, min(high, num))


def _clamp_int(value: Any, low: int = 0, high: int = 5) -> int:
    try:
        num = int(round(float(value)))
    except (TypeError, ValueError):
        num = low
    return max(low, min(high, num))


def _coerce_judgment(raw: dict[str, Any]) -> SkillSimilarityJudgment:
    preserve = raw.get("preserve") or raw.get("preserve_points") or []
    if isinstance(preserve, str):
        preserve = [preserve]
    if not isinstance(preserve, list):
        preserve = []
    relationship = str(raw.get("relationship") or "overlap").strip().lower()
    decision = str(raw.get("merge_decision") or raw.get("decision") or "review").strip().lower()
    risk = str(raw.get("risk") or "medium").strip().lower()
    return SkillSimilarityJudgment(
        similarity=round(_clamp_float(raw.get("similarity")), 3),
        relationship=relationship,
        merge_decision=decision,
        trigger_overlap=_clamp_int(raw.get("trigger_overlap")),
        procedure_overlap=_clamp_int(raw.get("procedure_overlap")),
        boundary_compatibility=_clamp_int(raw.get("boundary_compatibility")),
        context_rot_reduction=_clamp_int(raw.get("context_rot_reduction")),
        risk=risk,
        rationale=str(raw.get("rationale") or "").strip()[:900],
        preserve=[str(item).strip() for item in preserve if str(item).strip()][:8],
    )


def _skill_doc_excerpt(project_key: str, slug: str, *, max_chars: int = 8_000) -> str:
    path = BUNDLES_DIR / project_key / "skills" / slug / "SKILL.md"
    if not path.exists():
        return f"# {slug}\n\n(SKILL.md missing)"
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n\n[truncated]"


def _semantic_prompt(plan: DistillPlan, cluster: SkillCluster) -> tuple[str, str]:
    docs = []
    for slug in cluster.members:
        docs.append(f"===== SKILL: {slug} =====\n{_skill_doc_excerpt(plan.project_key, slug)}")
    system = (
        "You are watchmen's skill distillation judge. Evaluate whether several "
        "agent skills should be merged. Be strict: shared words are not enough. "
        "Score semantic replaceability and merge safety, not lexical overlap. "
        "You must return only valid JSON."
    )
    user = (
        "Judge this proposed skill merge using the rubric below.\n\n"
        "Similarity scale:\n"
        "- 1.00: functionally interchangeable; one distilled skill can replace the originals with no real loss.\n"
        "- 0.80-0.99: same workflow with minor naming or scope differences; merge is likely safe.\n"
        "- 0.60-0.79: overlapping workflow; merge only if branches/boundaries are preserved.\n"
        "- 0.40-0.59: related but should usually stay separate.\n"
        "- <0.40: adjacent/unrelated/conflicting.\n\n"
        "Return ONLY a JSON object with exactly these keys:\n"
        "{\n"
        '  "similarity": 0.0,\n'
        '  "relationship": "duplicate|superset|overlap|adjacent|unrelated|conflict",\n'
        '  "merge_decision": "merge|merge_with_caveats|keep_separate",\n'
        '  "trigger_overlap": 0,\n'
        '  "procedure_overlap": 0,\n'
        '  "boundary_compatibility": 0,\n'
        '  "context_rot_reduction": 0,\n'
        '  "risk": "low|medium|high",\n'
        '  "rationale": "one concise sentence",\n'
        '  "preserve": ["short point to preserve in a merged draft"]\n'
        "}\n\n"
        "Rubric dimensions are integers 0-5. boundary_compatibility means the "
        "when_not_to_use exclusions do not contradict each other. "
        "context_rot_reduction means merging would reduce duplicated context without hiding important distinctions.\n\n"
        f"Project: {plan.project_key}\n"
        f"Proposed draft slug: {cluster.proposed_slug}\n\n"
        + "\n\n".join(docs)
    )
    return system, user


def judge_distill_plan_with_llm(
    plan: DistillPlan,
    *,
    model: str | None = None,
    console: Console | None = None,
    show_visual: bool = True,
) -> DistillPlan:
    """Attach LLM rubric judgments to local distillation candidates."""
    if not plan.clusters:
        return plan
    import httpx

    from watchmen import config

    model = model or config.distill_default_model()
    with httpx.Client(timeout=300.0) as client:
        for idx, cluster in enumerate(plan.clusters, start=1):
            cluster.semantic_judgment = _judge_cluster_semantically(
                client,
                plan,
                cluster,
                idx=idx,
                total=len(plan.clusters),
                model=model,
                console=console,
                show_visual=show_visual,
            )
            if cluster.semantic_judgment.merge_decision == "keep_separate":
                cluster.reduction_hint = "LLM rubric says keep separate; do not stage"
            else:
                cluster.reduction_hint = (
                    f"{cluster.reduction_hint}; LLM {cluster.semantic_judgment.merge_decision}"
                )
    plan.semantic_model = model
    return plan


def _judge_cluster_semantically(
    client,
    plan: DistillPlan,
    cluster: SkillCluster,
    *,
    idx: int,
    total: int,
    model: str,
    console: Console | None = None,
    show_visual: bool = True,
    progress: _SemanticProgressState | None = None,
) -> SkillSimilarityJudgment:
    from watchmen import config
    from watchmen.agent import chat_call

    system, user = _semantic_prompt(plan, cluster)

    def call(messages: list[dict]) -> dict:
        provider = config.active_provider()
        extra: dict[str, Any] = {}
        if provider in {"openrouter", "openai"}:
            extra["response_format"] = {"type": "json_object"}
        return chat_call(
            client,
            messages,
            model=model,
            agent_name="distill-judge",
            max_retries=2,
            temperature=0,
            max_tokens=900,
            **extra,
        )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    content = ""
    for attempt in range(2):
        if console is not None and show_visual:
            data = _call_with_semantic_progress(
                console,
                plan,
                cluster,
                idx,
                total,
                lambda: call(messages),
                progress=progress,
            )
        else:
            data = call(messages)
        content = data["choices"][0]["message"].get("content") or ""
        try:
            return _coerce_judgment(_extract_json_object(content))
        except (json.JSONDecodeError, ValueError):
            if attempt == 1:
                break
            messages.extend([
                {"role": "assistant", "content": content or "(empty response)"},
                {
                    "role": "user",
                    "content": (
                        "Your previous answer was not a JSON object. Return only "
                        "the required JSON object now, with no prose and no markdown."
                    ),
                },
            ])
    return SkillSimilarityJudgment(
        similarity=0.0,
        relationship="unrelated",
        merge_decision="keep_separate",
        trigger_overlap=0,
        procedure_overlap=0,
        boundary_compatibility=0,
        context_rot_reduction=0,
        risk="high",
        rationale="LLM did not return parseable rubric JSON after retry.",
        preserve=[],
    )


def _semantic_progress_frame(
    plan: DistillPlan,
    cluster: SkillCluster,
    idx: int,
    total: int,
    tick: int,
    *,
    progress: _SemanticProgressState | None = None,
) -> Text:
    left = cluster.members[0] if cluster.members else "skill-a"
    right = cluster.members[1] if len(cluster.members) > 1 else "skill-b"
    total = max(1, total)
    pct = min(1.0, max(0.0, idx / total))
    width = 24
    filled = min(width, max(0, round(pct * width)))
    bar = "#" * filled + "-" * (width - filled)
    pulse = [".  ", ".. ", "...", "   "][tick % 4]
    accepted = progress.accepted_edges if progress else 0
    if progress and progress.strongest_pair and progress.strongest_score is not None:
        strongest = (
            f"{progress.strongest_pair[0]} + {progress.strongest_pair[1]} "
            f"{progress.strongest_score:.2f}"
        )
    else:
        strongest = "none yet"

    text = Text()
    text.append(f"semantic distill {idx}/{total}  [{bar}] {pct:>5.0%}{pulse}\n", style="bold cyan")
    text.append("judging: ", style="dim")
    text.append(left, style="green")
    text.append(" <-> ", style="dim")
    text.append(f"{right}\n", style="green")
    text.append(f"merge hits: {accepted}   strongest: {strongest}\n", style="dim")
    text.append(f"threshold: {plan.min_similarity:.2f}   model: {plan.semantic_model or 'local'}", style="dim")
    return text


def _call_with_semantic_progress(
    console: Console,
    plan: DistillPlan,
    cluster: SkillCluster,
    idx: int,
    total: int,
    call,
    *,
    progress: _SemanticProgressState | None = None,
) -> dict:
    stop = threading.Event()
    with Live(
        _semantic_progress_frame(plan, cluster, idx, total, 0, progress=progress),
        console=console,
        refresh_per_second=8,
        transient=True,
    ) as live:
        def pump() -> None:
            local_tick = 0
            while not stop.wait(0.14):
                local_tick += 1
                live.update(_semantic_progress_frame(plan, cluster, idx, total, local_tick, progress=progress))

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        try:
            return call()
        finally:
            stop.set()
            thread.join(timeout=1)
            live.update(_semantic_progress_frame(plan, cluster, idx, total, 0, progress=progress))
            time.sleep(0.08)


def _mergeable_decision(judgment: SkillSimilarityJudgment) -> bool:
    return judgment.merge_decision in {"merge", "merge_with_caveats"}


def _risk_rank(risk: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(risk, 1)


def _synthetic_cluster_judgment(judgments: list[SkillSimilarityJudgment]) -> SkillSimilarityJudgment | None:
    if not judgments:
        return None
    similarity = sum(j.similarity for j in judgments) / len(judgments)
    decision = "merge" if all(j.merge_decision == "merge" for j in judgments) else "merge_with_caveats"
    relationship = "duplicate" if all(j.relationship == "duplicate" for j in judgments) else "overlap"
    risk = max((j.risk for j in judgments), key=_risk_rank)
    preserve: list[str] = []
    for judgment in judgments:
        for item in judgment.preserve:
            if item not in preserve:
                preserve.append(item)
    return SkillSimilarityJudgment(
        similarity=round(similarity, 3),
        relationship=relationship,
        merge_decision=decision,
        trigger_overlap=round(sum(j.trigger_overlap for j in judgments) / len(judgments)),
        procedure_overlap=round(sum(j.procedure_overlap for j in judgments) / len(judgments)),
        boundary_compatibility=round(sum(j.boundary_compatibility for j in judgments) / len(judgments)),
        context_rot_reduction=round(sum(j.context_rot_reduction for j in judgments) / len(judgments)),
        risk=risk,
        rationale="Merged from pairwise semantic judgments.",
        preserve=preserve[:8],
    )


def build_semantic_distill_plan(
    project_key: str,
    *,
    model: str | None = None,
    min_similarity: float = 0.80,
    source_scope: str = "metadata",
    console: Console | None = None,
    show_visual: bool = True,
) -> DistillPlan:
    """Build the merge plan from LLM pair judgments instead of lexical edges."""
    import httpx

    from watchmen import config

    nodes = load_skill_nodes(project_key, source_scope=source_scope)
    by_slug = {n.slug: n for n in nodes}
    model = model or config.distill_default_model()
    pair_count = len(nodes) * (len(nodes) - 1) // 2
    seed_plan = DistillPlan(
        project_key=project_key,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        skill_count=len(nodes),
        edge_count=0,
        cluster_count=0,
        context_rot_score=0,
        total_skill_bytes=sum(n.byte_size for n in nodes),
        min_similarity=min_similarity,
        source_scope=source_scope,
        nodes=nodes,
        edges=[],
        clusters=[],
        standalone=[],
        semantic_model=model,
    )

    edges: list[SkillEdge] = []
    judged_edges: list[SkillEdge] = []
    pair_judgments: dict[tuple[str, str], SkillSimilarityJudgment] = {}
    idx = 0
    with httpx.Client(timeout=300.0) as client:
        for i, a in enumerate(nodes):
            for b in nodes[i + 1 :]:
                idx += 1
                local_score, shared, a_only, b_only = _similarity(a, b)
                pair_cluster = _build_cluster(idx, [a.slug, b.slug], by_slug, [])
                pair_cluster.score = round(local_score, 3)
                pair_cluster.shared_keywords = shared
                strongest_edge = max(judged_edges, key=lambda edge: edge.similarity, default=None)
                progress = _SemanticProgressState(
                    accepted_edges=len(edges),
                    strongest_pair=(strongest_edge.a, strongest_edge.b) if strongest_edge else None,
                    strongest_score=strongest_edge.similarity if strongest_edge else None,
                )
                judgment = _judge_cluster_semantically(
                    client,
                    seed_plan,
                    pair_cluster,
                    idx=idx,
                    total=pair_count,
                    model=model,
                    console=console,
                    show_visual=show_visual,
                    progress=progress,
                )
                pair_judgments[_edge_key(a.slug, b.slug)] = judgment
                judged_edge = SkillEdge(
                    a=a.slug,
                    b=b.slug,
                    similarity=round(judgment.similarity, 3),
                    shared_keywords=shared,
                    a_only=a_only,
                    b_only=b_only,
                    semantic_judgment=judgment,
                )
                judged_edges.append(judged_edge)
                if judgment.similarity >= min_similarity and _mergeable_decision(judgment):
                    edges.append(judged_edge)

    cluster_groups = _find_tight_clusters(nodes, edges, min_similarity=min_similarity)
    clusters: list[SkillCluster] = []
    for cluster_id, group in enumerate(cluster_groups, start=1):
        cluster = _build_cluster(cluster_id, group, by_slug, edges)
        judgments = [
            judgment
            for i, a in enumerate(group)
            for b in group[i + 1 :]
            if (judgment := pair_judgments.get(_edge_key(a, b))) is not None
        ]
        cluster.semantic_judgment = _synthetic_cluster_judgment(judgments)
        if cluster.semantic_judgment:
            cluster.reduction_hint = (
                f"{len(cluster.members)} skills -> 1 pending shared skill; "
                f"LLM {cluster.semantic_judgment.merge_decision}"
            )
        clusters.append(cluster)

    clustered = {slug for cluster in clusters for slug in cluster.members}
    standalone = [node.slug for node in nodes if node.slug not in clustered]
    context_rot = _context_rot_score(nodes, edges, clusters)
    return DistillPlan(
        project_key=project_key,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        skill_count=len(nodes),
        edge_count=len(edges),
        cluster_count=len(clusters),
        context_rot_score=context_rot,
        total_skill_bytes=sum(n.byte_size for n in nodes),
        min_similarity=min_similarity,
        source_scope=source_scope,
        nodes=nodes,
        edges=edges,
        clusters=clusters,
        standalone=standalone,
        semantic_model=model,
        semantic_judgments=judged_edges,
    )


def _cluster_is_stageable(cluster: SkillCluster) -> bool:
    return not (
        cluster.semantic_judgment is not None
        and cluster.semantic_judgment.merge_decision == "keep_separate"
    )


def _merged_skill_body(plan: DistillPlan, cluster: SkillCluster) -> str:
    node_by_slug = {n.slug: n for n in plan.nodes}
    members = [node_by_slug[s] for s in cluster.members if s in node_by_slug]
    triggers: list[str] = []
    anti_triggers: list[str] = []
    for node in members:
        triggers.extend(node.when_to_use[:4])
        anti_triggers.extend(node.when_not_to_use[:3])
    if not triggers:
        keyword_hint = ", ".join(cluster.shared_keywords[:5]) or "the shared workflow"
        triggers = [f"Use when the task touches {keyword_hint} across this project."]
    if not anti_triggers:
        anti_triggers = [
            "Do not use for one-off tasks that only match a single source skill narrowly.",
            "Do not use when a more specific hand-written skill has been explicitly requested.",
        ]

    sources = "\n".join(f"- {m.slug}: {m.description or '(no description)'}" for m in members)
    differences = "\n".join(
        f"- {slug}: {', '.join(words) if words else '(no distinct keywords)'}"
        for slug, words in cluster.differences.items()
    )
    trigger_lines = "\n".join(f"  - {t}" for t in triggers[:10])
    anti_lines = "\n".join(f"  - {t}" for t in anti_triggers[:8])
    shared = ", ".join(cluster.shared_keywords) or "shared workflow"
    if cluster.semantic_judgment is not None:
        rubric = (
            "\n## LLM Similarity Rubric\n\n"
            f"- similarity: {cluster.semantic_judgment.similarity:.2f}\n"
            f"- relationship: {cluster.semantic_judgment.relationship}\n"
            f"- decision: {cluster.semantic_judgment.merge_decision}\n"
            f"- risk: {cluster.semantic_judgment.risk}\n"
            f"- rationale: {cluster.semantic_judgment.rationale or '(none)'}\n"
        )
        if cluster.semantic_judgment.preserve:
            rubric += "\nPreserve:\n" + "\n".join(f"- {item}" for item in cluster.semantic_judgment.preserve) + "\n"
    else:
        rubric = ""

    return f"""---
name: {cluster.proposed_slug}
description: {cluster.proposed_description}
when_to_use:
{trigger_lines}
when_not_to_use:
{anti_lines}
---

# {cluster.proposed_slug}

This is a staged distillation draft. It merges overlapping guidance from:

{sources}

## Shared Core

The shared center of gravity is: {shared}.

## Procedure

1. Start from the current user request and decide which source skill pattern it resembles.
2. Apply only the shared steps that are relevant to the active project state.
3. Preserve source-specific details only when the request clearly needs them.
4. Prefer this distilled skill over the source skills when multiple originals would otherwise fire together.

## Differences To Preserve

{differences}
{rubric}

## Review Notes

- Generated by `watchmen distill --stage`.
- Approving this draft does not delete the original skills.
- After this draft proves useful, run `watchmen prune {plan.project_key}` and drop the superseded originals deliberately.
"""


def _write_distilled_candidate(plan: DistillPlan, cluster: SkillCluster, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    skill_md = dest / "SKILL.md"
    skill_md.write_text(_merged_skill_body(plan, cluster), encoding="utf-8")
    refs = dest / "references"
    refs.mkdir(exist_ok=True)
    (refs / "source-skills.md").write_text(
        "# Source Skills\n\n" + "\n".join(f"- {slug}" for slug in cluster.members) + "\n",
        encoding="utf-8",
    )
    return skill_md


def stage_distilled_candidates(
    plan: DistillPlan,
    *,
    selected_slugs: Iterable[str] | None = None,
) -> list[Path]:
    """Create merged draft skills under ``_pending/`` for each cluster."""
    pending_dir = BUNDLES_DIR / plan.project_key / "_pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    selected = set(selected_slugs) if selected_slugs is not None else None
    written: list[Path] = []
    for cluster in plan.clusters:
        if selected is not None and cluster.proposed_slug not in selected:
            continue
        if not _cluster_is_stageable(cluster):
            continue
        dest = pending_dir / cluster.proposed_slug
        written.append(_write_distilled_candidate(plan, cluster, dest))
    return written


def stageable_distill_clusters(plan: DistillPlan) -> list[SkillCluster]:
    return [cluster for cluster in plan.clusters if _cluster_is_stageable(cluster)]


def _read_slug_set(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if not isinstance(parsed, list):
        return set()
    return {str(item) for item in parsed if str(item).strip()}


def _write_slug_set(path: Path, values: set[str]) -> None:
    if values:
        path.write_text(json.dumps(sorted(values), indent=2) + "\n", encoding="utf-8")
    elif path.exists():
        path.unlink()


def _unique_child(parent: Path, name: str) -> Path:
    dest = parent / name
    if not dest.exists():
        return dest
    for idx in range(2, 10_000):
        candidate = parent / f"{name}-{idx}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find a unique archive path for {name}")


def _archive_dir(project_key: str) -> Path:
    root = BUNDLES_DIR / project_key / "_distilled_archive"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = _unique_child(root, stamp)
    path.mkdir(parents=True, exist_ok=False)
    (path / "sources").mkdir()
    (path / "active-conflicts").mkdir()
    return path


def _append_distill_audit(
    plan: DistillPlan,
    result: DistillApplyResult,
    clusters: list[SkillCluster],
) -> Path:
    project_dir = BUNDLES_DIR / plan.project_key
    review_path = project_dir / "review.md"
    ts = datetime.now().isoformat(timespec="seconds")
    block = [f"## distill merge {ts}", ""]
    block.append(f"- promoted: {', '.join(result.promoted) if result.promoted else '(none)'}")
    block.append(
        f"- archived originals: "
        f"{', '.join(result.archived_sources) if result.archived_sources else '(none)'}"
    )
    if result.archive_dir:
        block.append(f"- archive: {result.archive_dir}")
    for cluster in clusters:
        judgment = cluster.semantic_judgment
        if judgment:
            block.append(
                f"- {cluster.proposed_slug}: {judgment.similarity:.2f} "
                f"{judgment.merge_decision}/{judgment.risk} from {', '.join(cluster.members)}"
            )
        else:
            block.append(f"- {cluster.proposed_slug}: from {', '.join(cluster.members)}")
    block.append("")
    existing = review_path.read_text(encoding="utf-8") if review_path.exists() else ""
    review_path.write_text("\n".join(block) + ("\n" + existing if existing else ""), encoding="utf-8")
    return review_path


def apply_distilled_candidates(
    plan: DistillPlan,
    selected_slugs: Iterable[str],
) -> DistillApplyResult:
    """Promote selected distilled skills and remove their source skills.

    Removal is active-set removal, not irreversible deletion: superseded source
    skill folders are moved to ``_distilled_archive/<timestamp>/sources/`` and
    their slugs are added to ``_blocklist.json`` so the curator does not
    immediately regenerate them.
    """
    selected = set(selected_slugs)
    clusters = [
        cluster
        for cluster in stageable_distill_clusters(plan)
        if cluster.proposed_slug in selected
    ]
    if not clusters:
        return DistillApplyResult([], [], [], None, None)

    project_dir = BUNDLES_DIR / plan.project_key
    skills_dir = project_dir / "skills"
    pending_dir = project_dir / "_pending"
    skills_dir.mkdir(parents=True, exist_ok=True)
    archive_dir = _archive_dir(plan.project_key)
    sources_archive = archive_dir / "sources"
    conflicts_archive = archive_dir / "active-conflicts"

    promoted: list[str] = []
    archived_sources: list[str] = []
    blocklisted_sources: set[str] = _read_slug_set(project_dir / "_blocklist.json")

    for cluster in clusters:
        dest = skills_dir / cluster.proposed_slug
        if dest.exists():
            shutil.move(str(dest), str(_unique_child(conflicts_archive, dest.name)))

        pending = pending_dir / cluster.proposed_slug
        if pending.exists():
            shutil.move(str(pending), str(dest))
        else:
            _write_distilled_candidate(plan, cluster, dest)
        promoted.append(cluster.proposed_slug)

        for source_slug in cluster.members:
            if source_slug == cluster.proposed_slug:
                continue
            source_dir = skills_dir / source_slug
            if source_dir.exists():
                shutil.move(str(source_dir), str(_unique_child(sources_archive, source_slug)))
                archived_sources.append(source_slug)
            blocklisted_sources.add(source_slug)

    _write_slug_set(project_dir / "_blocklist.json", blocklisted_sources)
    result = DistillApplyResult(
        promoted=promoted,
        archived_sources=archived_sources,
        blocklisted_sources=sorted(blocklisted_sources),
        archive_dir=str(archive_dir),
        audit_path=None,
    )
    audit_path = _append_distill_audit(plan, result, clusters)
    result.audit_path = str(audit_path)
    return result


def _rot_label(score: int) -> tuple[str, str]:
    if score >= 70:
        return "critical", "red"
    if score >= 40:
        return "warm", "yellow"
    return "low", "green"


def render_plan_summary(plan: DistillPlan, *, console: Console | None = None) -> None:
    """Print a compact static mesh summary."""
    console = console or Console()
    label, color = _rot_label(plan.context_rot_score)
    semantic_mode = plan.semantic_model is not None
    if semantic_mode:
        graph_line = (
            f"skills={plan.skill_count} judged_pairs={len(plan.semantic_judgments)} "
            f"candidate_edges={plan.edge_count} clusters={plan.cluster_count} scope={plan.source_scope}"
        )
    else:
        graph_line = (
            f"skills={plan.skill_count} candidate_edges={plan.edge_count} "
            f"clusters={plan.cluster_count} scope={plan.source_scope}"
        )
    console.print(
        Panel(
            f"[bold]{plan.project_key}[/]\n"
            f"{graph_line}\n"
            f"semantic judge={plan.semantic_model or 'local-only'}\n"
            f"context rot=[{color}]{plan.context_rot_score}/100 ({label})[/]",
            title="watchmen distill",
            box=box.ROUNDED,
        )
    )

    has_semantic = any(cluster.semantic_judgment for cluster in plan.clusters)
    if semantic_mode and plan.semantic_judgments:
        semantic_table = Table(title="semantic similarity judgments", box=box.SIMPLE)
        semantic_table.add_column("skill A")
        semantic_table.add_column("skill B")
        semantic_table.add_column("sim", justify="right")
        semantic_table.add_column("relationship")
        semantic_table.add_column("decision")
        semantic_table.add_column("rationale")
        for edge in sorted(
            plan.semantic_judgments,
            key=lambda e: e.semantic_judgment.similarity if e.semantic_judgment else e.similarity,
            reverse=True,
        )[:12]:
            if not edge.semantic_judgment:
                continue
            semantic_table.add_row(
                edge.a,
                edge.b,
                f"{edge.semantic_judgment.similarity:.2f}",
                edge.semantic_judgment.relationship,
                f"{edge.semantic_judgment.merge_decision}/{edge.semantic_judgment.risk}",
                edge.semantic_judgment.rationale or "-",
            )
        console.print(semantic_table)
        if not plan.edges:
            console.print(
                f"[dim]no semantic merge edges above threshold {plan.min_similarity:.2f}; "
                "showing strongest rejected judgments[/]"
            )
    elif semantic_mode:
        console.print("[dim]no semantic judgments recorded[/]")
    elif plan.edges:
        edge_table = Table(title="local candidate edges", box=box.SIMPLE)
        edge_table.add_column("skill A")
        edge_table.add_column("skill B")
        edge_table.add_column("local", justify="right")
        edge_table.add_column("shared keywords")
        for edge in sorted(plan.edges, key=lambda e: e.similarity, reverse=True)[:12]:
            edge_table.add_row(
                edge.a,
                edge.b,
                f"{edge.similarity:.2f}",
                ", ".join(edge.shared_keywords[:6]) or "-",
            )
        console.print(edge_table)
    else:
        console.print("[dim]no local candidate edges above threshold[/]")

    if plan.clusters:
        cluster_table = Table(title="distillation candidates", box=box.SIMPLE)
        cluster_table.add_column("new draft")
        cluster_table.add_column("source skills")
        if has_semantic:
            cluster_table.add_column("llm", justify="right")
            cluster_table.add_column("decision")
        else:
            cluster_table.add_column("local", justify="right")
        cluster_table.add_column("alchemy")
        for cluster in plan.clusters:
            row = [
                cluster.proposed_slug,
                ", ".join(cluster.members),
            ]
            if has_semantic:
                if cluster.semantic_judgment:
                    row.extend([
                        f"{cluster.semantic_judgment.similarity:.2f}",
                        f"{cluster.semantic_judgment.merge_decision}/{cluster.semantic_judgment.risk}",
                    ])
                else:
                    row.extend(["-", "-"])
            else:
                row.append(f"{cluster.score:.2f}")
            row.append(cluster.reduction_hint)
            cluster_table.add_row(*row)
        console.print(cluster_table)
    else:
        console.print("[green]no merge clusters found at this threshold[/]")


def _mesh_frame(plan: DistillPlan, phase: str, highlight: int) -> Panel:
    lines: list[str] = []
    nodes = plan.nodes[:12]
    if not nodes:
        lines.append("(no skills)")
    for idx, node in enumerate(nodes):
        marker = "*" if idx == highlight % max(1, len(nodes)) else "o"
        linked = [e.b if e.a == node.slug else e.a for e in plan.edges if e.a == node.slug or e.b == node.slug]
        tail = " -- " + ", ".join(linked[:3]) if linked else ""
        lines.append(f"{marker} {node.slug}{tail}")
    if plan.clusters:
        lines.append("")
        for cluster in plan.clusters[:5]:
            lines.append(f"[{'+'.join(cluster.members[:3])}] => {cluster.proposed_slug}")
    text = Text("\n".join(lines), style="cyan")
    return Panel(
        Align.left(text),
        title=f"watchmen skill mesh :: {phase}",
        subtitle=f"context rot {plan.context_rot_score}/100 :: scope {plan.source_scope}",
        box=box.DOUBLE,
    )


def render_plan_animation(plan: DistillPlan, *, console: Console | None = None) -> None:
    """Render a short Rich Live visualization of the mesh reducing."""
    console = console or Console()
    phases = [
        "inspecting created skills",
        "weaving similarity mesh",
        "finding differences",
        "distilling overlap",
        "staging fewer sharper materials",
    ]
    with Live(console=console, refresh_per_second=8, transient=False) as live:
        ticks = max(8, len(plan.nodes) + len(plan.edges) + len(plan.clusters))
        for i in range(ticks):
            phase = phases[min(len(phases) - 1, math.floor(i / max(1, ticks / len(phases))))]
            live.update(_mesh_frame(plan, phase, i))
            import time

            time.sleep(0.08)
    render_plan_summary(plan, console=console)
