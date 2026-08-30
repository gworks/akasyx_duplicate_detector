# akasyx_duplicate_detector

重複判定アーカイバ。指定した**保存用フォルダ**に、内容が重複しないファイル集合を構築する。

投入元（追加したいファイル / フォルダ）を [akasyx_crawler](../akasyx_crawler) で探索して
SHA-256 を取得し、保存用フォルダの DB に無いハッシュだけを保存用フォルダへ **移動** する。
**ハッシュが重複したものは移動せず、投入元にそのまま残る。**

設計書: [`__documents/設計書_v010_重複判定アーカイバ.md`](__documents/設計書_v010_重複判定アーカイバ.md)

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

# 整合性チェック（人が保存フォルダを直接いじった場合の検出）
uv run main.py verify /path/to/archive
uv run main.py verify /path/to/archive --flag-quarantine unregistered archive_duplicate

# 据え置いた重複の後始末（--yes が無ければ一覧表示のみ）
uv run main.py delete-duplicates /path/to/archive
uv run main.py delete-duplicates /path/to/archive --yes

# 状態と履歴
uv run main.py report /path/to/archive
uv run main.py --help
```

### 判定のルール（add）

上から順に評価し、最初に該当したもので確定する。

| 条件 | 判定 | 実体 |
|---|---|---|
| 0 バイト（`--min-size` 未満） | `skipped_empty` | 投入元に残す |
| ハッシュが取れなかった | `skipped_nohash` | 投入元に残す |
| 同一ハッシュが保存 DB にある | `duplicate` | **投入元に残す** |
| 上記以外 | `moved` | 保存フォルダへ移動 |

### 保存フォルダの構造

```
<保存用フォルダ>/
├── .akasyx/
│   ├── archive.db      ← 正本（何が保存済みかの真実）
│   └── tmp/            ← 別ファイルシステム間コピーの一時領域
├── <投入元フォルダ名>/  ← --dest-subdir で変更、'' で直下に展開
│   └── <投入元の相対パス>
```

`archive.db` は保存フォルダの中にあるため、フォルダごと別ディスクへ移動・バックアップしても
正本が付いてくる。逆に、**保存フォルダを丸ごとコピーすると DB も複製される**ので、
コピー先で `add` を実行すると正本が分岐する点に注意。

出力先（既定・リポジトリルート直下）:

- 作業用 DB: `dist/db/file_inventory.db`（crawler の出力。消しても正本には影響しない）
- ログ・CSV レポート: `dist/log/`

### 安全側の設計

- 元ファイルを削除するのは、**移動先の実体とハッシュを検証した後**だけ
- 別ファイルシステムをまたぐ移動は `shutil.move` を使わず、
  copy → ハッシュ再計算 → 一致確認 → 元を削除、の順で行う
- 移動は3フェーズ（予約を commit → 移動 → 確定）。途中で落ちても次回起動時に復旧する
- `delete-duplicates` は削除前に2点検証（投入元の現ハッシュ / 保存フォルダ側の実体とハッシュ）
- 保存フォルダと投入元が入れ子だと実行を拒否する

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

## リリース運用（レベルB・全自動）

- **バージョンの正本**: リポジトリルートの `version.txt`（CLI の `--version` が読む）。
  `detector/pyproject.toml` の `[project] version` は配布メタデータとして CI が同期する。
  **人が編集するバージョンファイルは無い**（どちらも CI が書き換える）
- **リリースノートの正本**: GitHub Release。CI が直前タグからの差分で自動生成するが、
  自動生成の文面は骨組みにすぎないため、**`__documents/release_note_vXYZ.md` を
  原稿としてリポジトリ内に書き、公開後にそれを元に Release 本文を書き直す**
  （原稿ファイルは CI からは使われない。リポジトリ内の履歴も兼ねる）

### 手順（人がやること）

```bash
git checkout master && git pull origin master
git checkout -b release/0.2.0
git push origin release/0.2.0    # ← これだけ
```

以降 `.github/workflows/release-branch.yml` が自動実行する:

1. `detector/pyproject.toml` の version を更新し bot がコミット＆push
2. そのコミットに `v0.2.0` タグを付与
3. 直前タグからの差分でリリースノートを生成し GitHub Release を作成
4. master へ `--no-ff` マージ
5. release ブランチを削除

push 直後に手元で `git pull` して bot コミットを取り込むこと。

### 補助ワークフロー

| ファイル | 役割 |
|---|---|
| `.github/workflows/release.yml` | 人が手でタグを打った場合の保険（検証2点 + Release が無ければ作成） |
| `.github/workflows/version-guard.yml` | release/hotfix を head とする PR での検証（保険） |
| `.github/workflows/ci.yml` | pytest（push: master / feature / release / hotfix、PR: master） |
| `.github/scripts/` | bump_version.sh / verify_version.sh / release_notes.sh |

### やり直し（タグ・Release を消して切り直す）

```bash
gh release delete vX.Y.Z --yes
git push origin :refs/tags/vX.Y.Z && git tag -d vX.Y.Z
git push origin --delete release/X.Y.Z && git branch -D release/X.Y.Z
```
