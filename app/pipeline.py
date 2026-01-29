"""
ai-doctor-assistant :: pipeline.py

Goal:
- Read ONLY from local approved PDFs you uploaded in /data/pdfs
- Retrieve relevant passages with file+page citations
- Produce a structured clinical output (assistive, not diagnostic)
- If info isn't found in sources -> say so clearly (no guessing)

How to run (later on your laptop):
    python -m app.pipeline --question "Adult shortness of breath approach" --top_k 8
"""

from __future__ import annotations

import argparse
import os
import re
import math
from dataclasses import dataclass
from typing import List, Tuple, Dict, Optional

# --- Config (repo-relative paths) ---
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PDF_DIR = os.path.join(REPO_ROOT, "data", "pdfs")
STYLE_GUIDE_PATH = os.path.join(REPO_ROOT, "style_guide.md")

# --- Safety / discipline ---
SAFETY_NOTICE = (
    "Clinical decision support only (assistive, not diagnostic). "
    "Final clinical decisions must be made by a licensed physician."
)

# =========================
# Data structures
# =========================

@dataclass
class SourceSpan:
    pdf_file: str
    page: int  # 1-based page number
    text: str

@dataclass
class Chunk:
    pdf_file: str
    page: int
    text: str
    tokens: List[str]

# =========================
# Utilities
# =========================

def normalize_text(s: str) -> str:
    s = s.replace("\x00", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def simple_tokenize(s: str) -> List[str]:
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s\-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return []
    return s.split()

def list_pdfs(pdf_dir: str) -> List[str]:
    if not os.path.isdir(pdf_dir):
        return []
    files = []
    for fn in os.listdir(pdf_dir):
        if fn.lower().endswith(".pdf"):
            files.append(os.path.join(pdf_dir, fn))
    return sorted(files)

# =========================
# PDF Loading
# =========================

def extract_pdf_pages(pdf_path: str) -> List[SourceSpan]:
    """
    Extract text per page with (file, page, text).
    Tries 'pypdf' first. If missing, raises a helpful error.
    """
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "Missing dependency 'pypdf'. Install later with: pip install pypdf"
        ) from e

    reader = PdfReader(pdf_path)
    spans: List[SourceSpan] = []
    base = os.path.basename(pdf_path)

    for i, page in enumerate(reader.pages):
        raw = page.extract_text() or ""
        txt = normalize_text(raw)
        if txt:
            spans.append(SourceSpan(pdf_file=base, page=i + 1, text=txt))
    return spans

def make_chunks(spans: List[SourceSpan], max_chars: int = 1200, overlap_chars: int = 200) -> List[Chunk]:
    """
    Split each page into overlapping chunks for retrieval.
    """
    chunks: List[Chunk] = []
    for sp in spans:
        text = sp.text
        if len(text) <= max_chars:
            toks = simple_tokenize(text)
            chunks.append(Chunk(pdf_file=sp.pdf_file, page=sp.page, text=text, tokens=toks))
            continue

        start = 0
        while start < len(text):
            end = min(len(text), start + max_chars)
            piece = text[start:end]
            piece = normalize_text(piece)
            if piece:
                toks = simple_tokenize(piece)
                chunks.append(Chunk(pdf_file=sp.pdf_file, page=sp.page, text=piece, tokens=toks))
            if end >= len(text):
                break
            start = max(0, end - overlap_chars)
    return chunks

# =========================
# Retrieval (no internet)
# =========================

class SimpleRetriever:
    """
    Lightweight TF-IDF-ish scoring without external libs.
    - Builds document frequency over chunks
    - Scores by sum(tf * idf) over query tokens
    """
    def __init__(self, chunks: List[Chunk]):
        self.chunks = chunks
        self.df: Dict[str, int] = {}
        self.N = len(chunks)

        for ch in chunks:
            seen = set(ch.tokens)
            for t in seen:
                self.df[t] = self.df.get(t, 0) + 1

    def idf(self, term: str) -> float:
        # Smoothed IDF
        df = self.df.get(term, 0)
        return math.log((self.N + 1) / (df + 1)) + 1.0

    def score_chunk(self, q_tokens: List[str], ch: Chunk) -> float:
        if not q_tokens or not ch.tokens:
            return 0.0
        # term frequency in chunk
        tf: Dict[str, int] = {}
        for t in ch.tokens:
            tf[t] = tf.get(t, 0) + 1

        score = 0.0
        for qt in q_tokens:
            if qt in tf:
                score += (1 + math.log(tf[qt])) * self.idf(qt)
        return score

    def search(self, query: str, top_k: int = 8) -> List[Tuple[float, Chunk]]:
        q_tokens = simple_tokenize(query)
        scored: List[Tuple[float, Chunk]] = []
        for ch in self.chunks:
            s = self.score_chunk(q_tokens, ch)
            if s > 0:
                scored.append((s, ch))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:top_k]

# =========================
# Output formatting (your style guide)
# =========================

def load_style_guide(path: str) -> str:
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def format_citation(pdf_file: str, page: int) -> str:
    # concise citation format
    return f"[{pdf_file} p.{page}]"

