# 基金與 ETF 績效比較

比較野村投信基金、台股 ETF 與個股的含息總報酬，區間包含近一個月到近十年、今年以來、成立至今，以及自訂區間。

報告網址：https://thebigcoco.github.io/portfolio/

## 自動更新

GitHub Actions 每天台灣時間 18:00 自動產生報告並發布（`.github/workflows/update-report.yml`），也可以在 Actions 頁面手動執行「每日更新報告」。

## 手動更新報告

```bash
python build_report.py
```

- 標的清單在 `funds.json`。野村基金填 `fundId`（基金頁網址的 `gradeId`）；股票 / ETF 填 Yahoo Finance 代號，上市加 `.TW`、上櫃加 `.TWO`。
- 結束日為報告更新日的前一天（基金淨值當天 16:00 後才公布）。
- 區間算法與野村投信官網一致：以起始日當天淨值為基準，非交易日取前一交易日，月底對月底。

## 資料來源

- 野村投信官網：基金每日淨值
- Yahoo Finance：股票 / ETF 含息調整價
- 臺灣證券交易所：除息參考價（校正 Yahoo 漏算或錯算的配息）、缺漏日收盤價

過去績效不代表未來表現，本報告僅供參考，不構成投資建議。
