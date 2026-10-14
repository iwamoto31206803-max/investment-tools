# Phase 2B Selective Writer MVP — 開発実行結果

Development only / Production Not Yet Approved。実行コード: `7067ed4`。
合成 fixture とモック応答で実際の CLI を通し、
Reader → Planner → Acquisition Engine → Writer → 出力 Reader を接続した。
ライブ Yahoo 通信、正式 R01、Drive、Baseline、Standards、Manifest の変更はない。

## 実行結果

| ケース | 対象 | 置換 | 追加 | stock_daily 行数 | モック呼出 |
| --- | --- | ---: | ---: | --- | ---: |
| OVERLAP | 8725 | 20 | 26 | 370 → 396 | 1 |
| NO_DATA_CHANGE | 8725 | 0 | 0 | 370 → 370 | 0 |

7181 の全履歴と 8725 の対象外履歴が不変。8 シートと元の列スキーマを維持。
OVERLAP では stock_daily 以外の全 ZIP コンポーネント（他の 7 worksheet を含む）
の内容が一致し、出力を既存 Reader で再読込した。重複キーなし、更新値と
取得元・取得日時が正規化入力に一致。NO_DATA_CHANGE の XLSX は入力とバイト同一。

- 入力／実行後入力 SHA-256:
  `89323433456413d13f786332a00d0280c74618c5116f7af1c5df93b3f8ddb887`
- OVERLAP 出力 SHA-256:
  `0537cff3ae2eb799d0d933e82fe021a905c8d382826407375fa5fb41a769c0a6`
- NO_DATA_CHANGE 出力 SHA-256: 入力と同じ。

成果物は `/workspace/scratch/phase2b-selective-writer-final/` 内の
`overlap/updated.xlsx`、`no_data_change/updated.xlsx`、各 writer sidecar／run JSON、
normalized JSONL。XLSX や raw dataset は commit しない。
[小さな結果 JSON](phase2b_demo_results.json) に件数・ハッシュ・対応モードを記録した。

## 対応範囲と実装

対応: 完全な OVERLAP の置換／追加、明示証跡付き NO_DATA_CHANGE。
未対応: BACKFILL、FULL_REFRESH、更新対象 worksheet の formula/table/merge/extension。
取得失敗・部分取得・空バッチ・不一致ハッシュ・重複キー・未知 SOURCE_ID は拒否し、
出力 XLSX を公開しない。既存 output は上書きしない。

価格・取得元・取得日時を反映し、古い調整 OHLC・調整係数・品質フラグは対象行で空にする。
取得元 ID は既存 source_registry から明示指定する。新規の正式取得元は定義しない。
quality_summary 等はそのままで、正式品質の再計算や本番適格性は保証しない。
これを writer evidence と対象行の開発メモに明記する。

stock_daily は逐次 XML 処理し、重複キーは一時 SQLite に置く。変更しない ZIP メンバーは
チャンク単位でコピーする。検証済み一時出力を新規ファイルとして公開し、入力は read-only。
NO_DATA_CHANGE は XML 再生成も ZIP 再圧縮もせずコピーする。
小規模合成実行のプロセス peak RSS は約 23.9 MiB。約 20 MB の accepted R01 は未測定。
既存 Reader の日付索引・shared strings は引き続き使い、全 workbook のセルオブジェクトは作らない。

## 検証と再実行

**138 tests passed**（既存 126 + focused writer 12）、Ruff、compileall、pip check、
`git diff --check` 成功。正常更新、拒否、NO_DATA_CHANGE、未変更履歴／コンポーネント、
CLI 結合、XML 空白・dimension/filter、出力パス保護を確認。

```bash
python scripts/demo_phase2b_selective_writer.py \
  --development-dir /tmp/r01-phase2b-demo-new
python -m pytest -ra
ruff check .
```

生成 XLSX は開発用であり、実 R01 の全件性能や Yahoo ライブ取得成功の証拠ではない。
formal quality 更新、writer への JSONL 再読込、数式計算、production cutover は範囲外。
