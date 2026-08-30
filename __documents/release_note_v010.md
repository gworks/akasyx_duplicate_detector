# リリースノート v0.1.0

**リリース日:** 2026-08-30
**タグ:** v0.1.0

---

## 概要

初版リリース。指定した**保存用フォルダ**に、内容が重複しないファイル集合を構築する
重複判定アーカイバを実装しました。投入元を akasyx_crawler で探索して SHA-256 を取得し、
保存用フォルダの DB に無いハッシュだけを保存用フォルダへ**移動**します。
**ハッシュが重複したものは移動せず、投入元にそのまま残ります。**

設計書: `__documents/設計書_v010_重複判定アーカイバ.md`

---

## 変更内容

### 新機能

#### 取り込み（add）— 判定表（ingest.py）

上から順に評価し、最初に該当したもので確定します。

| 条件 | 判定 | 実体 |
|------|------|------|
| 0 バイト（`--min-size` 未満） | `skipped_empty` | 投入元に残す |
| ハッシュが取れなかった | `skipped_nohash` | 投入元に残す |
| 同一 `(filehash, hash_algo)` が保存 DB にある | `duplicate` | **投入元に残す** |
| 上記以外 | `moved` | 保存フォルダへ移動 |

- **0 バイトは保存しません。** 空ファイルはすべて同一ハッシュ（`e3b0c442…`）になるため、
  放置すると「最初の1件だけが保存され、以降の全空ファイルが重複扱い」という直感に
  反する結果になります。`--min-size 0` を指定すれば従来どおり内容一致として扱えます
- **ハッシュが取得できなかったファイルは動かしません。** crawler は読み取りエラー時に
  `filehash = NULL` で登録を続行するため、これを「新規」と誤判定すると重複が
  保存フォルダに紛れ込み、以後の判定が信用できなくなります
- 同一の投入バッチ内に同じ内容が2つあれば、1件目が `moved`、2件目が `duplicate` になります

#### 保存フォルダの構造とレイアウト（mover.py）

```
<保存用フォルダ>/
├── .akasyx/
│   ├── archive.db      ← 正本（何が保存済みかの真実）
│   └── tmp/            ← 別ファイルシステム間コピーの一時領域（*.part）
├── <投入元フォルダ名>/  ← --dest-subdir で変更、'' で保存フォルダ直下に展開
│   └── <投入元の相対パス>
```

- 投入元の相対パス構造をそのまま再現します
- 同名・別内容の衝突は `名前 (2).ext` → `名前 (3).ext` … で回避します。判定は
  「実体の有無」「DB の占有」の両方で行い、大文字小文字を区別しないファイルシステム
  （macOS APFS 既定 / Windows）を考慮して casefold して突き合わせます
- `archive.db` を保存フォルダの中に置くことで、フォルダごと別ディスクへ移動・
  バックアップしても正本が付いてきます

#### 安全な移動 — 3フェーズ + クラッシュ復旧（mover.py）

「移動 → DB 登録」だと実体だけが残り、「DB 登録 → 移動」だと幽霊レコードが残るため、
**pending を先に commit する3フェーズ**としました。

```
Phase 1（予約）  status='pending' で INSERT → commit（実体を触る前）
Phase 2（移動）  同一FS: os.replace（アトミック）
                 別FS  : copy → ハッシュ再計算で検証 → ★検証通過後にのみ元を削除
Phase 3（確定）  移動先を stat してサイズ照合 → status='stored' → commit
```

- **別ファイルシステム間で `shutil.move` は使いません。** 内部が copy+delete で
  コピー内容を検証しないためです。ネットワークドライブや外付けディスクでは
  静かに壊れたコピーが起こり得ます
- 起動時に `pending` 行を4パターン（移動完了 / 内容不一致 / 移動前 / 双方消失）で
  判定して安全な状態へ寄せます。`.akasyx/tmp/*.part` は無条件で削除します
- 復旧はすべてのサブコマンドの開始時に走ります

#### 整合性チェック（verify.py）

保存フォルダを crawler で再スキャンし（`--exclude '.akasyx/'`）、DB と突き合わせます。

| 検出 | 内容 | DB の更新 |
|------|------|----------|
| `missing` | DB にあるが実体が無い | `status='missing'`（**行は消さない**） |
| `unregistered` | 実体はあるが DB に無い | `status='unregistered'` で事後登録 |
| `archive_duplicate` | 保存フォルダ**内**に同一内容が2つ以上 | 2件目以降を `unregistered` として登録 |
| `hash_mismatch` | DB と実体のハッシュが食い違う | **DB のハッシュは書き換えない**（人の判断に委ねる） |
| `relocated` | ハッシュ一致で場所だけ変わっていた | `stored_path_rel` を更新 |

