"""Economics literature discovery using OpenAlex and Crossref."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json
import re
from loguru import logger

from .base import BaseRetriever, register_retriever
from ..protocol import Paper

def _get_json(url: str) -> dict:
    req = Request(url, headers={"User-Agent": "zotero-arxiv-daily/1.1 (mailto:research@example.org)"})
    with urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))

def _abstract_from_inverted(index: dict | None) -> str:
    if not index:
        return ""
    words = {}
    for word, positions in index.items():
        for pos in positions:
            words[int(pos)] = word
    return " ".join(words[k] for k in sorted(words))

def _norm_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())

@register_retriever("economics")
class EconomicsRetriever(BaseRetriever):
    """Fetch recent economics papers from OpenAlex and Crossref; DOI/title dedup downstream."""

    def _retrieve_raw_papers(self) -> list[dict]:
        days = int(self.retriever_config.get("days_back", 3))
        limit = int(self.retriever_config.get("max_results_per_source", 100))
        since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        topics = list(self.retriever_config.get("topics", []))
        results: list[dict] = []
        seen: set[str] = set()

        for topic in topics:
            try:
                params = urlencode({"search": topic, "filter": f"from_publication_date:{since}", "per-page": min(limit, 100), "mailto": "research@example.org"})
                data = _get_json("https://api.openalex.org/works?" + params)
                for work in data.get("results", []):
                    title = work.get("title") or ""
                    doi = (work.get("doi") or "").replace("https://doi.org/", "").lower() or None
                    key = doi or _norm_title(title)
                    if not title or key in seen:
                        continue
                    seen.add(key)
                    results.append({"title": title, "doi": doi, "abstract": _abstract_from_inverted(work.get("abstract_inverted_index")), "authors": [a.get("author", {}).get("display_name", "") for a in work.get("authorships", [])], "url": work.get("doi") or work.get("id"), "date": work.get("publication_date"), "journal": (work.get("primary_location") or {}).get("source", {}).get("display_name", "") if (work.get("primary_location") or {}).get("source") else ""})
            except Exception as exc:
                logger.warning(f"OpenAlex query failed for '{topic}': {exc}")
            try:
                params = urlencode({"query": topic, "filter": f"from-pub-date:{since}", "rows": min(limit, 100), "select": "DOI,title,author,abstract,published,container-title,URL"})
                data = _get_json("https://api.crossref.org/works?" + params)
                for work in data.get("message", {}).get("items", []):
                    title = (work.get("title") or [""])[0]
                    doi = (work.get("DOI") or "").lower() or None
                    key = doi or _norm_title(title)
                    if not title or key in seen:
                        continue
                    seen.add(key)
                    abstract = re.sub(r"<[^>]+>", " ", work.get("abstract") or "")
                    authors = [(" ".join(filter(None, [a.get("given"), a.get("family")]))) for a in work.get("author", [])]
                    date_parts = (work.get("published", {}).get("date-parts") or [[None]])[0]
                    date = "-".join(str(x).zfill(2) if i == 1 else str(x) for i, x in enumerate(date_parts) if x)
                    results.append({"title": title, "doi": doi, "abstract": abstract, "authors": authors, "url": work.get("URL") or (f"https://doi.org/{doi}" if doi else ""), "date": date, "journal": (work.get("container-title") or [""])[0]})
            except Exception as exc:
                logger.warning(f"Crossref query failed for '{topic}': {exc}")
        return results

    def convert_to_paper(self, raw_paper: dict) -> Paper | None:
        return Paper(source=self.name, title=raw_paper["title"], authors=raw_paper.get("authors") or [], abstract=raw_paper.get("abstract") or "", url=raw_paper.get("url") or "", doi=raw_paper.get("doi"), publication=raw_paper.get("journal"), publication_date=raw_paper.get("date"))
