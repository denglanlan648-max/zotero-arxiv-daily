from loguru import logger
from pyzotero import zotero
from omegaconf import DictConfig, ListConfig
from .utils import glob_match
from .retriever import get_retriever_cls
from .protocol import CorpusPaper
import random
from datetime import datetime
from .reranker import get_reranker_cls
from .construct_email import render_email
from .utils import send_email
from openai import OpenAI
from tqdm import tqdm
import re


def normalize_path_patterns(patterns: list[str] | ListConfig | None, config_key: str) -> list[str] | None:
    if patterns is None:
        return None

    if not isinstance(patterns, (list, ListConfig)):
        raise TypeError(
            f"config.zotero.{config_key} must be a list of glob patterns or null, "
            'for example ["2026/survey/**"]. Single strings are not supported.'
        )

    if any(not isinstance(pattern, str) for pattern in patterns):
        raise TypeError(f"config.zotero.{config_key} must contain only glob pattern strings.")

    return list(patterns)


class Executor:
    def __init__(self, config:DictConfig):
        self.config = config
        self.include_path_patterns = normalize_path_patterns(config.zotero.include_path, "include_path")
        self.ignore_path_patterns = normalize_path_patterns(config.zotero.ignore_path, "ignore_path")
        self.retrievers = {
            source: get_retriever_cls(source)(config) for source in config.executor.source
        }
        self.reranker = get_reranker_cls(config.executor.reranker)(config)
        self.openai_client = OpenAI(api_key=config.llm.api.key, base_url=config.llm.api.base_url)
    def fetch_zotero_corpus(self) -> list[CorpusPaper]:
        logger.info("Fetching zotero corpus")
        zot = zotero.Zotero(self.config.zotero.user_id, 'user', self.config.zotero.api_key)
        collections = zot.everything(zot.collections())
        collections = {c['key']:c for c in collections}
        corpus = zot.everything(zot.items(itemType='conferencePaper || journalArticle || preprint'))
        corpus = [c for c in corpus if c['data']['abstractNote'] != '']
        def get_collection_path(col_key:str) -> str:
            if p := collections[col_key]['data']['parentCollection']:
                return get_collection_path(p) + '/' + collections[col_key]['data']['name']
            else:
                return collections[col_key]['data']['name']
        for c in corpus:
            paths = [get_collection_path(col) for col in c['data']['collections']]
            c['paths'] = paths
        logger.info(f"Fetched {len(corpus)} zotero papers")
        return [CorpusPaper(
            title=c['data']['title'],
            abstract=c['data']['abstractNote'],
            added_date=datetime.strptime(c['data']['dateAdded'], '%Y-%m-%dT%H:%M:%SZ'),
            paths=c['paths']
        ) for c in corpus]
    
    def filter_corpus(self, corpus:list[CorpusPaper]) -> list[CorpusPaper]:
        if self.include_path_patterns:
            logger.info(f"Selecting zotero papers matching include_path: {self.include_path_patterns}")
            corpus = [
                c for c in corpus
                if any(
                    glob_match(path, pattern)
                    for path in c.paths
                    for pattern in self.include_path_patterns
                )
            ]
        if self.ignore_path_patterns:
            logger.info(f"Excluding zotero papers matching ignore_path: {self.ignore_path_patterns}")
            corpus = [
                c for c in corpus
                if not any(
                    glob_match(path, pattern)
                    for path in c.paths
                    for pattern in self.ignore_path_patterns
                )
            ]
        if self.include_path_patterns or self.ignore_path_patterns:
            samples = random.sample(corpus, min(5, len(corpus)))
            samples = '\n'.join([c.title + ' - ' + '\n'.join(c.paths) for c in samples])
            logger.info(f"Selected {len(corpus)} zotero papers:\n{samples}\n...")
        return corpus

    
    def _deduplicate(self, papers):
        unique = {}
        for paper in papers:
            doi = (getattr(paper, "doi", None) or "").lower().replace("https://doi.org/", "").strip()
            key = "doi:" + doi if doi else "title:" + re.sub(r"[^a-z0-9]+", "", paper.title.lower())
            if key not in unique:
                unique[key] = paper
            else:
                old = unique[key]
                for field in ("doi", "publication", "publication_date", "abstract"):
                    if not getattr(old, field, None) and getattr(paper, field, None):
                        setattr(old, field, getattr(paper, field))
        return list(unique.values())

    def _write_zotero(self, papers):
        if self.config.executor.get("dry_run", True):
            logger.info("DRY RUN: Zotero write is disabled; no library items were created.")
            return
        zot = zotero.Zotero(self.config.zotero.user_id, 'user', self.config.zotero.api_key)
        collection_name = self.config.executor.get("zotero_collection", "Economics Daily")
        matches = [c for c in zot.everything(zot.collections()) if c["data"]["name"] == collection_name]
        if matches:
            collection_key = matches[0]["key"]
        else:
            created = zot.create_collections([{"name": collection_name}])
            collection_key = created["successful"]["0"]["key"]
        items = []
        for paper in papers:
            item = zot.item_template("journalArticle")
            item["title"] = paper.title
            item["abstractNote"] = paper.abstract or ""
            item["DOI"] = getattr(paper, "doi", None) or ""
            item["publicationTitle"] = getattr(paper, "publication", None) or ""
            item["date"] = getattr(paper, "publication_date", None) or ""
            item["url"] = paper.url
            item["creators"] = [{"creatorType": "author", "name": a} for a in (paper.authors or []) if a]
            item["collections"] = [collection_key]
            item["tags"] = [{"tag": "economics"}, {"tag": paper.source}]
            items.append(item)
        for offset in range(0, len(items), 50):
            result = zot.create_items(items[offset:offset + 50])
            if result.get("failed"):
                logger.warning(f"Some Zotero records failed to import: {len(result['failed'])}")
        logger.info(f"Zotero import attempted for {len(items)} papers into '{collection_name}'")

    def run(self):
        corpus = self.fetch_zotero_corpus()
        corpus = self.filter_corpus(corpus)
        if len(corpus) == 0:
            logger.error("No Zotero corpus found; cannot personalize recommendations.")
            return
        all_papers = []
        for source, retriever in self.retrievers.items():
            logger.info(f"Retrieving {source} papers...")
            try:
                papers = retriever.retrieve_papers()
                logger.info(f"Retrieved {len(papers)} {source} papers")
                all_papers.extend(papers)
            except Exception as exc:
                logger.exception(f"Retriever {source} failed: {exc}")
        all_papers = self._deduplicate(all_papers)
        logger.info(f"{len(all_papers)} unique papers after DOI/title deduplication")
        ranked = self.reranker.rerank(all_papers, corpus) if all_papers else []
        ranked = ranked[:int(self.config.executor.get("top_papers", 5))]
        if ranked:
            logger.info("Generating TLDR and affiliations...")
            for paper in tqdm(ranked):
                paper.generate_tldr(self.openai_client, self.config.llm)
                paper.generate_affiliations(self.openai_client, self.config.llm)
        logger.info(f"Selected {len(ranked)} papers")
        for i, paper in enumerate(ranked, 1):
            logger.info(f"{i}. {paper.title} | DOI={getattr(paper, 'doi', None)} | score={paper.score}")
        self._write_zotero(ranked)