**先に全件のハッシュ表を作ってから判定します。** パス単位で逐次判定すると、人が動かした
ファイルを `missing` + `unregistered` の2件に割ってしまうためです。また `missing` 行と
同一内容の実体が戻ってきた場合は `relocated` として復活させます。

#### 処置予定フラグ（disposition）

```bash
uv run main.py verify <保存用フォルダ> --flag-quarantine unregistered archive_duplicate
```

検出結果に `disposition='quarantine'` を立てます。**このコマンドはファイルを一切
動かしません。** 隔離フォルダへの実移動は将来対応で、その際は `disposition='quarantine'`
の行を拾うだけで済みます。指定できる種別は
`missing` / `unregistered` / `archive_duplicate` / `hash_mismatch` の4つです。

#### 据え置いた重複の後始末（dedupe.py）

`add` で投入元に残した重複を、検証付きで削除します。削除前に**2点検証**を必須とし、
1つでも欠ければスキップして理由を記録します。

1. 投入元のファイルが存在し、**現在の**ハッシュが記録と一致する
2. 対応する保存フォルダ側の行が `stored` で、実体があり、ハッシュが一致する

検証は毎回ハッシュを再計算します。時間はかかりますが、削除は取り返しがつかないため
省略しません。**`--yes` を付けなければ一覧を表示するだけで何も削除しません。**
`--trash-dir DIR` で削除ではなく退避もできます。

#### 永続化と実行管理

- SQLite `<保存用フォルダ>/.akasyx/archive.db` に3テーブル
  （`ar_ingests` / `ar_archive_files` / `ar_ingest_items`）
- **重複判定の要は部分 UNIQUE 索引** `(filehash, hash_algo) WHERE status IN ('pending','stored')`。
  実装にバグがあっても DB が二重登録を弾きます。`missing` / `unregistered` を索引から
  外すことで、消えたファイルと同内容のものを後から再登録できます
- PRAGMA: `journal_mode=WAL` / **`synchronous=FULL`**（クラッシュ時に pending 行が
  失われると復旧できないため速度より耐久性を優先）/ `foreign_keys=ON`
- 保存フォルダ単位のロックファイル（`.akasyx/lock`）で多重起動を拒否。
  死んだプロセスのロックは PID を見て引き継ぎます
- `--dry-run`、CSV レポート（`dist/log/*_result_*.csv`・6列）、実行サマリ出力
- `report` サブコマンドで保存フォルダの状態・処置予定フラグ・未処置の重複・実行履歴を表示

---

## 影響範囲

初版のため既存機能への影響はありません。

| 項目 | 内容 |
|------|------|
| 必須依存 | `sqlalchemy` のみ（ハッシュは標準 `hashlib`、ファイル操作は標準 `os` / `shutil`） |
| 外部プロセス | **akasyx_crawler v0.1.0 以上**（既定で兄弟ディレクトリ `../akasyx_crawler` を探す。`--crawler-repo` で変更可） |
| 実行方法 | `cd detector && uv run main.py <サブコマンド> …`（非パッケージモード） |
| DB | `<保存用フォルダ>/.akasyx/archive.db`（新規作成）/ 作業用に `dist/db/file_inventory.db` |

### akasyx_crawler との連携

crawler は `package = false` のフラット構成（`import collector`）でライブラリ import に
向かないため、**CLI としてサブプロセス起動し、出力された `file_inventory.db` を
読み取り専用で参照**します。crawler 側の変更は不要です。

- crawler の既定 `--gitignore-mode nested` は使わず **`off` を渡します**。
  投入元の `.gitignore` で取り込み対象が勝手に減らないようにするためです
- `--no-hash` / `--hash-max-size` は**渡しません**（ハッシュが無いと判定できないため）
- **crawler のスキャンが `completed` 以外なら取り込みを中止します**（終了コード 3）。
  不完全な走査で移動を始めると、投入元の一部だけが移った状態になるためです
- 投入元が**単一ファイル**の場合は crawler を使わず detector が直接 stat + SHA-256 を
  計算します（1ファイルのためにサブプロセスを起こす意味がなく、除外指定の
  取りこぼしリスクも避けられるため）
- crawler 起動時に `VIRTUAL_ENV` を環境から外します（detector の venv を持ち込むと
  uv が「プロジェクトの環境と違う」と警告し、意図しない環境で走りかねないため）

### 終了コード

| 値 | 意味 |
|----|------|
| 0 | 正常終了 |
| 1 | 致命的エラーで停止した |
| 2 | 完走したが失敗が 1 件以上あった（人の確認が要る） |
| 3 | 事前チェックで拒否した（入れ子・crawler 不在・crawler のスキャン不完全・多重起動 等） |

---

## テスト

pytest 85件 全パス（support 20 / mover 18 / main 16 / ingest 11 / verify 11 / dedupe 9）。

