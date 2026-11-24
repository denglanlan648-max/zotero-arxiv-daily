"""Daily economics discovery. Dry-run is the default; credentials come from the environment."""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

TOPICS = ["development economics", "agricultural economics", "labor economics", "public economics", "environmental economics", "health economics", "consumer economics", "applied microeconomics", "发展经济学", "农业经济", "劳动经济", "公共经济", "环境经济", "健康经济", "消费经济"]
HEADERS = {"User-Agent": "economics-literature-daily/1.0", "Accept": "application/json"}


def request(url, *, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={**HEADERS, **(headers or {}), **({"Content-Type": "application/json"} if data is not None else {})}, method=method)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 2: raise
            delay = exc.headers.get("Retry-After", "5")
            time.sleep(min(int(delay) if delay.isdigit() else 5, 30))


def title_key(s):
    return re.sub(r"[^\w]+", "", (s or "").casefold())


def doi_key(s):
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", (s or "").strip(), flags=re.I).casefold()


def abstract(index):
    return " ".join(word for _, word in sorted((position, word) for word, positions in (index or {}).items() for position in positions))


def clean(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def discover(days=7):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    papers, successes = [], 0
    for topic in TOPICS:
        for source in ("openalex", "crossref"):
            try:
                if source == "openalex":
                    params = urllib.parse.urlencode({"search": topic, "filter": "from_publication_date:" + since, "per-page": 50})
                    api_key = os.environ.get("OPENALEX_API_KEY")
                    works = request("https://api.openalex.org/works?" + params, headers={"Authorization": "Bearer " + api_key} if api_key else None)["results"]
                    for w in works:
                        location = w.get("primary_location") or {}
                        papers.append(dict(title=w.get("title") or "", doi=doi_key(w.get("doi")), abstract=abstract(w.get("abstract_inverted_index")), authors=[a.get("author", {}).get("display_name", "") for a in w.get("authorships", [])], date=w.get("publication_date") or "", journal=(location.get("source") or {}).get("display_name", ""), url=w.get("doi") or w.get("id") or "", source=source))
                else:
                    time.sleep(1)
                    params = urllib.parse.urlencode({"query.bibliographic": topic, "filter": "from-pub-date:" + since, "rows": 50})
                    works = request("https://api.crossref.org/works?" + params)["message"]["items"]
                    for w in works:
                        parts = (w.get("published", {}).get("date-parts") or [[]])[0]
                        papers.append(dict(title=(w.get("title") or [""])[0], doi=doi_key(w.get("DOI")), abstract=clean(w.get("abstract")), authors=[" ".join(filter(None, (a.get("given"), a.get("family")))) for a in w.get("author", [])], date="-".join(str(v).zfill(2) if i else str(v) for i, v in enumerate(parts)), journal=(w.get("container-title") or [""])[0], url=w.get("URL") or "", source=source))
                successes += 1
            except Exception as exc:
                print(f"{source} query failed ({topic}): {type(exc).__name__}: {exc}", file=sys.stderr)
    try:
        params = urllib.parse.urlencode({"search_query": "cat:econ.*", "start": 0, "max_results": 100, "sortBy": "submittedDate", "sortOrder": "descending"})
        req = urllib.request.Request("https://export.arxiv.org/api/query?" + params, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=45) as response:
            root = ET.fromstring(response.read())
        ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
        for entry in root.findall("a:entry", ns):
            date = (entry.findtext("a:published", default="", namespaces=ns))[:10]
            if date < since: continue
            papers.append(dict(title=" ".join(entry.findtext("a:title", default="", namespaces=ns).split()), doi=doi_key(entry.findtext("arxiv:doi", default="", namespaces=ns)), abstract=" ".join(entry.findtext("a:summary", default="", namespaces=ns).split()), authors=[x.findtext("a:name", default="", namespaces=ns) for x in entry.findall("a:author", ns)], date=date, journal=entry.findtext("arxiv:journal_ref", default="", namespaces=ns), url=entry.findtext("a:id", default="", namespaces=ns), source="arxiv"))
        successes += 1
    except Exception as exc:
        print(f"arxiv query failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    if not successes:
        raise RuntimeError("All literature sources failed")
    return papers


def deduplicate(papers, existing=()):
    dois, titles, result = set(), set(), []
    for p in existing:
        if doi_key(p.get("doi")): dois.add(doi_key(p["doi"]))
        if title_key(p.get("title")): titles.add(title_key(p["title"]))
    for p in papers:
        d, t = doi_key(p.get("doi")), title_key(p.get("title"))
        if not t: continue
        if t in titles or (d and d in dois):
            titles.add(t)
            if d: dois.add(d)
            # Combine complementary metadata when two discovery sources report the same paper.
            for old in result:
                if title_key(old.get("title")) == t or (d and doi_key(old.get("doi")) == d):
                    for field in ("doi", "abstract", "authors", "journal", "date", "url"):
                        if not old.get(field) and p.get(field): old[field] = p[field]
                    if d: dois.add(d)
                    titles.add(t)
                    break
            continue
        titles.add(t)
        if d: dois.add(d)
        result.append(p)
    return result


def zotero_existing(user, key):
    base = f"https://api.zotero.org/users/{urllib.parse.quote(user, safe='')}/items/top"
    items, start = [], 0
    while True:
        page = request(base + "?" + urllib.parse.urlencode({"limit": 100, "start": start}), headers={"Zotero-API-Key": key})
        items.extend({"title": x["data"].get("title", ""), "doi": x["data"].get("DOI", "")} for x in page)
        if len(page) < 100: break
        start += len(page)
    return items


def rank(papers, api_key, base_url, model):
    # Restrict the input to plausible candidates, then ask the model to assess research value.
    terms = re.compile(r"econom|labou?r|agricultur|farm|poverty|household|inequality|wage|employment|policy|health|consumer|climate|environment|education|tax|trade|develop|经济|农业|农村|农户|劳动|就业|工资|贫困|收入|消费|环境|健康|政策|教育|税收|贸易", re.I)
    candidates = [p for p in papers if terms.search(p["title"] + " " + p["abstract"])]
    candidates.sort(key=lambda p: (bool(p["abstract"]), bool(p["doi"]), p["date"]), reverse=True)
    candidates = candidates[:100]
    if not candidates: return []
    prompt = "Select up to five distinct papers most relevant to economics and applied microeconomics, assessing novelty, credible methods and research value from the supplied metadata. Do not invent evidence. Return JSON object with an indices array of zero-based integers, in best-first order.\n" + json.dumps([{"index": i, "title": p["title"], "abstract": p["abstract"][:1800]} for i, p in enumerate(candidates)], ensure_ascii=False)
    response = request(base_url.rstrip("/") + "/chat/completions", method="POST", headers={"Authorization": "Bearer " + api_key}, data={"model": model, "messages": [{"role": "user", "content": prompt}], "response_format": {"type": "json_object"}, "temperature": 0})
    indices = json.loads(response["choices"][0]["message"]["content"])["indices"]
    selected = []
    for i in indices:
        if type(i) is int and 0 <= i < len(candidates) and candidates[i] not in selected: selected.append(candidates[i])
        if len(selected) == 5: break
    return selected


def write_zotero(user, key, papers):
    base = f"https://api.zotero.org/users/{urllib.parse.quote(user, safe='')}"
    headers = {"Zotero-API-Key": key, "Zotero-API-Version": "3"}
    items = []
    for p in papers:
        item_type = "preprint" if p["source"] == "arxiv" and not p["journal"] else "journalArticle"
        item = {"itemType": item_type, "title": p["title"], "creators": [{"creatorType": "author", "name": a} for a in p["authors"] if a], "abstractNote": p["abstract"], "date": p["date"], "DOI": p["doi"], "url": p["url"], "tags": [{"tag": "economics daily"}, {"tag": p["source"]}]}
        if item_type == "journalArticle": item["publicationTitle"] = p["journal"]
        items.append(item)
    if items:
        result = request(base + "/items", method="POST", headers=headers, data=items)
        if result.get("failed") or len(result.get("successful", {})) + len(result.get("unchanged", {})) != len(items):
            raise RuntimeError("Zotero did not confirm all items: " + json.dumps(result.get("failed", {})))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Write to Zotero after validating a dry run")
    args = parser.parse_args()
    user, key = os.environ["ZOTERO_ID"], os.environ["ZOTERO_KEY"]
    papers = deduplicate(discover(), zotero_existing(user, key))
    selected = rank(papers, os.environ["OPENAI_API_KEY"], os.environ.get("OPENAI_API_BASE") or "https://api.openai.com/v1", os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    print(json.dumps({"dry_run": not args.live, "candidate_count": len(papers), "selected": selected}, ensure_ascii=False, indent=2))
    if args.live:
        # Recheck immediately before writing to reduce duplicate imports on retries.
        write_zotero(user, key, deduplicate(selected, zotero_existing(user, key)))


if __name__ == "__main__":
    main()
