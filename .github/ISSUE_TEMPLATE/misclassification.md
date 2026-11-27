---
name: 誤分類の報告
about: racepath の競合の検出漏れ・誤検出・種別やプローブの誤りを記録する
title: "[誤分類] "
labels: misclassification
---

## 対象の競合ID・種別
<!-- 例: rp003 / hold (CKBI vs SAT)。検出漏れなら「なし」と書き、期待する組を下に書く -->

- ID:
- 種別(racepath の出力):
- 入力 a / b:

## 期待した種別やプローブ
<!-- 例: wl_pre であるべき。data_node は XLAT.DL ではなく XLAT.X を測りたい -->

- 期待する種別:
- 期待するプローブ:
- 理由:

## 再現用の最小ネットリスト
<!-- 社外秘の名前は置き換えてよい。tests/cases/ にそのまま追加できる形が望ましい -->

```spice

```

実行したコマンド:

```bash
racepath analyze case.cdl -o case_racepath
```

## racepath のバージョン
<!-- racepath --version の出力 -->

