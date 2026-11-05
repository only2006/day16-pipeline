from pathlib import Path

DOCS_DIR = Path("docs")


def load_docs() -> list[tuple[str, str]]:
    if not DOCS_DIR.exists():
        return []
    out = []
    for p in sorted(DOCS_DIR.glob("*.md")) + sorted(DOCS_DIR.glob("*.txt")):
        out.append((p.name, p.read_text(encoding="utf-8", errors="ignore")))
    return out


def search(query: str, k: int = 3) -> list[dict]:
    q = set(query)
    scored = []
    for name, text in load_docs():
        for para in text.split("\n\n"):
            para = para.strip()
            if len(para) < 30:
                continue
            score = len(q & set(para)) / max(len(q), 1)
            scored.append((score, name, para))
    scored.sort(key=lambda x: -x[0])
    return [
        {"source": name, "text": para, "score": round(s, 3)}
        for s, name, para in scored[:k]
    ]