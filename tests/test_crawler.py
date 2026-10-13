import csv
import gzip
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from html import escape
from pathlib import Path
from unittest.mock import patch

import httpx

from scripts.crawler import QuickerAction


def row(code="a", revision="1", content="initial"):
    # Same field locations and tooltip format as the archived Recent HTML.
    title = (f"Action {code}\n动作大小：1 KB\n创建时间：2020-01-01 12:00"
             f"\n修订版本：{revision}\n更新内容：{content}")
    return (f'<tr><td></td><td><a class="mr-1" href="/Sharedaction?code={code}" '
            f'title="{escape(title, quote=True)}">Action</a><span title="description"></span></td>'
            '<td><a><img title="common - 通用"></a></td>'
            '<td><a class="user-link" href="/User/1">Author</a>'
            '<div title="最后更新">1天3小时前</div></td></tr>')


def page(new="", updated="", tbody=False):
    parts = []
    for label, criterion, rows in (("新动作", "创建", new), ("最近更新", "更新", updated)):
        table = '<tr><th>名称</th></tr>' + rows
        if tbody:
            table = '<tbody>' + table + '</tbody>'
        parts.append(f'<h4>{label}<small>仅显示组合动作，{criterion}时间48小时内</small></h4>'
                     f'<div><table>{table}</table></div>')
    return ('<html><body>' + ''.join(parts) + '</body></html>').encode()


class CrawlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        client_patch = patch("scripts.crawler.httpx.Client")
        self.client = client_patch.start().return_value
        self.addCleanup(client_patch.stop)
        self.crawler = QuickerAction(self.data)
        # Running tests inside Actions must not set the actual step's outputs.
        output_patch = patch.dict(os.environ, {"GITHUB_OUTPUT": ""})
        output_patch.start()
        self.addCleanup(output_patch.stop)

    def run_page(self, html, when="2026-10-13T08:00:00+08:00"):
        with patch.object(self.crawler, "_fetch", return_value=html):
            return self.crawler.run_task(now=datetime.fromisoformat(when))

    def read_day(self, day="2026-10-13"):
        with (self.data / "csv" / f"quicker_actions_{day}.csv").open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            self.assertEqual(reader.fieldnames, self.crawler.csv_header)
            return list(reader)

    def snapshot(self):
        return {str(p.relative_to(self.data)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.data.rglob('*') if p.is_file()}

    def test_consecutive_days_skip_window_overlap_keep_revision_and_content_changes(self):
        self.run_page(page(row("a"), row("b")))
        first_day = (self.data / "csv/quicker_actions_2026-10-13.csv").read_bytes()
        changes = self.run_page(page(row("a"), row("b", "2") + row("c")), "2026-10-14T08:00:00+08:00")
        self.assertEqual([(r['动作名'], r['修订版本']) for r in changes], [("Action b", "2"), ("Action c", "1")])
        self.assertEqual({r['inputtime'] for r in self.read_day("2026-10-14")}, {"2026-10-14"})
        self.assertEqual((self.data / "csv/quicker_actions_2026-10-13.csv").read_bytes(), first_day)
        # Same revision, changed release notes is also a new observation.
        changes = self.run_page(page(updated=row("b", "2", "corrected\nnotes")), "2026-10-15T08:00:00+08:00")
        self.assertEqual(len(changes), 1)
        self.assertEqual(self.read_day("2026-10-15")[0]['更新内容'], "corrected\nnotes")

    def test_same_day_appends_versions_preserves_dropped_rows_and_sources(self):
        first = page(row("a") + row("b"))
        second = page(updated=row("a", "2"))
        self.run_page(first)
        self.run_page(second, "2026-10-13T21:00:00+08:00")
        self.assertEqual([(r['动作名'], r['修订版本']) for r in self.read_day()],
                         [("Action a", "1"), ("Action b", "1"), ("Action a", "2")])
        self.assertEqual(gzip.decompress((self.data / "html/quicker_actions_2026-10-13.html.gz").read_bytes()), first)
        extra = list((self.data / "html/2026-10-13").glob('*.html.gz'))
        self.assertEqual(len(extra), 1)
        self.assertEqual(gzip.decompress(extra[0].read_bytes()), second)
        before = self.snapshot()
        self.assertEqual(self.run_page(second), [])
        self.assertEqual(self.snapshot(), before)

    def test_duplicate_sections_and_rerun_are_idempotent(self):
        html = page(row(), row(), tbody=True)
        self.assertEqual(len(self.run_page(html)), 1)
        before = self.snapshot()
        self.assertEqual(self.run_page(html), [])
        self.assertEqual(self.snapshot(), before)
        # Relative time and counters may change without a revision change.
        self.run_page(html.replace('1天3小时前'.encode(), '1天4小时前'.encode()))
        self.assertEqual(self.snapshot(), before)

    def test_empty_valid_page_writes_header_once_and_does_not_erase_records(self):
        self.assertEqual(self.run_page(page()), [])
        self.assertEqual(self.read_day(), [])
        before = self.snapshot()
        self.run_page(page())
        self.assertEqual(self.snapshot(), before)
        self.run_page(page(row()))
        before = self.snapshot()
        self.run_page(page())
        self.assertEqual(len(self.read_day()), 1)
        self.assertEqual(self.snapshot(), before)

    def test_unchanged_second_day_writes_header_only(self):
        self.run_page(page(row()))
        self.run_page(page(row()), "2026-10-14T08:00:00+08:00")
        self.assertEqual(self.read_day("2026-10-14"), [])

    def test_old_fingerprint_does_not_reappear_after_an_empty_day(self):
        self.run_page(page(row()))
        self.run_page(page(), "2026-10-14T08:00:00+08:00")
        self.run_page(page(row()), "2026-10-15T08:00:00+08:00")
        self.assertEqual(self.read_day("2026-10-15"), [])

    def test_utc_midnight_boundary_uses_shanghai_date(self):
        self.run_page(page(row()), "2026-10-12T15:59:00+00:00")
        self.run_page(page(updated=row(revision="2")), "2026-10-12T16:01:00+00:00")
        self.assertEqual(self.read_day("2026-10-12")[0]['inputtime'], "2026-10-12")
        self.assertEqual(self.read_day("2026-10-13")[0]['inputtime'], "2026-10-13")

    def test_naive_observation_time_rejected(self):
        with self.assertRaises(ValueError):
            self.run_page(page(), "2026-10-13T08:00:00")
        self.assertEqual(self.snapshot(), {})

    def test_malformed_pages_do_not_modify_archives(self):
        self.run_page(page(row()))
        before = self.snapshot()
        invalid = [b'', b'<html>maintenance</html>',
                   page().replace('最近更新'.encode(), b'2026-10-13'),
                   page(row()).replace('修订版本：1'.encode(), '修订版本：'.encode()),
                   page().replace(b'<table>', b'<section>').replace(b'</table>', b'</section>'),
                   page().replace(b'</table>', b'<tr><td>broken action</td></tr></table>')]
        for html in invalid:
            with self.subTest(html=html[:80]):
                with self.assertRaises(ValueError):
                    self.run_page(html)
                self.assertEqual(self.snapshot(), before)

    def test_fetch_failure_propagates_without_writes(self):
        self.run_page(page(row()))
        before = self.snapshot()
        with patch.object(self.crawler, "_fetch", side_effect=ValueError("fetch failed")):
            with self.assertRaises(ValueError):
                self.crawler.run_task()
        self.assertEqual(self.snapshot(), before)

    def test_http_error_is_retried_and_raised(self):
        self.client.get.return_value = httpx.Response(503, request=httpx.Request("GET", "https://getquicker.net/Share/Recent"))
        with self.assertRaises(ValueError):
            self.crawler.run_task()
        self.assertEqual(self.client.get.call_count, 3)
        self.assertEqual(self.snapshot(), {})

    def test_corrupt_history_fails_before_writing(self):
        self.run_page(page(row()))
        (self.data / "csv/quicker_actions_2026-10-12.csv").write_text('wrong,header\n', encoding='utf-8')
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.run_page(page(row(revision="2")))
        self.assertEqual(self.snapshot(), before)

    def test_gzip_is_deterministic(self):
        compressed = self.crawler.gzencode("test")
        self.assertEqual(compressed, self.crawler.gzencode(b"test"))
        self.assertEqual(compressed[4:8], b'\x00\x00\x00\x00')

    def test_archived_october_samples_reproduce_27_duplicates_and_4_revisions(self):
        source = Path(__file__).resolve().parents[1] / "data"
        (self.data / "csv").mkdir()
        baseline = source / "csv/quicker_actions_2026-10-11.csv"
        copied = self.data / "csv" / baseline.name
        shutil.copyfile(baseline, copied)
        html = gzip.decompress((source / "html/quicker_actions_2026-10-12.html.gz").read_bytes())
        changes = self.run_page(html)
        self.assertEqual(len(self.crawler._extract(html.decode(), "2026-10-13")), 39)
        self.assertEqual(len(changes), 12)  # 39 current - 27 already observed
        old_urls = {r['动作URL'] for r in self.crawler._read_csv(copied)}
        revised = {r['动作名']: r['修订版本'] for r in changes if r['动作URL'] in old_urls}
        self.assertEqual(revised, {"窗口贴边速切": "7", "大图导入 Photoshop": "3", "Edge垂直书签": "11", "应用抽屉": "4"})
        self.assertEqual(copied.read_bytes(), baseline.read_bytes())
        before = self.snapshot()
        self.run_page(html)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
