"""Plagiarism detection between teams.

1. Fingerprint each repo: normalised token 5-grams (identifiers, numbers and
   strings replaced by placeholders, so renaming variables does not hide a
   copy), reduced by winnowing.
2. Drop boilerplate: fingerprints present in many repos (framework scaffolds,
   starter code) cannot show copying between two teams.
3. MinHash LSH finds candidate pairs without comparing every pair.
4. Candidates are measured exactly and, if JPlag is configured, confirmed by
   JPlag on that pair only.
5. Pairs above the threshold go to the human queue. Nothing is automatic.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import tempfile
from collections import Counter
from pathlib import Path

import numpy as np
from sqlalchemy import select

from . import db, jobs
from .config import get_settings
from .models import ACTIVE, PlagiarismPair, Submission
from .quality import iter_code_files
from .review import raise_flag
from .snapshot import code_path
from .util import run

log = logging.getLogger("hackeval.plagiarism")

KEYWORDS = set("""
if else elif for while do switch case break continue return function def class import from export default
const let var new try catch finally throw raise with as async await yield lambda public private protected
static void int float double bool boolean string char long short struct interface enum extends implements
package func go defer select chan map type range nil null None True False true false this self super in is
not and or of println print echo include require module use fn mut impl trait match where let pub crate
""".split())

_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/|#[^\n]*|<!--.*?-->|\"\"\".*?\"\"\"|'''.*?'''", re.S)
_TOKENS = re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*|\d[\w.]*|"
                     r"==|!=|<=|>=|=>|->|&&|\|\||\+\+|--|[{}()\[\];,.:=+\-*/%<>!&|^~?@]")


def tokenize(text: str) -> list[str]:
    text = _COMMENTS.sub(" ", text)
    out = []
    for t in _TOKENS.findall(text):
        c = t[0]
        if c in "\"'`":
            out.append("S")
        elif c.isdigit():
            out.append("N")
        elif c.isalpha() or c in "_$":
            out.append(t if t in KEYWORDS else "V")
        else:
            out.append(t)
    return out


def _h(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "little")


def winnow(tokens: list[str], k: int, w: int) -> set[int]:
    if len(tokens) < k:
        return set()
    hashes = [_h(" ".join(tokens[i:i + k])) for i in range(len(tokens) - k + 1)]
    if len(hashes) <= w:
        return {min(hashes)}
    return {min(hashes[i:i + w]) for i in range(len(hashes) - w + 1)}


def fingerprint_dir(root: Path) -> np.ndarray:
    cfg = get_settings().plagiarism
    fps: set[int] = set()
    for p, _rel in iter_code_files(root):
        try:
            if p.stat().st_size > 500_000:
                continue
            fps |= winnow(tokenize(p.read_text(errors="ignore")), cfg.shingle_k, cfg.winnow_window)
        except OSError:
            continue
    return np.array(sorted(fps), dtype=np.uint64)


def fp_path(sub_id: int) -> Path:
    return get_settings().path("fingerprints") / f"{sub_id}.npy"


@jobs.handler("fingerprint")
def fingerprint_job(p: dict) -> dict:
    sub_id = p["submission_id"]
    with db.session() as s:
        root = code_path(s, sub_id)
    if root is None:
        raise jobs.Retry(300, "full code not fetched yet")
    arr = fingerprint_dir(root)
    np.save(fp_path(sub_id), arr)
    return {"fingerprints": int(arr.size)}


def _starter_fingerprints() -> set[int]:
    """Starter code shipped with a track is never evidence of copying."""
    out: set[int] = set()
    root = get_settings().tracks_dir
    if root.exists():
        for d in root.glob("*/starter"):
            out |= set(fingerprint_dir(d).tolist())
    return out


def _main_language(root: Path) -> str:
    exts = Counter(p.suffix.lower() for p, _ in iter_code_files(root))
    m = {".py": "python3", ".java": "java", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
         ".tsx": "typescript", ".go": "go", ".kt": "kotlin", ".cs": "csharp", ".cpp": "cpp", ".cc": "cpp",
         ".c": "c", ".rs": "rust", ".swift": "swift", ".scala": "scala"}
    for ext, _n in exts.most_common():
        if ext in m:
            return m[ext]
    return "text"


def jplag_pair(a: Path, b: Path) -> float | None:
    """Similarity (0-1) from JPlag for one pair, or None if JPlag is not set up or fails."""
    cfg = get_settings().plagiarism
    if not cfg.jplag_jar or not Path(cfg.jplag_jar).exists() or not shutil.which(cfg.java_bin):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "subs"
        root.mkdir()
        (root / "a").symlink_to(a.resolve(), target_is_directory=True)
        (root / "b").symlink_to(b.resolve(), target_is_directory=True)
        result = Path(tmp) / "result"
        r = run([cfg.java_bin, "-jar", cfg.jplag_jar, "-l", _main_language(a), "-r", str(result),
                 "--csv-export", "--mode", "RUN", str(root)], timeout=600)
        if r.code != 0:
            log.warning("JPlag failed: %s", r.err[-300:])
            return None
        for csv_file in Path(tmp).rglob("*.csv"):
            for line in csv_file.read_text().splitlines()[1:]:
                nums = re.findall(r"\d+\.\d+|\d+", line.split(",", 2)[-1] if "," in line else line)
                if nums:
                    v = float(nums[0])
                    return v / 100 if v > 1 else v
    return None


def run_plagiarism(min_round: int = 2) -> dict:
    """Barrier step after all fingerprint jobs: find, confirm and flag similar pairs."""
    from datasketch import MinHash, MinHashLSH

    cfg = get_settings().plagiarism
    if not cfg.enabled:
        return {"disabled": True, "note": "plagiarism.enabled is false for this event"}
    with db.session() as s:
        ids = [x.id for x in s.scalars(select(Submission).where(Submission.status == ACTIVE,
                                                                Submission.round >= min_round))]
    fps = {i: np.load(fp_path(i)) for i in ids if fp_path(i).exists()}
    if len(fps) < 2:
        return {"repos": len(fps), "pairs": 0}
    allv = np.concatenate(list(fps.values()))
    vals, counts = np.unique(allv, return_counts=True)
    limit = max(3, int(cfg.boilerplate_df * len(fps)))
    common = set(vals[counts > limit].tolist()) | _starter_fingerprints()
    common_arr = np.array(sorted(common), dtype=np.uint64)
    sets = {i: np.setdiff1d(a, common_arr, assume_unique=True) for i, a in fps.items()}

    lsh = MinHashLSH(threshold=cfg.lsh_threshold, num_perm=cfg.num_perm)
    mh = {}
    for i, a in sets.items():
        if a.size < 20:
            continue
        m = MinHash(num_perm=cfg.num_perm, hashfunc=lambda v: int(v) & 0xFFFFFFFF)
        m.update_batch(a.tolist())
        mh[i] = m
        lsh.insert(str(i), m)

    seen, flagged, candidates = set(), 0, 0
    best: dict[int, float] = {}
    with db.session() as s:
        for i, m in mh.items():
            for other in lsh.query(m):
                j = int(other)
                key = (min(i, j), max(i, j))
                if i == j or key in seen:
                    continue
                seen.add(key)
                a, b = sets[key[0]], sets[key[1]]
                inter = np.intersect1d(a, b, assume_unique=True).size
                containment = inter / max(1, min(a.size, b.size))
                if containment < cfg.candidate_similarity:
                    continue
                candidates += 1
                est = m.jaccard(mh[j])
                ca, cb = code_path(s, key[0]), code_path(s, key[1])
                jp = jplag_pair(ca, cb) if ca and cb else None
                final = jp if jp is not None else containment
                pair = s.scalars(select(PlagiarismPair).where(PlagiarismPair.a_id == key[0],
                                                              PlagiarismPair.b_id == key[1])).first()
                if pair is None:
                    pair = PlagiarismPair(a_id=key[0], b_id=key[1])
                    s.add(pair)
                pair.minhash_est, pair.similarity, pair.jplag_similarity = est, containment, jp
                pair.method = "jplag" if jp is not None else "winnowing-containment"
                pair.detail = {"shared_fingerprints": int(inter), "size_a": int(a.size), "size_b": int(b.size)}
                for x in key:
                    best[x] = max(best.get(x, 0.0), final)
                if final >= cfg.flag_similarity:
                    ev = {"similarity": round(final, 3), "method": pair.method,
                          "containment": round(containment, 3), "jplag": jp, **pair.detail}
                    raise_flag(s, key[0], 2, "plagiarism", f"Code is {final:.0%} similar to another team's.", ev,
                               related_id=key[1])
                    raise_flag(s, key[1], 2, "plagiarism", f"Code is {final:.0%} similar to another team's.", ev,
                               related_id=key[0])
                    flagged += 1
    get_settings().path("r2").joinpath("max_similarity.json").write_text(
        __import__("json").dumps({str(k): v for k, v in best.items()}))
    return {"repos": len(fps), "boilerplate_fingerprints": len(common), "candidates": candidates,
            "flagged_pairs": flagged}


def max_similarity() -> dict[int, float]:
    import json

    p = get_settings().path("r2") / "max_similarity.json"
    return {int(k): v for k, v in json.loads(p.read_text()).items()} if p.exists() else {}
