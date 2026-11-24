import unittest
from unittest.mock import patch
import os
import economics_daily as daily


class DailyTests(unittest.TestCase):
    def test_deduplicates_both_doi_and_title_including_existing(self):
        existing = [{"title": "Existing paper", "doi": "10.1/ABC"}]
        papers = [{"title": "Different", "doi": "https://doi.org/10.1/abc"},
                  {"title": "Existing PAPER!", "doi": ""},
                  {"title": "New paper", "doi": ""},
                  {"title": "New paper!", "doi": "10.2/123"}]
        self.assertEqual([p["title"] for p in daily.deduplicate(papers, existing)], ["New paper"])

    def test_abstract_reconstruction(self):
        self.assertEqual(daily.abstract({"world": [1], "hello": [0]}), "hello world")

    def test_rejects_partial_zotero_write(self):
        paper = dict(title="A", authors=[], abstract="", journal="", date="", doi="", url="", source="test")
        with patch.object(daily, "request", return_value={"failed": {"0": {"message": "error"}}}):
            with self.assertRaises(RuntimeError): daily.write_zotero("1", "key", [paper])

    def test_rank_validates_model_indices(self):
        paper = dict(title="Labor economics", abstract="", doi="", date="2026")
        response = {"choices": [{"message": {"content": '{"indices":[0,0,99,"0"]}'}}]}
        with patch.object(daily, "request", return_value=response):
            self.assertEqual(daily.rank([paper], "key", "https://example.com", "model"), [paper])

    def test_default_run_never_writes_to_zotero(self):
        paper = dict(title="农业经济", abstract="研究", doi="10.1/a", date="2026", authors=[], journal="", url="", source="test")
        with patch.dict(os.environ, {"ZOTERO_ID": "1", "ZOTERO_KEY": "key", "OPENAI_API_KEY": "key"}), \
             patch("sys.argv", ["economics_daily.py"]), \
             patch.object(daily, "discover", return_value=[paper]), \
             patch.object(daily, "zotero_existing", return_value=[]), \
             patch.object(daily, "rank", return_value=[paper]), \
             patch.object(daily, "write_zotero") as write, \
             patch("builtins.print"):
            daily.main()
            write.assert_not_called()


if __name__ == "__main__": unittest.main()