crawler は別リポジトリで CI には存在しないため、crawler への依存は
`crawler_client.py` に閉じ込め、判定ロジックは `fs_files` 相当のテーブルを持つ
一時 SQLite を作って検証しています。**テストは crawler を一切必要としません。**

重点的に検証した箇所:

- 別ファイルシステム経路で、**検証が通らなければ元ファイルが必ず残る**こと
- クラッシュ復旧の4パターンがそれぞれ正しい状態へ収束すること
- 部分 UNIQUE 索引が二重登録を弾き、`missing` 行とは衝突しないこと
- verify で、移動されたファイルが `missing` + `unregistered` に割れないこと
- `delete-duplicates` の2点検証のどちらかを崩すと削除されないこと、
  `--yes` 無しで削除されないこと
- crawler が無い環境で `add` / `verify` が実行前に断ること（終了コード 3）、
  および単一ファイル投入は crawler 不在でも通ること

### 実 crawler での動作確認

- **add**: 5ファイル投入 → 移動3 / 重複1（投入元に残存）/ 空ファイル1（投入元に残存）
- **verify**: 保存フォルダから1件削除・1件を別ディレクトリへ移動・1件を外部から追加した
  状態で、`missing` 1 / `relocated` 1 / `unregistered` 1 を正しく判別
- **`--flag-quarantine`**: disposition が2件に立ち、ファイルは1つも動いていないことを確認
- **delete-duplicates**: `--yes` 無しでは一覧表示のみ、`--yes` で2点検証を通して削除

---

## 対応内容一覧

| 対象ファイル | 変更内容 |
|------------|---------|
| `detector/main.py` | サブコマンド振り分け・事前チェック・ロック・実行記録・終了コード |
| `detector/config.py` | CLI 引数解析（4サブコマンド）・DetectorConfig・ログ初期化 |
| `detector/models.py` / `database.py` | 3テーブルのスキーマ・部分 UNIQUE 索引・PRAGMA・ロック |
| `detector/crawler_client.py` | crawler のサブプロセス起動と `fs_files` の読み取り・単一ファイル経路 |
| `detector/ingest.py` | add の判定表・判定ループ・保存先第1階層の決定 |
| `detector/mover.py` | 衝突回避・3フェーズ移動・安全な移動・クラッシュ復旧・空ディレクトリ掃除 |
| `detector/verify.py` | 整合性チェック（3パス）・処置予定フラグ・状態サマリ |
| `detector/dedupe.py` | 据え置いた重複の2点検証・削除 / 退避 |
| `detector/report.py` | 保存フォルダの状態・処置予定フラグ・未処置重複・実行履歴の表示 |
| `detector/errors.py` | DetectorError / PreflightError |
| `detector/utl/hashing.py` | SHA-256 ストリーミング計算（移動後の検証用） |
| `detector/utl/helpers.py` / `result_csv.py` | パス正規化・入れ子判定・CSV レポート |
| `detector/tests/` | テスト85件 + 疑似 crawler DB のフィクスチャ |
| `.github/workflows/` / `.github/scripts/` | リリース自動化（レベルB。akasyx_crawler から移植） |
| `__documents/` | 設計書 v0.1.0・本リリースノート |
| `version.txt` / `README.md` | バージョンの正本・使い方 |

---

## 申し送り事項

- **バージョンの正本はリポジトリルートの `version.txt`。** `detector/pyproject.toml` の
  `[project] version` は配布メタデータで、CI（`.github/scripts/bump_version.sh`）が
  同期します。**人が編集するバージョンファイルはありません**
- リリースは `release/x.y.z` ブランチを切って push するだけで、bump → タグ → Release
  作成 → master マージ → ブランチ削除まで CI が行います（レベルB）
- **保存フォルダを丸ごとコピーすると `archive.db` も複製されます。** コピー先で `add` を
  実行すると正本が2つに分岐する点に注意してください
- **ハードリンク**: 同一 inode の2ファイルは片方が移動され、もう片方は重複として
  投入元に残ります。リンク関係は保存フォルダに引き継がれません
- **crawler が SharePoint（quickXorHash）に対応すると `hash_algo` が混在します。**
  照合は同一 algo 同士のみで、リモートの実体は移動できないため、v0.1.0 の取り込み対象は
  `source_type='local'` に限ります
- 隔離フォルダへの実移動（`disposition='quarantine'` の消費）は v0.1.0 では未実装です。
  フラグを立てるところまでは動くので、消費側を足すだけで済みます
- **テストで crawler をモックするときは、`run_crawler`（実行）だけでなく
  `resolve_crawler_repo`（存在確認）も差し替えること。** 前者だけだと、兄弟ディレクトリに
  実物の crawler がある開発機では通り、無い CI では落ちます。テストの既定
  `crawler_repo` も存在しないパスにしてあります（初回 CI で26件失敗して判明）