def build_assistive_output(question: str, hits: List[Tuple[float, Chunk]], language: str = "en") -> str:
    """
    IMPORTANT:
    - We do NOT invent missing content
    - We surface only what is found, with citations
    - We keep structure consistent (matches your style_guide concept)
    """

    if language not in ("en", "ar"):
        language = "en"

    if language == "ar":
        # We keep medical logic same; only headings change.
        H = {
            "title": "إجابة داعمة (من المصادر المرفوعة فقط)",
            "summary": "ملخص سريري",
            "redflags": "إنذارات خطر",
            "ddx": "تشخيصات تفريقية (مرتبة)",
            "workup": "فحوصات/تقييم أولي",
            "management": "تدبير أولي",
            "refs": "المراجع (مقاطع داعمة)",
            "notfound": "غير موجود في المصادر المرفوعة",
        }
    else:
        H = {
            "title": "Assistive answer (ONLY from uploaded sources)",
            "summary": "Clinical Summary",
            "redflags": "Red Flags (Must-Not-Miss)",
            "ddx": "Differential Diagnosis (Ranked)",
            "workup": "Recommended Initial Workup",
            "management": "Initial Management",
            "refs": "Supporting Extracts (with citations)",
            "notfound": "Not found in the provided references",
        }

    lines: List[str] = []
    lines.append(f"# {H['title']}")
    lines.append("")
    lines.append(f"**Question:** {question}")
    lines.append("")
    lines.append(f"**Safety notice:** {SAFETY_NOTICE}")
    lines.append("")

    if not hits:
        lines.append(f"## {H['summary']}")
        lines.append(f"- {H['notfound']}.")
        lines.append("")
        lines.append(f"## {H['refs']}")
        lines.append(f"- {H['notfound']}.")
        return "\n".join(lines)

    # We do a conservative approach:
    # - Provide a short “what the sources mention” summary (not diagnostic)
    # - Provide extracted bullets grouped as: red flags / ddx / workup / management
    # Since we don't run an LLM here, we only quote small snippets (not large blocks).
    # You can later add a rewrite step (LLM) that paraphrases while keeping citations.

    # Create small snippet bullets
    extracts: List[str] = []
    used = set()
    for score, ch in hits:
        key = (ch.pdf_file, ch.page, ch.text[:80])
        if key in used:
            continue
        used.add(key)
        snippet = ch.text
        # Keep snippet short to avoid dumping whole pages
        snippet = snippet[:400].strip()
        snippet = re.sub(r"\s+", " ", snippet)
        extracts.append(f"- {snippet} {format_citation(ch.pdf_file, ch.page)}")

    # Heuristic grouping by keywords (simple, but useful)
    def pick_by_keywords(keywords: List[str], max_items: int = 6) -> List[str]:
        out = []
        for ex in extracts:
            low = ex.lower()
            if any(k in low for k in keywords):
                out.append(ex)
            if len(out) >= max_items:
                break
        return out

    redflags = pick_by_keywords(["red flag", "hypotension", "syncope", "shock", "altered", "cyanosis", "hemoptysis", "chest pain"])
    workup = pick_by_keywords(["ecg", "troponin", "x-ray", "ct", "d-dimer", "abg", "vbg", "labs", "imaging", "ultrasound", "spo2", "pulse oximetry"])
    management = pick_by_keywords(["oxygen", "bronchodilator", "nebul", "antibiotic", "anticoag", "diuretic", "steroid", "epinephrine", "intub", "ventilation"])
    ddx = pick_by_keywords(["differential", "asthma", "copd", "pneumonia", "pe", "pulmonary embol", "heart failure", "acs", "pneumothorax", "anxiety"])

    lines.append(f"## {H['summary']}")
    lines.append("- Summary is based on extracted passages below (assistive; not a final diagnosis).")
    lines.append("")

    lines.append(f"## {H['redflags']}")
    if redflags:
        lines.extend(redflags)
    else:
        lines.append(f"- {H['notfound']} for explicit red-flag statements in top matches.")
    lines.append("")

    lines.append(f"## {H['ddx']}")
    if ddx:
        lines.extend(ddx)
    else:
        lines.append(f"- {H['notfound']} for explicit DDx statements in top matches.")
    lines.append("")

    lines.append(f"## {H['workup']}")
    if workup:
        lines.extend(workup)
    else:
        lines.append(f"- {H['notfound']} for explicit workup statements in top matches.")
    lines.append("")

    lines.append(f"## {H['management']}")
    if management:
        lines.extend(management)
    else:
        lines.append(f"- {H['notfound']} for explicit management statements in top matches.")
    lines.append("")

    lines.append(f"## {H['refs']}")
    # Always show the top extracts (even if not grouped)
    lines.extend(extracts[: min(10, len(extracts))])

    return "\n".join(lines)

# =========================
# Main pipeline
# =========================

def build_corpus_from_pdfs(pdf_dir: str) -> List[Chunk]:
    pdfs = list_pdfs(pdf_dir)
    if not pdfs:
        raise RuntimeError(f"No PDFs found in: {pdf_dir}")

    all_chunks: List[Chunk] = []
    for pdf in pdfs:
        spans = extract_pdf_pages(pdf)
        chunks = make_chunks(spans)
        all_chunks.extend(chunks)

    if not all_chunks:
        raise RuntimeError("PDFs were found but no extractable text was produced (may be scanned images).")
    return all_chunks

def main():
    parser = argparse.ArgumentParser(description="Local clinical PDF retrieval (assistive).")
    parser.add_argument("--question", required=True, help="Clinical question or scenario")
    parser.add_argument("--top_k", type=int, default=8, help="Number of top chunks to retrieve")
    parser.add_argument("--lang", type=str, default="en", help="en or ar (headings only)")
    args = parser.parse_args()

    _ = load_style_guide(STYLE_GUIDE_PATH)  # reserved for later rewrite step
    chunks = build_corpus_from_pdfs(PDF_DIR)
    retriever = SimpleRetriever(chunks)

    hits = retriever.search(args.question, top_k=args.top_k)
    out = build_assistive_output(args.question, hits, language=args.lang)
    print(out)

if __name__ == "__main__":
    main()
