"""Prompt-intent map (#111) — the one place a spatial layout is justified.

Embeds genuine user prompts with a **local** sentence model, projects them to
2D, and colours each point by how the turn went (errored / rephrase / clean).
Semantic space is inherently projectable, so a scatter actually reveals
structure here — clusters are *kinds of things you ask agents to do*, and a
cluster glowing red is a kind of task that keeps going wrong.

Design constraints (Batuhan's, carried through the whole visual-tools series):

- **Local-first, no data leaves the machine.** The default embedder is
  `model2vec` (static embeddings — a distilled sentence-transformer baked into
  a lookup table): pure-numpy, no PyTorch, ~30 MB model, thousands of prompts a
  second on CPU. It and scikit-learn live behind the optional ``[map]`` extra,
  so the core install stays ML-free. `EMBEDDER` is swappable if we ever want to
  trade footprint for the sharper neighbourhoods of a contextual model.
- **No causation claims.** Outcome colours describe what *co-occurred* with a
  prompt (a tool error in that turn, a re-prompt right after), never that the
  prompt caused it.

The expensive parts — embedding and the t-SNE projection — are cached in
`intent.db`, keyed so a re-run only does new work. The cache is rebuildable
from `corpus.db` at any time.
"""

from __future__ import annotations

import hashlib
import sqlite3
import struct
from datetime import date, timedelta

from watchmen.paths import CORPUS_DB, INTENT_DB, STATE_DB

# Default local model. potion-base-8M is the small/fast tier; the 32M tier
# trades ~4× size for sharper neighbourhoods. Kept here so the CLI/tests can
# see what the cache was built with (vectors are namespaced by model name).
DEFAULT_MODEL = "minishlab/potion-base-8M"

# Cap on points fed to the projector. t-SNE is ~O(n²); past a few thousand
# points the map turns to mush anyway. When a window has more genuine prompts
# than this we sample the most recent and SAY SO (no silent truncation).
MAX_POINTS = 4000


class MapDepsMissing(RuntimeError):
    """Raised when the optional ``[map]`` extra isn't installed. Callers catch
    this to show an install hint instead of a traceback."""


# ── Genuine-prompt filter ────────────────────────────────────────────────
#
# The `prompts` table is not all human intent. Agents and the harness inject
# synthetic "prompts": PR-review notifications, interrupt markers, continuation
# banners, slash-command stdout. A map of *how you use agents* should reflect
# what you typed, so we drop the obvious machine-authored ones. Substring match,
# case-insensitive, cheap — precision over recall (better to drop a real prompt
# than to pollute the map with bot noise).
_SYNTHETIC_MARKERS = (
    "pull request #",
    "got a `request_changes`",
    "got a `approve`",
    "[request interrupted",
    "this session is being continued",
    "<system-reminder>",
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "local-command-caveat",
    "caveat: the messages below",
    "<task-notification",
    "[task-notification",
    "<turn_aborted",
    "# agents.md instructions",
    "you are a contributor on this team",
    "the user opened the file",
    "the user's task",
)

# Below this many words a prompt is an acknowledgement ("go ahead", "yes
# please", "merged", "resume"), not a task intent — keep it off the intent map.
MIN_WORDS = 3


def _is_genuine(text: str, min_words: int = MIN_WORDS) -> bool:
    if not text:
        return False
    stripped = text.strip()
    if len(stripped.split()) < min_words:
        return False
    low = stripped.lower()
    return not any(low.startswith(m) or m in low[:200] for m in _SYNTHETIC_MARKERS)


# ── Embedder (pluggable; default model2vec) ──────────────────────────────

class Model2VecEmbedder:
    """Lazy wrapper over a model2vec StaticModel. Loads the model once on first
    use (≈20 s incl. one-time download), then encodes in-process."""

    def __init__(self, model_name: str = DEFAULT_MODEL):
        self.model_name = model_name
        self._model = None

    def _ensure(self):
        if self._model is None:
            try:
                from model2vec import StaticModel
            except ImportError as e:  # optional extra not installed
                raise MapDepsMissing(
                    "the prompt-intent map needs the optional ML deps. "
                    "Install them with:  pip install 'watchmen[map]'"
                ) from e
            self._model = StaticModel.from_pretrained(self.model_name)
        return self._model

    def encode(self, texts: list[str]):
        model = self._ensure()
        return model.encode(texts)


