import csv
import gzip
import hashlib
import io
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import httpx
from lxml import etree
from loguru import logger
from datetime import datetime
from zoneinfo import ZoneInfo


SITE_TIMEZONE = ZoneInfo("Asia/Shanghai")

class QuickerAction:
    def __init__(self, data_dir=None):
        self.host = "https://getquicker.net"
        self.data_dir = Path(data_dir) if data_dir is not None else Path(__file__).resolve().parents[1] / "data"
        self.client = httpx.Client(headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36',
        })
        self.csv_header = ['动作名', '动作URL', '动作简介', '适用于', '作者', '作者URL', '动作大小', '创建时间', '修订版本', '更新内容', 'inputtime']
    
    @staticmethod
    def gzencode(data):
        if type(data) == str:
            data = bytes(data, 'utf8') 
        s_out = gzip.compress(data, mtime=0)
        return s_out

    def _fetch(self):
        for _ in range(3):
            try:
                resp = self.client.get("https://getquicker.net/Share/Recent", timeout=30)
                resp.raise_for_status()
                return resp.content
            except Exception as e:
                print(e)
                continue
        raise ValueError("fetch url err")
    
    def _extract(self, response, observed_on):
        response = etree.HTML(response)
        if response is None:
            raise ValueError("Empty HTML response")
        ret = []
        # These are rolling 48-hour categories, NOT calendar-date headings.
        for label in ("新动作", "最近更新"):
            headings = [h for h in response.xpath('//h4')
                        if h.xpath('string(.)').strip().startswith(label)]
            if len(headings) != 1 or "48小时内" not in headings[0].xpath('string(.)'):
                raise ValueError(f"Missing or changed Recent section: {label}")
            tables = headings[0].xpath('following-sibling::*[1][self::div]/table')
            if len(tables) != 1 or not tables[0].xpath('./tr/th | ./tbody/tr/th'):
                raise ValueError(f"Missing or changed Recent table: {label}")
            for jobEle in tables[0].xpath('./tr[td] | ./tbody/tr[td]'):
                act_prop = {}
    
                links = jobEle.xpath('./td[2]//a[contains(concat(" ", normalize-space(@class), " "), " mr-1 ")]')
                if len(links) != 1 or not links[0].get("href", "").startswith("/Sharedaction?code="):
                    raise ValueError("Malformed action row")
                act_a = links[0]
                desc = act_a.attrib.get("title")
                desc_list = desc.split("\n", 4)
                exe = jobEle.xpath("string(./td[3]/a/img/@title)").split(' - ')
    
                act_prop["动作名"] = desc_list[0]
                act_prop["动作URL"] = self.host+act_a.attrib.get("href")
                act_prop["动作简介"] = jobEle.xpath('string(./td[2]//span/@title)')
                act_prop["适用于"] = exe[1] if len(exe)==2 else ''
                act_prop["作者"] = jobEle.xpath('string(./td[4]/a[contains(@class, "user-link")])').strip()
                act_prop["作者URL"] = self.host+jobEle.xpath('string(./td[4]/a[contains(@class, "user-link")]/@href)')
    
                for item in desc_list[1:]:
                    k, v = item.split("：", 1)
                    act_prop[k] = v
    
                if not all(key in act_prop for key in ("创建时间", "修订版本", "更新内容")) or not act_prop["修订版本"].strip():
                    raise ValueError("Missing revision metadata")
                act_prop["inputtime"] = observed_on
                ret.append(act_prop)
        return ret

    @staticmethod
    def _fingerprint(row):
        return tuple(row[key].replace("\r\n", "\n").strip()
                     for key in ("动作URL", "修订版本", "更新内容"))

    def _read_csv(self, path):
        with path.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != self.csv_header:
                raise ValueError(f"Unexpected CSV header: {path}")
            rows = list(reader)
            if any(set(row) != set(self.csv_header) or None in row.values() for row in rows):
                raise ValueError(f"Malformed CSV: {path}")
            return rows

    @staticmethod
    def _write_atomic(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=path.parent, delete=False) as f:
            temporary = Path(f.name)
            try:
                f.write(content)
                f.close()
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)

    def run_task(self, now=None):
        response = self._fetch()
        now = now if now is not None else datetime.now(SITE_TIMEZONE)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Observation time must be timezone-aware")
        observed_on = now.astimezone(SITE_TIMEZONE).date().isoformat()
        # Parse and validate before touching either archive; errors must fail CI.
        rows = self._extract(response.decode("utf-8"), observed_on)
        csv_path = self.data_dir / "csv" / f"quicker_actions_{observed_on}.csv"
        existing, seen = [], set()
        for archive in sorted((self.data_dir / "csv").glob("quicker_actions_????-??-??.csv")):
            archived = self._read_csv(archive)
            seen.update(self._fingerprint(row) for row in archived)
            if archive == csv_path:
                existing = archived
        added = []
        for row in rows:
            key = self._fingerprint(row)
            if key not in seen:
                seen.add(key)
                added.append(row)

        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=self.csv_header)
        writer.writeheader()
        writer.writerows(existing + added)

        # Keep the first daily source snapshot and each later source of new events.
        html_path = self.data_dir / "html" / f"quicker_actions_{observed_on}.html.gz"
        if not html_path.exists():
            self._write_atomic(html_path, self.gzencode(response))
        elif added:
            digest = hashlib.sha256(response).hexdigest()
            snapshot = html_path.parent / observed_on / f"{digest}.html.gz"
            if not snapshot.exists():
                self._write_atomic(snapshot, self.gzencode(response))
        if added or not csv_path.exists():
            self._write_atomic(csv_path, output.getvalue().encode("utf-8"))
        message = f"{observed_on} 新观察到 {len(added)} 条动作变化"
        logger.info(message)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
                f.write(f"message={message}\n")
        return added

if __name__ == "__main__":
    crawler = QuickerAction()
    try:
        crawler.run_task()
    finally:
        crawler.client.close()

