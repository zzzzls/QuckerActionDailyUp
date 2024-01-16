import csv
import httpx
from lxml import etree
from loguru import logger
from datetime import datetime, timedelta

logger.add("../data/log/quicker.log", rotation="10 MB", compression='tar.gz')

class QuickerAction:
    def __init__(self):
        self.host = "https://getquicker.net"
        self.client = httpx.Client(headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36',
        })
        self.today = datetime.now().strftime("%Y-%m-%d")
        self.yesterday = (datetime.now()-timedelta(days=1)).strftime("%Y-%m-%d")
        self.csv_header = ['动作名', '动作URL', '动作简介', '适用于', '作者', '作者URL', '动作大小', '创建时间', '修订版本', '更新内容', 'inputtime']
    
    def _fetch(self):
        for _ in range(3):
            try:
                resp = self.client.get("https://getquicker.net/Share/Recent")
                assert resp.status_code == 200
                return resp.content.decode("utf-8")
            except:
                continue
    
    def _extract(self, response):
        response = etree.HTML(response)
        yesterdayJob = response.xpath('//h4[text()="昨日更新"]/following-sibling::div[1]/table')[0]

        ret = []
        for jobEle in yesterdayJob.xpath('./tr[position()>1]'):
            act_prop = {}

            act_a = jobEle.xpath('./td[2]//a[@class="mr-1"]')[0]
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

            act_prop["inputtime"] = self.today
            ret.append(act_prop)
        return ret

    def run_task(self):
        try:
            response = self._fetch()              
            with open(f"../data/html/quicker_actions_{self.yesterday}.html", 'w', encoding='utf-8') as f:
                f.write(response)
                                           
            ret = self._extract(response)
            with open(f"../data/csv/quicker_actions_{self.yesterday}.csv", "w", encoding="utf-8", newline="") as f:
                csv_writer = csv.DictWriter(f, fieldnames=self.csv_header)
                csv_writer.writeheader()
                csv_writer.writerows(ret)
            logger.info(f"{self.yesterday} 更新了 {len(ret)} 个动作")
        except Exception as e:
            logger.exception(e)

if __name__ == "__main__":
    QuickerAction().run_task()