# Module-global default so the long-running viewer loads the model once.
# Tests inject a stub here to stay offline.
EMBEDDER = Model2VecEmbedder()


# ── Vector cache (intent.db) ─────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prompt_vectors (
    prompt_id  INTEGER NOT NULL,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vec        BLOB NOT NULL,
    PRIMARY KEY (prompt_id, model)
);
CREATE TABLE IF NOT EXISTS projection (
    cache_key  TEXT NOT NULL,
    prompt_id  INTEGER NOT NULL,
    x          REAL NOT NULL,
    y          REAL NOT NULL,
    PRIMARY KEY (cache_key, prompt_id)
);
"""


def _connect_cache() -> sqlite3.Connection:
    conn = sqlite3.connect(str(INTENT_DB))
    conn.executescript(_SCHEMA)
    return conn


def _pack(vec) -> bytes:
    return struct.pack(f"<{len(vec)}f", *(float(v) for v in vec))


def _unpack(blob: bytes, dim: int):
    return struct.unpack(f"<{dim}f", blob)


def _embed_with_cache(conn: sqlite3.Connection, model_name: str,
                      prompts: list[tuple[int, str]]):
    """Return {prompt_id: vector}. Reads cached vectors, embeds only the misses,
    writes them back. `prompts` is [(id, text), ...]."""
    import numpy as np

    have: dict[int, tuple] = {}
    dim = None
    rows = conn.execute(
        "SELECT prompt_id, dim, vec FROM prompt_vectors WHERE model = ?",
        (model_name,),
    ).fetchall()
    cached_ids = set()
    for pid, d, blob in rows:
        cached_ids.add(pid)
        if pid in {p[0] for p in prompts}:
            have[pid] = np.array(_unpack(blob, d), dtype=np.float32)
            dim = d

    missing = [(pid, text) for pid, text in prompts if pid not in cached_ids]
    if missing:
        vecs = EMBEDDER.encode([t for _, t in missing])
        vecs = np.asarray(vecs, dtype=np.float32)
        dim = vecs.shape[1]
        conn.executemany(
            "INSERT OR REPLACE INTO prompt_vectors (prompt_id, model, dim, vec) VALUES (?,?,?,?)",
            [(pid, model_name, dim, _pack(vecs[i])) for i, (pid, _t) in enumerate(missing)],
        )
        conn.commit()
        for i, (pid, _t) in enumerate(missing):
            have[pid] = vecs[i]
    return have, dim


def _project(vectors, *, seed: int = 0):
    """High-dim embedding rows → 2D coordinates. PCA pre-reduces to ≤50 dims so
    t-SNE runs on a tame space (the standard recipe). Tiny sets skip t-SNE."""
    import numpy as np
    from sklearn.decomposition import PCA
    from sklearn.manifold import TSNE

    X = np.asarray(vectors, dtype=np.float32)
    n = X.shape[0]
    if n < 3:
        # Not enough points to lay out; drop them at the origin neighbourhood.
        return np.zeros((n, 2), dtype=np.float32)
    if X.shape[1] > 50:
        X = PCA(n_components=min(50, n - 1), random_state=seed).fit_transform(X)
    # t-SNE requires perplexity < n_samples; scale it to the set and clamp.
    perplexity = min(max(2.0, (n - 1) / 3.0), 30.0, float(n - 1))
    xy = TSNE(
        n_components=2, init="pca", perplexity=perplexity,
        random_state=seed, max_iter=1000,
    ).fit_transform(X)
    return xy


# ── Outcome classification (turn level) ──────────────────────────────────
#
# A prompt's "turn" runs from its timestamp to the next prompt in the same
# session. We label it by what co-occurred:
#   errored  — a tool call failed during the turn
#   rephrase — the next prompt arrived with NO tool calls in between (you had
#              to restate; the agent did nothing useful with the first ask)
#   clean    — everything else
# This is association, not causation — colours describe co-occurrence.

OUTCOMES = ("errored", "rephrase", "clean")
OUTCOME_COLORS = {
    "errored":  "#dc2626",  # red
    "rephrase": "#f59e0b",  # amber
    "clean":    "#3b82f6",  # blue
}

# Cosine threshold above which two adjacent (no-tools-between) prompts count as
# a genuine *restatement* rather than just the next conversational turn. Tuned
# to fire on "do X" → "I said do X" / near-paraphrases, not on topic shifts.
REPHRASE_SIM = 0.82


def _classify_errors(session_rows: dict, tool_rows: dict) -> tuple[set, dict]:
    """For each prompt's turn (its timestamp → the next prompt in the session):
    did a tool error occur, and did ANY tool run? Returns
    (errored_prompt_ids, had_tools_by_prompt). The rephrase upgrade happens
    later with embeddings — a no-tools turn is only a *candidate*."""
    import bisect

    errored: set[int] = set()
    had_tools: dict[int, bool] = {}
    for sid, prompts in session_rows.items():
        tools = tool_rows.get(sid, [])
        tool_ts = [t[0] for t in tools]
        for i, (pid, ts) in enumerate(prompts):
            nxt = prompts[i + 1][1] if i + 1 < len(prompts) else None
            lo = bisect.bisect_left(tool_ts, ts)
            hi = bisect.bisect_left(tool_ts, nxt) if nxt is not None else len(tools)
            turn_tools = tools[lo:hi]
            had_tools[pid] = bool(turn_tools)
            if any(is_err for _ts, is_err in turn_tools):
                errored.add(pid)
    return errored, had_tools


def _assign_outcomes(genuine: list, errored_ids: set, had_tools: dict,
                     vecmap: dict) -> dict:
    """Final per-prompt outcome. errored wins; otherwise a no-tools turn whose
    next same-session genuine prompt is a near-restatement (high cosine) is a
    `rephrase`; everything else is `clean`. Uses the embeddings we already have,
    so the third colour means something instead of "the agent replied in text"."""
    import numpy as np

    # Next genuine kept prompt within the same session (genuine is ts-sorted).
    by_session: dict[str, list] = {}
    for r in genuine:
        by_session.setdefault(r["sid"], []).append(r)

    def _cos(a, b) -> float:
        a = np.asarray(a); b = np.asarray(b)
        d = float(np.linalg.norm(a) * np.linalg.norm(b))
        return float(a @ b) / d if d else 0.0

    outcomes: dict[int, str] = {}
    for sid, rows in by_session.items():
        for i, r in enumerate(rows):
            pid = r["id"]
            if pid in errored_ids:
                outcomes[pid] = "errored"
                continue
            oc = "clean"
            if not had_tools.get(pid, False) and i + 1 < len(rows):
                nxt = rows[i + 1]["id"]
                if pid in vecmap and nxt in vecmap and _cos(vecmap[pid], vecmap[nxt]) >= REPHRASE_SIM:
                    oc = "rephrase"
            outcomes[pid] = oc
    return outcomes


# ── Public: intent_map ───────────────────────────────────────────────────

def _project_dir_for_key(project_key: str) -> str | None:
    if not STATE_DB.exists():
        return None
    try:
        with sqlite3.connect(str(STATE_DB)) as conn:
            row = conn.execute(
                "SELECT source_repo FROM projects WHERE project_key = ?",
                (project_key,),
            ).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _cache_key(model_name: str, prompt_ids: list[int]) -> str:
    """Projection is global to its input set, so it must be re-run when the set
    changes. Key it by model + the exact id set so an unchanged window reuses
    the stored coordinates."""
    h = hashlib.sha1()
    h.update(model_name.encode())
    h.update(b"|")
    h.update(",".join(str(i) for i in sorted(prompt_ids)).encode())
    return h.hexdigest()


def intent_map(
    days: int = 180,
    project_key: str | None = None,
    model_name: str = DEFAULT_MODEL,
    max_points: int = MAX_POINTS,
    rebuild: bool = False,
) -> dict:
    """Build (or load from cache) the prompt-intent scatter.

    Returns (JSON-able):
        {
          "generated_for", "days", "model",
          "total_prompts":   genuine prompts in window (pre-sampling),
          "shown":           points actually plotted,
          "sampled":         True if total > max_points (we kept the newest),
          "outcomes":        {errored, rephrase, clean} counts among shown,
          "colors":          OUTCOME_COLORS,
          "points":          [{x, y, outcome, text, repo, date, session_id}],
        }

    Never raises for missing data (→ empty). Raises MapDepsMissing only if the
    optional extra is absent AND embedding new prompts is actually required."""
    out: dict = {
        "generated_for": project_key, "days": days, "model": model_name,
        "total_prompts": 0, "shown": 0, "sampled": False,
        "outcomes": {k: 0 for k in OUTCOMES}, "colors": OUTCOME_COLORS,
        "points": [],
    }
    if not CORPUS_DB.exists():
        return out

    project_dir: str | None = None
    if project_key is not None:
        project_dir = _project_dir_for_key(project_key)
        if project_dir is None:
            return out

    today = date.today()
    cutoff = (today - timedelta(days=days - 1)).isoformat()
    proj_filter = " AND s.project_dir = ?" if project_dir else ""
    params = [cutoff] + ([project_dir] if project_dir else [])

    with sqlite3.connect(str(CORPUS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        prows = conn.execute(
            """SELECT p.id AS id, p.session_id AS sid, p.text AS text,
                      p.timestamp AS ts, s.project_dir AS repo
                 FROM prompts p JOIN sessions s ON p.session_id = s.session_id
                WHERE s.is_subagent = 0 AND p.text IS NOT NULL
                  AND date(p.timestamp, 'localtime') >= ?""" + proj_filter,
            params,
        ).fetchall()

    genuine = [r for r in prows if _is_genuine(r["text"])]
    out["total_prompts"] = len(genuine)
    if not genuine:
        return out

    # Sample the newest if over the cap (genuine is in query order; sort by ts).
    genuine.sort(key=lambda r: r["ts"] or "")
    if len(genuine) > max_points:
        genuine = genuine[-max_points:]
        out["sampled"] = True

    # Turn outcomes need the full per-session prompt + tool ordering.
    kept_ids = {r["id"] for r in genuine}
    session_ids = {r["sid"] for r in genuine}
    session_prompts: dict[str, list] = {}
    for r in sorted(prows, key=lambda r: (r["sid"], r["ts"] or "")):
        if r["sid"] in session_ids:
            session_prompts.setdefault(r["sid"], []).append((r["id"], r["ts"]))
    tool_rows: dict[str, list] = {}
    with sqlite3.connect(str(CORPUS_DB)) as conn:
        qmarks = ",".join("?" * len(session_ids))
        for sid, ts, is_err in conn.execute(
            f"""SELECT session_id, timestamp, is_error FROM tool_calls
                 WHERE session_id IN ({qmarks}) ORDER BY session_id, timestamp""",
            tuple(session_ids),
        ):
            tool_rows.setdefault(sid, []).append((ts, bool(is_err)))
    errored_ids, had_tools = _classify_errors(session_prompts, tool_rows)

    # Embed + project (cached). vecmap is also reused below for the rephrase
    # signal, so we always need it in hand even when coords come from cache.
    cache = _connect_cache()
    try:
        key = _cache_key(model_name, sorted(kept_ids))
        id_text = [(r["id"], r["text"]) for r in genuine]
        vecmap, _dim = _embed_with_cache(cache, model_name, id_text)
        coords = {
            pid: (x, y) for pid, x, y in cache.execute(
                "SELECT prompt_id, x, y FROM projection WHERE cache_key = ?", (key,)
            )
        }
        if rebuild or set(coords) != kept_ids:
            ids_ordered = [pid for pid, _t in id_text if pid in vecmap]
            mat = [vecmap[pid] for pid in ids_ordered]
            xy = _project(mat)
            coords = {pid: (float(xy[i][0]), float(xy[i][1]))
                      for i, pid in enumerate(ids_ordered)}
            cache.execute("DELETE FROM projection WHERE cache_key = ?", (key,))
            cache.executemany(
                "INSERT INTO projection (cache_key, prompt_id, x, y) VALUES (?,?,?,?)",
                [(key, pid, x, y) for pid, (x, y) in coords.items()],
            )
            cache.commit()
    finally:
        cache.close()

    outcomes = _assign_outcomes(genuine, errored_ids, had_tools, vecmap)

    points = []
    counts = {k: 0 for k in OUTCOMES}
    for r in genuine:
        pid = r["id"]
        if pid not in coords:
            continue
        oc = outcomes.get(pid, "clean")
        counts[oc] += 1
        x, y = coords[pid]
        text = r["text"].strip().replace("\n", " ")
        points.append({
            "x": round(x, 3), "y": round(y, 3), "outcome": oc,
            "text": text[:240], "repo": _repo_label(r["repo"]),
            "date": (r["ts"] or "")[:10], "session_id": r["sid"],
        })
    out["points"] = points
    out["shown"] = len(points)
    out["outcomes"] = counts
    return out


def _repo_label(project_dir) -> str:
    if not project_dir:
        return "?"
    from watchmen.metrics import _repo_label as _rl
    return _rl(project_dir)
