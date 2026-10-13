# R01 Generator Phase 2A 合成データ統合試験レポート

**Development only / Production Not Yet Approved。今回の入力・応答はすべて合成データ。**

`test/phase2a-synthetic-fixture` の `5ba0aec` を基点に、提供された
`scripts/generate_r01_synthetic_fixture.py` を実行した。生成された合成 XLSX を
既存 Reader、Planner、Sanity Gate、Yahoo モック transport、Acquisition Engine、
CLI に結合し、正常系と期待される拒否・失敗系を検証した。

再実行用コードをコミットした `6fa626369135ccb85ffd3b1245afcb0d620d104b` で
独立したスクリプト実行を行い、入力ハッシュ・各ケースの結果・コンポーネントハッシュを
[機械可読結果](phase2a_synthetic_integration_results.json) に記録した。
この後のレポート追加コミットは実行コードを変更しない。

## 生成と Reader 互換性

生成先は開発用 `/workspace/scratch/phase2a-synthetic-integration/`。
合成 XLSX、モック JSONL、詳細 run evidence は checkout 外に置き、正式 R01 を使用しなかった。

- ZIP CRC 検証正常、OOXML パッケージ 12 メンバー。
- 8 シートの名前・ワークシート XML・ヘッダーを生成スクリプトの定義と照合して一致。
- 既存 Reader のシート relationship 解決、metadata、component inventory、日付索引が成功。
- `stock_daily` の `DATE` は既存 Reader がサポート済みの列名。生成値の ISO 日付・inline string を読めた。
- metadata の `FIXTURE_KIND=SYNTHETIC` を確認。Reader はヘッダー行も
  `FIELD: VALUE` として保持する既存動作だが、合成マーカーの読取を妨げなかった。
- **Reader との非互換は検出されなかった。生成スクリプトも Reader も変更していない。**
  正式仕様を緩和する修正は行っていない。

| シート | データ行数（ヘッダーを除く） |
| --- | ---: |
| metadata | 2 |
| source_registry | 1 |
| stock_master | 2 |
| universe | 2 |
| stock_daily | 370 |
| quality_summary | 2 |
| acquisition_log | 2 |
| corporate_actions | 0 |

8725・7181 は各 185 観測。開始 2025-12-01、最終 2026-08-14。
両銘柄の合成履歴は同一の日付列を使う。
入力 XLSX の SHA-256 は全実行前後で
`968ebb7cfb4dbce6e4fc4f4f3737348ee9e9a8c8da2369eeab90366ce33c5d6b`
のまま保持された。

## Planner / Sanity Gate

分析日を 2026-09-24 とし、既存の `config/r01_generator.default.toml` を使った。

| 銘柄 | 通常モード | 通常開始日 | 終了日 | 既存重複数 | 状態 |
| --- | --- | --- | --- | ---: | --- |
| 8725 | OVERLAP | 2026-07-20 | 2026-09-24 | 20 | READY |
| 7181 | OVERLAP | 2026-07-20 | 2026-09-24 | 20 | READY |

各銘柄の履歴で 96 観測を含む 2026-04-03 へ強制的に開始日を広げると、
既存 Planner が実履歴から重複数 96 を導出し、両方を
`ACQUISITION_PLAN_BLOCKED` にした。Engine に渡した結果は `REJECTED`、
バッチ結果は期待どおり `FAIL`、transport 呼出は **0 件**。
同じ期間で caller が重複数 20 と過少申告する試みも、両銘柄で
`PlanValidationError` により拒否された。Override は使用していない。

## モック Yahoo / Acquisition Engine / CLI

HTTP transport はネットワーク実装を持たない `MockYahooTransport`。
正確な URL、銘柄、interval、period1、period2 を照合する。
開始日の東京午前 0 時、終了日の翌日の東京午前 0 時（Yahoo exclusive end）を
UTC epoch に変換した値で要求が一致した。実際の transport は一切構築していない。

通常のモック応答は、各銘柄の既存重複 20 観測と、人工的な新規 weekday bar 26 観測。
週末と 2026-09-21/22/23 の意図的なモック欠落日を追加生成せずに保持した。
この閉場日集合は正式な取引カレンダーではない。

| ケース | 7181 | 8725 | バッチ結果 | モック HTTP 呼出数 |
| --- | --- | --- | --- | ---: |
| 正常応答 | SUCCESS / 46 行 | SUCCESS / 46 行 | PASS | 2 |
| 7181 の初回 503 → 成功 | SUCCESS / 46 行、retry=1 | SUCCESS / 46 行 | PASS | 3 |
| 7181 の既存重複を 1 行欠落 | PARTIAL / 45 行 | SUCCESS / 46 行 | PARTIAL | 2 |
| 7181 に重複日付 | DATA_QUALITY_ERROR / 正規化出力 0 行 | SUCCESS / 46 行 | FAIL | 2 |
| 7181 に要求開始日より前の行 | DATA_QUALITY_ERROR / 正規化出力 0 行 | SUCCESS / 46 行 | FAIL | 2 |
| 正常な空応答 | NO_DATA / 0 行 | NO_DATA / 0 行 | NO_DATA | 2 |
| 8725 が恒久 404 | SUCCESS / 46 行 | PROVIDER_ERROR / 0 行 | FAIL | 2 |
| 96 観測の BLOCKED 計画 | REJECTED | REJECTED | FAIL（期待される拒否） | 0 |
| 明示 run evidence の NO_DATA_CHANGE | NO_DATA_CHANGE / 0 行 | NO_DATA_CHANGE / 0 行 | PASS | 0 |

