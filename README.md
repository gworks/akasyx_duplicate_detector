# akasyx_duplicate_detector

重複判定アーカイバ。指定した**保存用フォルダ**に、内容が重複しないファイル集合を構築する。

投入元（追加したいファイル / フォルダ）を [akasyx_crawler](../akasyx_crawler) で探索して
SHA-256 を取得し、保存用フォルダの DB に無いハッシュだけを保存用フォルダへ **移動** する。
**ハッシュが重複したものは移動せず、投入元にそのまま残る。**

設計書: [`__documents/設計書_v020_重複判定アーカイバ.md`](__documents/設計書_v020_重複判定アーカイバ.md)（v0.1.0 版は `設計書_v010_…` に凍結）

## 前提

- Python 3.12+ / [uv](https://docs.astral.sh/uv/)
- **akasyx_crawler**（既定では兄弟ディレクトリ `../akasyx_crawler` を探す。`--crawler-repo` で変更可）

## セットアップ

```bash
cd detector
uv sync
```

## 使い方

```bash
cd detector

# 取り込み（新規のみ移動し、重複は投入元に残す）
uv run main.py add /path/to/archive /path/to/inbox
uv run main.py add /path/to/archive /path/to/inbox --dry-run      # 判定だけ見る
uv run main.py add /path/to/archive /path/to/inbox --folder-limit 300   # 1 フォルダの上限を変える

# 整合性チェック（人が保存フォルダを直接いじった場合の検出）
uv run main.py verify /path/to/archive
uv run main.py verify /path/to/archive --flag-quarantine unregistered archive_duplicate

# 据え置いた重複の後始末（--yes が無ければ一覧表示のみ）
uv run main.py delete-duplicates /path/to/archive
uv run main.py delete-duplicates /path/to/archive --yes

# 状態と履歴
uv run main.py report /path/to/archive
uv run main.py archives                       # 正本 DB に登録されている保存フォルダの一覧
uv run main.py --help
```

### 判定のルール（add）

上から順に評価し、最初に該当したもので確定する。

| 条件 | 判定 | 実体 |
|---|---|---|
| `.DS_Store` / `Thumbs.db` / `desktop.ini` | 走査対象外（レポートにも出ない） | 投入元に残す（`--prune-empty-dirs` で消える） |
| 0 バイト（`--min-size` 未満） | `skipped_empty` | 投入元に残す |
| ハッシュが取れなかった | `skipped_nohash` | 投入元に残す |
| detector 自身のデータ（データフォルダ・正本 DB・作業用 DB・ログ） | `skipped_own_data` | 投入元に残す（ホームを投入元にした場合など。サマリに件数が出る） |
| 同一ハッシュが保存 DB にある | `duplicate` | **投入元に残す** |
| 上記以外 | `moved` | 保存フォルダへ移動 |

### 保存フォルダの構造

保存先は **作成日の年月フォルダの下に、投入元の階層をそのまま再現**する。ファイルが直接入る
フォルダ（葉）の件数が `--folder-limit`（既定 500）に達したら `001/`、それも埋まれば `002/` …
と枝を増やす（`999/` の次は `1000/`）。Finder や同期ツールが 1 フォルダを開くときの重さを
抑えるための構造で、何万件あっても 1 フォルダに 500 件を超えて平置きされない。

```
<保存用フォルダ>/
├── .akasyx/
│   ├── archive.id          ← この保存フォルダの識別子（uid 1 行）。正本 DB と対応づける
│   └── tmp/                ← 別ファイルシステム間コピーの一時領域
├── 2026-08/                ← 作成日（ローカル時刻）の YYYY-MM
│   └── <投入元フォルダ名>/     ← --dest-subdir で変更、'' で付けない
│       └── 旅行/
│           ├── IMG_0001.jpg    ← 葉フォルダ直下に 500 件まで
│           └── 001/            ← 501 件目以降
├── 2026-09/
│   └── <投入元フォルダ名>/
│       └── 旅行/IMG_0900.jpg   ← 同じ「旅行」でも作成月が違えばこちら
└── unknown-date/           ← 作成日も更新日時も取れなかったもの
```

- **作成日**は macOS の `st_birthtime`（crawler の `created_at`）。取れなければ更新日時に落ちる
- **同じ投入元フォルダの中身は作成月ごとに分かれる**。「投入元のまとまりを 1 か所に残す」より
  「月ごとに見られる」ことを優先した構造
- 件数は**実体の数と DB の予約数の大きい方**で見るので、人が手でファイルを置いても、
  前回クラッシュして pending 予約だけ残っていても、枝分かれの判断は崩れない
- 同名衝突は同じフォルダ内で `名前 (2).ext` になる（別の枝には逃がさない）
- 投入元が単一ファイルのときは投入元フォルダ名を付けず `YYYY-MM/<ファイル名>`

### 正本 DB の場所（v0.2.0 で変更）

正本の SQLite は**保存フォルダの外**（中に置くと起動時に断る）、既定で `<データフォルダ>/archive.db` に 1 つだけ置く
（`--archive-db` で変更可。UI では「詳細設定」）。データフォルダは開発時（`uv run`）は
`<リポジトリルート>/dist/`、配布版（.app）は `~/Library/Application Support/akasyx-duplicate-detector/`
（Windows は `%LOCALAPPDATA%\akasyx-duplicate-detector\`）。今の場所は `main.py --help` の末尾と、
UI の「詳細設定 → データの保存場所」（「Finder で表示」で開ける）に出る。
配布版のアプリを削除してもデータフォルダは残る。完全に消すときはこのフォルダも削除する。保存フォルダはこの DB の `ar_archives` に
1 行ずつ登録され、ファイル行は保存フォルダ ID を持つ。**重複判定は保存フォルダ単位**で、
別の保存フォルダにある同じ内容は重複扱いにならない。

- **起動時に正本 DB が無ければ（親フォルダごと）自動で作る**。ログに「正本 DB がありません。新規作成します」と出る。
  初回起動・`dist/` を消した後・別マシンでの初回はこれで空の DB から始まる
- 保存フォルダ側には識別子ファイル `.akasyx/archive.id` だけを置く。フォルダを移動・改名しても
  この uid で同じ登録に繋がり、記録されている絶対パスは自動で更新される
- ネットワーク上の保存フォルダ（SMB 等）でも、SQLite はローカルにしか置かれないので安全に使える
- **v0.1.x の保存フォルダ（`.akasyx/archive.db` が中にある）を開くと、自動で正本 DB へ取り込み**、
  旧 DB は `archive.db.migrated-<日時>` に改名して残す
- **正本 DB に登録の無い保存フォルダ（`archive.id` はあるが中身が別の正本 DB で管理されていた）は
  どのコマンドでも開けない**。開発版（`dist/`）と配布版の切り替えや別の Mac で起きる。重複を作らないため、
  使っていた正本 DB を `--archive-db` で指定する（中身が空なら新規登録して続ける）
- 保存フォルダを丸ごとコピーすると `archive.id` も複製される。**元の保存フォルダが残っている間は
  コピー先を開くと断る**（移動とみなすと 2 つが 1 つの登録を取り合う）。
  コピー先を別の保存フォルダとして使うなら、先に `.akasyx/archive.id` を消す
- `dist/` は git 管理外。**正本 DB のバックアップは自分で取る**（保存フォルダのバックアップだけでは
  何が保存済みかの記録は復元できない）

出力先（既定・データフォルダの下）:

- 作業用 DB: `db/file_inventory.db`（crawler の出力。消しても正本には影響しない）
- ログ・CSV レポート: `log/`

### 安全側の設計

- 元ファイルを削除するのは、**移動先の実体とハッシュを検証した後**だけ
- 別ファイルシステムをまたぐ移動は `shutil.move` を使わず、
  copy → ハッシュ再計算 → 一致確認 → 元を削除、の順で行う
- 移動は3フェーズ（予約を commit → 移動 → 確定）。途中で落ちても次回起動時に復旧する
- `delete-duplicates` は削除前に2点検証（投入元の現ハッシュ / 保存フォルダ側の実体とハッシュ）
- 保存フォルダと投入元が入れ子だと実行を拒否する
- `--prune-empty-dirs` は空になった投入元ディレクトリを消す。`.DS_Store` など OS のメタデータしか
  残っていないフォルダも空とみなす（既定はオフ。UI の取り込みタブでは既定オン）

### 終了コード

| 値 | 意味 |
|---|---|
| 0 | 正常終了 |
| 1 | 致命的エラーで停止した |
| 2 | 完走したが失敗が 1 件以上あった（人の確認が要る） |
| 3 | 事前チェックで拒否した（入れ子・crawler 不在・crawler のスキャン不完全 等） |

## テスト

```bash
cd detector
uv run pytest
```

crawler は別リポジトリで CI には無いため、`fs_files` 相当の一時 SQLite を作って
判定ロジックを回す。crawler の起動そのものは `crawler_client.py` に閉じ込めてある。

## UI（Electron）

パス入力とオプション指定を画面から行うためのフロントエンドです。`electron-ui/` に置き、
**detector 側には一切手を入れていません**。UI はロジックを持たず、フォームの値から
`uv run main.py ...` を組み立てて子プロセス起動し、標準出力・終了コードを表示するだけです
（設計書 §16 の「境界を CLI に固定する」方針）。UI が壊れても CLI は無傷で、
CLI で直せることは UI でも同じように直ります。

### 起動

```bash
cd electron-ui
npm install       # 初回のみ。Electron のバイナリ（約110MB）を取得する
npm run dev
```

`npm install` が「Downloading Electron binary...」のまま進まないときは回線が細いだけなので、
そのまま待つか `node node_modules/electron/install.js` で取得だけやり直す。

Python 側の前提は CLI と同じです（`detector/` で `uv sync` 済み、`add`/`verify` で
フォルダを扱うなら `../akasyx_crawler` も用意されていること）。

### 画面でできること

| タブ | 対応コマンド |
|---|---|
| 取り込み | `add`（dry-run、`--folder-limit`、`--min-size` などを含む） |
| 整合性チェック | `verify`（`--flag-quarantine` の4種別をチェックボックスで指定） |
| 重複の後始末 | `delete-duplicates`（`--trash-dir` / `--yes`） |
| 状態・履歴 | `report` |

入力を楽にするための仕掛け:

- **ドラッグ＆ドロップ** — パス欄にフォルダやファイルを落とすとパスが入ります
- **ターミナルからの貼り付けもそのまま通る** — `'…'` / `"…"` で囲まれたパス、`\ ` でエスケープされた
  パス、`~/` 始まりのパスは、コマンドを組み立てるときに正規化します（正規化後の値がプレビューに出ます）
- **保存用フォルダの履歴** — 過去に使ったものを入力欄の候補から選べます
- **前回の入力を復元** — 次回起動時に同じ値が入っています
  （ただし `--yes`（実削除）は毎回オフに戻ります）
- **実行されるコマンドを常時表示** — 「コピー」でターミナルにそのまま貼れる形で取れます
- **進捗の集計表示** — `add` の1ファイル1行の出力はログに流さず、件数と判定内訳に集計します
- **CSV / ログへの導線** — 実行後に出力された CSV を Finder で開けます

### 多言語対応

- 画面（メニューと説明）は **ja / en / fr / de / it / et** の 6 言語。右上のセレクトで切り替え、選んだ言語は次回も使う。
  初回は OS の言語（対応外なら英語）
- 文言は `electron-ui/locales/<lang>.json`。main プロセスが読んで renderer に渡す（renderer の CSP が `fetch` を許さないため）。
  辞書に無いキーは英語の値で埋める。HTML を含む値はキー名を `*_html` にする（`data-i18n-html` で innerHTML に入る）
- 文言を足したら 6 言語すべてに同じキーを入れる。`cd electron-ui && npm test` が、JSON として読めること・キーと
  `{差し込み名}` の一致・`index.html` と入力エラーが参照するキーの存在を検査する
- detector 本体のログ・コンソール出力は**英語のみ**。UI は `Progress: <n> files (<result>)` と `CSV report: <path>` の
  2 種類の行を読む（変えるときは `electron-ui/main.js` の正規表現も直す）

### 安全側の作り

- 引数の組み立ては `electron-ui/commands.js` の 1 箇所だけで行い、画面に出す
  コマンドプレビューと実際に実行するコマンドが食い違わないようにしています
- `delete-duplicates --yes`（実削除）は、メインプロセス側で必ず確認ダイアログを出します
  （画面側の実装に依存しません）
- 「中断」は子プロセスのプロセスグループへ `SIGINT` を送ります。detector は
  `KeyboardInterrupt` を受けて `status='interrupted'` で後片付けするので、
  途中で止めても DB と実体の整合は保たれます
- レンダラは `contextIsolation` / `sandbox` 有効、Node 統合なし、CSP で外部読み込み禁止

### 配布について

配布物（署名・公証済みの `.app` と zip）は Apple Silicon の Mac で次の 1 本で作ります:

```bash
# 公証の認証情報は名前だけを環境変数で渡す（どちらか）。無ければ公証せず zip 名に -UNNOTARIZED が付く
#   AKASYX_NOTARY_PROFILE=<notarytool store-credentials のプロファイル名>
#   APPLE_API_KEY=<.p8 のパス> APPLE_API_KEY_ID=<Key ID> APPLE_API_ISSUER=<Issuer ID>
packaging/build_mac.sh
# → build/release/（akasyx Duplicate Detector.app・README.txt・THIRD_PARTY_LICENSES.txt）
#   build/akasyx-duplicate-detector-<版>-mac-arm64.zip
```

- 流れ: ① `packaging/build_python.sh` で detector と crawler を PyInstaller の onedir に
  （利用者の Mac に uv・Python が無くても動かすため）② `packaging/build_licenses.sh` でライセンス表記
  ③ electron-builder で `.app` にして Developer ID で署名（hardened runtime）④ 公証 → staple → `spctl` で検証
- 同梱物の欠け・版の不一致・署名の不正・GPL 系ライブラリの混入があれば途中で失敗する
- 利用者向けの説明（データの場所・完全な削除の手順）は `packaging/README.dist.txt`。配布版は UI の設定も
  データフォルダの `ui/` に置くので、完全に消すときはデータフォルダ 1 つを消せば済む

同梱する実行形式だけを作る場合:

```bash
packaging/build_python.sh    # → build/pyi/dist/akasyx-{detector,crawler}/（PyInstaller onedir）
```

- 兄弟リポジトリ `../akasyx_crawler` が要る。crawler の GPL 系依存（mutagen・pillow-heif）は同梱しない
  （音声・HEIC のメタデータが取れないだけで走査は続く）。混入していればスクリプトが失敗する
- 実行形式は起動しただけで配布版の扱いになり、データフォルダは Application Support 側を使う
- 開発中の UI から実行形式を試す: `AKASYX_BIN_DIR=../build/pyi/dist npm run dev`（`electron-ui/` で）。
  実データを汚したくなければ `AKASYX_DETECTOR_HOME=<一時フォルダ>` も付ける
- 配布版の UI は `Contents/Resources/bin/akasyx-detector/akasyx-detector` を起動し、
  detector には `AKASYX_SIBLINGS_BIN` で同梱の crawler の場所を渡す

`electron-ui/package.json` に version を持たせていないのは、
バージョンの正本を `version.txt` 1 本に保つためです（開発時の UI はこれを読んで表示します）。
配布版の UI は `app.getVersion()` を表示するので、`build_mac.sh` が `version.txt` の値を焼き込みます（`-c.extraMetadata.version`）。

## リリース運用（レベルB・全自動）

- **バージョンの正本**: リポジトリルートの `version.txt`（CLI の `--version` が読む）。
  `detector/pyproject.toml` の `[project] version` は配布メタデータとして CI が同期する。
  **人が編集するバージョンファイルは無い**（どちらも CI が書き換える）
- **リリースノートの正本**: GitHub Release。CI が直前タグからの差分で自動生成するが、
  自動生成の文面は骨組みにすぎないため、**`__documents/release_note_vXYZ.md` を
  原稿としてリポジトリ内に書き、公開後にそれを元に Release 本文を書き直す**
  （原稿ファイルは CI からは使われない。リポジトリ内の履歴も兼ねる）

### 手順（人がやること）

**develop が本流。** コードの修正もテストも develop で行い、緑になってから release へ展開する。
release ブランチでは何もしない（切って push するだけ）。

```bash
git checkout develop && git pull origin develop   # CI が緑であることを確認
git checkout -b release/0.2.0
git push origin release/0.2.0    # ← これだけ
```

以降 `.github/workflows/release-branch.yml` が自動実行する:

0. **リリースゲート**: pytest を実行する。**緑でなければ以降を一切実行しない**
   （タグもリリースも作られない）
1. `detector/pyproject.toml` の version を更新し bot がコミット＆push
2. そのコミットに `v0.2.0` タグを付与
3. 直前タグからの差分でリリースノートを生成し GitHub Release を作成
4. **master と develop の両方へ `--no-ff` マージ**（bump コミットを本流に戻す）
5. 両方への反映を確認してから release ブランチを削除

push 直後に手元で `git checkout develop && git pull` して bot コミットを取り込むこと。

### 補助ワークフロー

| ファイル | 役割 |
|---|---|
| `.github/workflows/tests.yml` | テスト本体（再利用可能ワークフロー）。CI とリリースゲートの両方から呼ばれる |
| `.github/workflows/ci.yml` | pytest（push: develop / master、PR: develop 宛） |
| `.github/workflows/release.yml` | 人が手でタグを打った場合の保険（ゲート + 検証2点 + Release が無ければ作成） |
| `.github/workflows/version-guard.yml` | release/hotfix を head とする PR での検証（保険） |
| `.github/scripts/` | bump_version.sh / verify_version.sh / release_notes.sh |

**テストが緑でないブランチはリリースできません。** `release-branch.yml` と `release.yml` は
どちらも先頭に `tests.yml` を呼ぶゲートジョブを持ち、リリース処理は `needs: test` で
それに依存しています。テストが落ちればタグも GitHub Release も作られません
（手動タグの場合、タグ自体は既に人が打っているので取り消せませんが、Release の公開は止まります）。

### やり直し（タグ・Release を消して切り直す）

```bash
gh release delete vX.Y.Z --yes
git push origin :refs/tags/vX.Y.Z && git tag -d vX.Y.Z
git push origin --delete release/X.Y.Z && git branch -D release/X.Y.Z
```
