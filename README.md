# QuickerActionDailyUp
记录 Quicker 动作库中首次观察到的新增或修订 https://getquicker.net/Share/Recent

每天北京时间 08:00 / 21:00 通过 GitHub Actions 抓取（cron 为 UTC 00:00 / 13:00，实际执行可能延迟）。代码使用 `Asia/Shanghai`，不依赖运行机器的默认时区。

## 运行与回归测试

```sh
pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/crawler.py
```