503 再試行は元の要求期間をそのまま保持。待機は注入した sleep に記録し、実待機はしない。
片側が成功しても、失敗したバッチは PASS に昇格しなかった。
不正データの返却件数は証跡に残り、その銘柄の正規化行は公開されなかった。

CLI も実際の入力契約を読み込み、`--acquire` 経路をモック transport と
注入時計で検証した。`--acquire` はこの runner 内でモックへ置換され、ライブ取得を
意味しない。新しい `mock-cli/` に `run.json` と `observations.jsonl` を生成し、
exit code 0、PASS、合計 92 行を確認した。行の Decimal 文字列、銘柄別 46 行、
plan lineage、run/contract/config hash、出力 SHA-256、入力 SHA-256 前後一致を確認した。

全証跡は `synthetic_only`、モック transport 記録または Yahoo meta 内の
`fixtureKind=SYNTHETIC_MOCK_NOT_MARKET_DATA` によって区別する。
JSONL の `SOURCE=Yahoo Chart API` は使用したアダプターの識別であり、実 Yahoo
由来の市場データという意味ではない。固定 retrieval clock は
`2026-09-25T00:00:00+00:00` で、実通信日時を表さない。

## テストと静的検証

Python 3.12.14 / pytest 8.4.2 / Ruff 0.14.0。

- `python -m pytest -ra`: **126 passed**、失敗・skip なし。
  既存 112 件を保持し、合成統合試験 14 ケースを追加。
- 既存の 665 銘柄／96 観測の恒久回帰テストを含め、旧テストは無変更で成功。
- `ruff check .`: 成功。
- `python -m compileall -q scripts r01_generator tests conftest.py`: 成功。
- `git diff --check`: 成功。
- `python -m pip check`: 成功。
- 独立した `scripts/run_phase2a_synthetic_integration.py` の実行: 成功。
- pytest 全体の既存 socket/DNS guard に加え、runner 自身も同じ通信禁止を設定。
  pytest 外での誤接続も拒否することを試験した。**取得ライブ通信 0 件**。
- 基点ブランチとの比較で、`r01_generator/`、既存 config、提供された生成スクリプト、
  既存テスト・network guard が無変更であることを確認。

## 再実行

リポジトリルートから実行する。出力先は新しい開発ディレクトリを選ぶ。

```bash
source /workspace/venvs/investment-tools/bin/activate
python scripts/generate_r01_synthetic_fixture.py \
  --output /tmp/r01-synthetic-check/R01_SYNTHETIC.xlsx
python scripts/run_phase2a_synthetic_integration.py \
  --input /tmp/r01-synthetic-check/R01_SYNTHETIC.xlsx \
  --development-dir /tmp/r01-synthetic-check/evidence-001
python -m pytest -ra
ruff check .
```

runner は既存出力ディレクトリを拒否する。合成マーカーと既知の fixture 構造・銘柄数を
要求するため、実 R01 の検証 runner としては使用しない。詳細の `plans.json`、
各ケース JSON、`mock-cli/run.json`、`mock-cli/observations.jsonl`、
`mock-cli-transport.json`、`summary.json` は指定した開発ディレクトリに保存される。
checked-in 結果 JSON はこの実行の要約であり、全 raw price 応答や XLSX を含まない。

## 制限事項

この結果は **合成データとモック応答での結合検証のみ**。

- 実 R01 の全件性能、容量、メモリ効率、実市場データ品質を測定・保証していない。
  2 銘柄・370 履歴行・92 モック出力行の結果を全件へ外挿しない。
- Yahoo ライブ取得は実行しておらず、可用性、認証不要での接続成功、rate limit、
  最新 schema、調整価格の現実の挙動は未検証。
- 元の生成スクリプトは平日列を使う合成 fixture で、正式な東証取引日列ではない。
  185 観測は実際の営業日数・listing/history 完備性を意味しない。
- 両銘柄の日付履歴は同一で、銘柄ごとの異なる実最終観測日・取引停止はこの fixture の
  対象外。銘柄ごとの異なる窓は既存の 665 銘柄回帰テストで別途検証される。
- パッケージが Reader とヘッダー定義に適合したことを確認しただけで、正式 R01 の
  全業務ルール、計算式、quality_summary の意味、corporate actions の完全性を承認していない。
- 元 generator の XML 内容は決定的だが ZIP メンバーの作成時刻は固定されない。
  再生成時の XLSX バイト SHA-256 は異なり得る。この実行の入力ハッシュと、再照合用の
  各コンポーネント内容ハッシュを区別して記録した。
- **正式 R01、Google Drive、Current Baseline、Standards、Manifest は変更していない。**
  Reader/Planner/Sanity Gate/Engine の正式入力要件も変更していない。

Phase 2B に向けた開発用証跡の結合は確認できたが、writer/rebuild、正式品質更新、
本番承認・切替には進んでいない。PR はレビュー待ちとし、自動 merge は行わない。
