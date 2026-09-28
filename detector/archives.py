# archives.py - 保存フォルダの登録・識別・旧 DB の取り込み（設計書 §4 / §8。v0.2.0）
#
# 正本 DB は 1 つで、保存フォルダは ar_archives の行として区別する。
# 識別は uid（保存フォルダ内の .akasyx/archive.id にも書く）で行い、絶対パスは「現在の場所」。
import logging
import os
import sqlite3
import stat
import uuid
from datetime import datetime

from sqlalchemy import func

from config import OS_JUNK_FILES
from database import META_DIRNAME, archive_id_path, legacy_db_path, meta_dir, tmp_dir
from errors import PreflightError
from models import (
    STATUS_FAILED,
    STATUS_PENDING,
    OWNING_STATUSES,
    PATH_HOLDING_STATUSES,
    STATUS_FORGOTTEN,
    STATUS_UNREGISTERED,
    STATUS_MISSING,
    STATUS_STORED,
    Archive,
    ArchiveFile,
    Base,
    Ingest,
    IngestItem,
    LegacyImport,
    PendingAdoption,
    utcnow,
)

logger = logging.getLogger(__name__)


def read_uid(archive_root: str) -> str | None:
    """`.akasyx/archive.id` の uid。ファイルが無ければ None。

    読めない（権限・I/O エラー）のは「無い」と区別して断る。無いとみなすと別の保存フォルダとして
    登録したり識別子を書き直したりして、既にある内容と同じファイルを取り込んでしまう。
    """
    path = archive_id_path(archive_root)
    try:
        with open(path, encoding="utf-8") as f:
            uid = f.read().strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise PreflightError(f"Cannot read the archive folder ID: {path}: {e}") from e
    return uid or None


def _write_uid(archive_root: str, uid: str) -> None:
    os.makedirs(meta_dir(archive_root), exist_ok=True)
    with open(archive_id_path(archive_root), "w", encoding="utf-8") as f:
        f.write(uid + "\n")


CRAWLER_DEFAULT_IGNORED_DIR = ".git"


def _has_stored_content(root: str) -> bool:
    """保存フォルダに、adopt が登録する実ファイルがあるか（#4 / #5）。

    adopt と同じ条件で数える: `.akasyx/`・`.git/`（crawler の既定除外）・OS のゴミファイル・シンボリックリンク（crawler は既定で辿らない）・
    0 バイトのファイル（min_size 未満。add でも取り込まない）は数えない。条件がずれると、adopt が何も登録しない
    フォルダを「中身あり」として断り続けたり、逆に登録すべきファイルを見逃したりする。
    読めない配下があれば「空」とは言えないので断る（os.walk は既定で黙って飛ばす）。
    """

    def _unreadable(e: OSError):
        raise PreflightError(f"Cannot read part of the archive folder: {e.filename}: {e}") from e

    for dirpath, dirs, names in os.walk(root, onerror=_unreadable):
        # adopt・verify は crawler に --exclude .akasyx/ を渡す（gitignore の書き方なのでどの深さでも効く）。
        # .git/ は crawler の組み込みの既定除外（akasyx_crawler ignore.DEFAULT_IGNORE_PATTERNS、どの深さでも）。
        # detector は --gitignore-mode off で呼ぶので、これ以外に crawler が黙って飛ばすものは無い
        dirs[:] = [d for d in dirs if d not in (META_DIRNAME, CRAWLER_DEFAULT_IGNORED_DIR)]
        for n in names:
            if n in OS_JUNK_FILES:
                continue
            try:
                st = os.lstat(os.path.join(dirpath, n))
            except OSError as e:
                _unreadable(e)
            if stat.S_ISREG(st.st_mode) and st.st_size > 0:
                return True
    return False


def _same_location(a: str, b: str, st_b: os.stat_result | None = None) -> bool:
    """2 つのパスが同じ実体のフォルダか（シンボリックリンク・大文字小文字違いの表記も含む）。

    判定は OS に実体を問い合わせる（st_dev と st_ino）。文字列を大文字小文字無視で比べると、
    区別するボリューム上の別々のフォルダを 1 つの登録にまとめてしまう（入れ子の判定とは逆に、
    ここでの一致しすぎは危険側）。どちらかが存在しなければ同じ場所ではない。
    ファイル ID を返さない FS（一部の SMB 等で st_ino が 0）では ID が当てにならないので、
    実体のパスの比較に落とす。st_b は b の stat を使い回すときに渡す。
    """
    try:
        st_a = os.stat(a)
        st_b = st_b if st_b is not None else os.stat(b)
    except OSError:
        return False
    if st_a.st_ino == 0 or st_b.st_ino == 0:
        return _same_real_path(a, b)
    return os.path.samestat(st_a, st_b)


def _same_real_path(a: str, b: str) -> bool:
    """ファイル ID が使えないときの代わり: 実体のパスが同じか。

    最後の要素（フォルダ名）の大文字小文字だけ違う場合は、親フォルダの中にその名前が 1 つしか無ければ
    同じフォルダ（区別しないボリュームで表記が違うだけ）、2 つあれば別々のフォルダとみなす。
    親フォルダの表記が違う場合は確かめようがないので別々とみなす（一致しすぎは危険側）。
    """
    ra = os.path.normcase(os.path.realpath(a))
    rb = os.path.normcase(os.path.realpath(b))
    if ra == rb:
        return True
    if os.path.dirname(ra) != os.path.dirname(rb) or ra.casefold() != rb.casefold():
        return False
    name = os.path.basename(ra).casefold()
    try:
        spellings = [n for n in os.listdir(os.path.dirname(ra)) if n.casefold() == name]
    except OSError:
        return False
    return len(spellings) == 1


def _find_by_location(session, root: str) -> Archive | None:
    """登録済みの保存フォルダのうち、root と同じ実体の場所にあるもの。

    文字列の一致だけだと、archive.id を失った保存フォルダを別表記（シンボリックリンク経由・
    大文字小文字違い）で開いたときに見つけられず、同じ実体を新規登録して重複を作る。
    保存フォルダの行は少ないので全件を比べる。

    同じ表記の登録があればそれを使う（接続していないネットワークの場所を stat して待たないため）。
    無ければ実体で照合し、複数一致したら最後に使った行を選んで警告する（文字列一致しか見ていなかった
    頃に同じ実体が重複登録されていることがあるため）。同じ表記の登録が複数あるときも同じ規則で選ぶ。
    制限: 同じ表記の登録が無いときは全登録を順に stat するので、接続していないネットワークの登録が
    あるとその分待たされる（archive.id を失ったときだけ起きる。将来の課題）。
    """
    rows = session.query(Archive).all()
    # 同じ表記の登録があれば stat せずに済ませる（登録に接続していないネットワークの場所があると
    # stat がタイムアウトまで待つため、まず文字列で当てる）
    matches = [r for r in rows if r.root_abs == root]
    if not matches:
        try:
            st_root = os.stat(root)
        except OSError:
            return None
        matches = [r for r in rows if _same_location(r.root_abs, root, st_root)]
    if not matches:
        return None
    # DB から読んだ値は naive、同じセッション内で入れた値は aware なので揃えて比べる
    matches.sort(
        key=lambda r: (r.last_used_at.replace(tzinfo=None) if r.last_used_at else datetime.min, r.id),
        reverse=True,
    )
    if len(matches) > 1:
        logger.warning(
            "Several registrations point to this archive folder; using the most recently used one: "
            + ", ".join(f"#{r.id} {r.root_abs}" for r in matches)
        )
    return matches[0]


def has_archive_marker(directory: str) -> bool:
    """保存フォルダの目印があるか。v0.2 の `.akasyx/archive.id` と、まだ開いていない v0.1.x の `.akasyx/archive.db`。"""
    return os.path.isfile(archive_id_path(directory)) or os.path.isfile(legacy_db_path(directory))


def enclosing_archive(path: str) -> str | None:
    """path 自身か、その上位にある保存フォルダ（目印 has_archive_marker がある）を返します。無ければ None。

    正本 DB の登録ではなく実物の目印で見る。移動したまま開いていない保存フォルダ、別の正本 DB の
    保存フォルダも拾い、目印を消した（もう保存フォルダではない）フォルダは拾わない（#6）。
    """
    return ArchiveLookup().enclosing(path)


def archives_below(
    root: str, follow_symlinks: bool = False, limit: int = 5, exclude_root: bool = False
) -> list[str]:
    """root の中にある保存フォルダ（`.akasyx/archive.id` のあるフォルダ）を返します（最大 limit 件）。

    add の事前チェック用。crawler の走査（全ハッシュ）より前に断れるよう、メタデータだけを歩く。
    `.akasyx/` の中には入らない。読めないフォルダは crawler も読めないので飛ばす。
    """
    found = []
    seen: set[tuple[int, int]] = set()
    for dirpath, dirnames, _ in os.walk(root, followlinks=follow_symlinks):
        if follow_symlinks:
            # シンボリックリンクを辿ると循環しうる（os.walk は検出しない）。実体で一度だけ歩く
            try:
                st = os.stat(dirpath)
            except OSError:
                dirnames.clear()
                continue
            if (st.st_dev, st.st_ino) in seen:
                dirnames.clear()
                continue
            seen.add((st.st_dev, st.st_ino))
        if META_DIRNAME in dirnames:
            if has_archive_marker(dirpath) and not (exclude_root and dirpath == root):
                found.append(dirpath)
                if len(found) >= limit:
                    break
            dirnames.remove(META_DIRNAME)
    return found


class ArchiveLookup:
    """1 回の実行で共有する保存フォルダの確認結果（add と delete-duplicates で共用）。

    ネットワーク上の場所を 1 件ごとに問い合わせないよう、フォルダ・保存フォルダごとに 1 回だけ確かめる。
    """

    def __init__(self):
        self._enclosing: dict[str, str | None] = {}  # 実体のパス → それを含む保存フォルダ
        self._available: dict[int, tuple[bool, str | None]] = {}
        # add: つながっていない保存フォルダにしか無い重複の件数（保存フォルダごと。サマリで知らせる）
        self.unavailable_duplicates: dict[int, int] = {}

    def enclosing(self, path: str) -> str | None:
        """path を含む保存フォルダ（enclosing_archive と同じ判定）。上位フォルダごとの結果を使い回す。"""
        d = os.path.realpath(path)
        if not os.path.isdir(d):
            d = os.path.dirname(d)
        chain = []
        result = None
        while d not in self._enclosing:
            chain.append(d)
            if has_archive_marker(d):
                result = d
                break
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        else:
            result = self._enclosing[d]
        for c in chain:
            # 目印のあるフォルダより下は、その保存フォルダの中
            self._enclosing[c] = result
        return result

    def available(self, row: Archive) -> tuple[bool, str | None]:
        """保存フォルダが登録上の場所に今あるか。戻り値は (ある, 無い理由)。識別子を読めなければ無い扱い。"""
        if row.id not in self._available:
            try:
                ok = is_at_registered_location(row)
                reason = None if ok else f"The archive folder is not available: {row.root_abs}"
            except PreflightError as e:
                ok, reason = False, f"Cannot read the archive folder: {e}"
            self._available[row.id] = (ok, reason)
        return self._available[row.id]


def promote_many(session, rows) -> list:
    """rows を stored にします。同じ内容が今、別の保存フォルダに stored なら stored にしません（#6）。

    「同じ内容の stored は全保存フォルダで 1 つ」を守る唯一の入口。add 以外で行を stored にする経路
    （中断の復旧・verify の missing 復活・v0.1.x の DB の取り込み・forget の取り消し）はすべてここを通す。
    add は判定（find_owning）と正本 DB のロックで同じことを守っている。
    stored にしなかった行は触らない（どうするかは呼び出し側が決める）。戻り値は stored にした行。
    """
    rows = list(rows)
    hashes = sorted({r.filehash for r in rows if r.filehash})
    holders: dict[tuple[str, str], set[int]] = {}
    for i in range(0, len(hashes), 500):  # SQLite の変数の上限を超えないよう分ける
        chunk = hashes[i : i + 500]
        for h, algo, aid in session.query(
            ArchiveFile.filehash, ArchiveFile.hash_algo, ArchiveFile.archive_id
        ).filter(ArchiveFile.status == STATUS_STORED, ArchiveFile.filehash.in_(chunk)):
            holders.setdefault((h, algo), set()).add(aid)
    promoted = []
    for r in rows:
        key = (r.filehash, r.hash_algo)
        if holders.get(key, set()) - {r.archive_id}:
            continue
        r.status = STATUS_STORED
        holders.setdefault(key, set()).add(r.archive_id)  # 同じ呼び出しの中で別の保存フォルダと重ならないように
        promoted.append(r)
    return promoted


def check_tree_readable(root: str) -> None:
    """root の配下がすべて読めることを確かめます（adopt の走査の前 — #4 / #5）。

    crawler は読めないフォルダを警告だけ出して飛ばし、走査を completed で終える。adopt がそれを
    知らずに確定すると一部だけ登録され、記録があるので adopt をやり直せず、登録し損ねた内容と同じ
    ファイルをあとの add が取り込んでしまう。読めない場所があれば 1 件も登録する前に断る。
    """
    problems: list[str] = []

    def _unreadable(e: OSError):
        problems.append(f"{e.filename}: {e.strerror or e}")

    for dirpath, dirs, names in os.walk(root, onerror=_unreadable):
        dirs[:] = [d for d in dirs if d not in (META_DIRNAME, CRAWLER_DEFAULT_IGNORED_DIR)]
        for n in names:
            try:
                os.lstat(os.path.join(dirpath, n))
            except OSError as e:
                _unreadable(e)
        if len(problems) >= 5:
            break
    if problems:
        listed = "\n".join(f"  {p}" for p in problems)
        raise PreflightError(
            "Part of the archive folder cannot be read, so adopt did not register anything:\n"
            f"{listed}\n"
            "  Fix the permissions (or remove these) and run adopt again."
        )


def is_at_registered_location(row: Archive) -> bool:
    """登録上の場所に、この保存フォルダ（同じ uid）が今あるか。

    フォルダが無い（移動した・NAS が外れている）か、別の保存フォルダが置かれていれば False。
    識別子を読めないときは PreflightError（扱いは呼び出し側で決める）。
    """
    return os.path.isdir(row.root_abs) and read_uid(row.root_abs) == row.uid


def _is_live_copy_source(row: Archive, root: str) -> bool:
    """登録上の場所に、同じ uid の保存フォルダがまだ残っているか（= root はその複製）。

    呼び出し側で「root は登録上の場所とは別の実体」と確かめてから呼ぶ（同じ場所の別表記は複製ではない）。
    """
    try:
        return is_at_registered_location(row)
    except PreflightError as e:
        # もう使っていない場所の不調で、移動した保存フォルダまで開けなくしない
        logger.warning(f"Could not check the previous location; treating as moved: {e}")
        return False


def check_new_archive_placement(root: str) -> None:
    """新しく登録する保存フォルダが、別の保存フォルダの中にも、中に別の保存フォルダを含む位置にもないこと（#6）。

    どちらも新しく登録するときだけ見る（#6 より前に登録済みの入れ子の保存フォルダは、これまでどおり開ける）。
    中に含むと、中の保存物を verify が未登録として拾い、同じ実体を 2 つの保存フォルダが持つことになる。
    """
    parent = os.path.dirname(root)
    # ドライブのルート（E:\ や /）は親が自分自身。自分の目印を「上位の保存フォルダ」と取り違えない
    outer = enclosing_archive(parent) if parent != root else None
    if outer is not None:
        raise PreflightError(
            "This folder is inside another archive folder, so it cannot be an archive folder:\n"
            f"  folder            : {root}\n"
            f"  enclosing archive : {outer}"
        )
    nested = archives_below(root, exclude_root=True)
    if nested:
        listed = "\n".join(f"  {d}" for d in nested)
        raise PreflightError(
            f"This folder contains another archive folder, so it cannot be an archive folder:\n{listed}"
        )


def resolve_archive(session, archive_root: str, adopting: bool = False) -> Archive:
    """保存フォルダに対応する ar_archives 行を返します（無ければ登録）。

    1. `.akasyx/archive.id` があれば uid で探す。見つかれば、パスが変わっていれば更新する
       （保存フォルダを移動・改名した場合）
    2. 無ければ場所で探す（識別子ファイルだけ消えた場合）。シンボリックリンク経由や大文字小文字
       違いの表記でも同じ実体なら同じ行とみなす。見つかれば識別子を書き戻す
    3. どちらも無ければ新規登録し、識別子を書く。v0.1.x の `.akasyx/archive.db` が
       残っていればその内容を取り込む（旧 DB は `.migrated-<日時>` に改名して残す）

    次の 2 つは PreflightError で断る（どのサブコマンドでも。登録を作らないため）:
    - 識別子があるのに DB に無く、中にファイルがある: 別の正本 DB で使われていた保存フォルダを
      空の登録として扱うと、既にある内容と同じファイルまで取り込んで重複を作る。
      add 以外で登録を許すと、その後の add が「登録済み」として素通りするので全コマンドで断る
    - 同じ uid の元の保存フォルダがまだ別の場所にある: フォルダの複製。移動とみなすと
      2 つのフォルダが 1 つの登録を交互に書き換える
    """
    # 登録には実体のパスを記録する（シンボリックリンク等の一時的な別名を記録すると、別名が消えたあとに
    # 場所で見つけられなくなる）。新規登録・移動・別表記のどの分岐でもこの形を使う
    root = os.path.realpath(archive_root)
    uid = read_uid(root)

    row = None
    if uid:
        row = session.query(Archive).filter(Archive.uid == uid).first()
    # 識別子で見つかった = forget した保存フォルダそのものが戻ってきた（場所の一致だけでは同じとは言えない）
    returned = row is not None
    if row is None:
        row = _find_by_location(session, root)
        if row is not None and uid and row.uid != uid:
            row = None  # 同じ場所に別の保存フォルダが置かれた。パス一致では同一視しない

    if adopting and _legacy_will_import(session, row, root):
        # v0.1.x の保存フォルダは開けば移行で記録ができるので adopt は要らない。登録や adopt 待ちの印を作る前に
        # 断る（先に印を付けると、移行した行と adopt の行がパスの一意索引でぶつかって adopt が毎回落ち、印が残る）
        raise PreflightError(
            "This is a v0.1.x archive folder (it has .akasyx/archive.db); adopt is not needed.\n"
            f"  Open it with another command (e.g. verify) to migrate its records: {root}"
        )
    if row is None:
        # 新しく登録する保存フォルダの中に別の保存フォルダがあれば断る（#6）。中の保存物を verify が
        # 未登録として拾い、同じ実体を 2 つの保存フォルダが持つことになる。歩くのは初回の登録時だけ
        check_new_archive_placement(root)
        # v0.1.x の DB がある保存フォルダは、このあと移行（import_legacy_db）で記録ができるので断らない
        if not adopting and not os.path.isfile(legacy_db_path(root)) and _has_stored_content(root):
            # 中身があるのに記録が無いまま登録すると、中にある内容と同じファイルまで取り込んで重複を作る（#4 / #5）。
            # 識別子を書く前に断る（断るだけの実行で利用者のフォルダに登録の跡を残さない）
            if uid:
                raise PreflightError(
                    "This archive folder is not registered in the master DB, but it already contains files:\n"
                    f"  archive folder: {root} (ID {uid})\n"
                    "  It may have been used with a different master DB (e.g. development vs. packaged app,\n"
                    "  another computer). Specify the master DB it was used with via --archive-db.\n"
                    f"  If that master DB is lost, register the files in it again: adopt {root}"
                )
            raise PreflightError(
                "This folder already contains files, but has no records in the master DB:\n"
                f"  folder: {root}\n"
                "  Using it as is could store duplicates of files already in it.\n"
                f"  Register the files in it first: adopt {root}"
            )
        if uid:
            state = "registering the files in it (adopt)" if adopting else "the archive folder is empty"
            logger.warning(f"ID {uid} is not in the DB; {state}; registering it with this ID")
        row = Archive(uid=uid or uuid.uuid4().hex, root_abs=root, last_used_at=utcnow())
        session.add(row)
        if adopting:
            # 登録と同じコミットで adopt 待ちにする（この後 adopt が落ちても、他のコマンドは断る）
            session.flush()
            session.add(PendingAdoption(archive_id=row.id))
        session.commit()
        _write_uid(root, row.uid)
        logger.info(f"Registered archive folder: #{row.id} {root}")
    else:
        if (
            not returned
            and not adopting
            and _has_forgotten(session, row)
            # v0.1.x の保存フォルダで、このあと移行（import_legacy_db）が本当に記録を作るときは断らない（新規登録と同じ）
            and not _legacy_will_import(session, row, root)
            and _has_stored_content(root)
        ):
            # forget した保存フォルダの跡（同じ場所）に、識別子の無い別のフォルダが置かれた。forget した記録は
            # この中身と無関係なので（_settle_forgotten が missing にする）、記録が無いのと同じ。識別子を書く前に断る
            raise PreflightError(
                "This folder is where a forgotten archive folder was, and it contains files that have no records:\n"
                f"  folder: {root}\n"
                f"  Register the files in it first: adopt {root}"
            )
        moved = row.root_abs != root and not _same_location(row.root_abs, root)
        if moved and _is_live_copy_source(row, root):
            raise PreflightError(
                "This archive folder looks like a copy of another archive folder (same ID):\n"
                f"  this folder    : {root}\n"
                f"  registered one : {row.root_abs} (ID {row.uid})\n"
                "  To use the copy as a separate archive, delete its .akasyx/archive.id first.\n"
                "  If you moved the folder, remove or rename the old one."
            )
        if moved:
            logger.info(f"Archive folder location changed: {row.root_abs} -> {root}")
            row.root_abs = root
        elif row.root_abs != root and os.path.realpath(row.root_abs) != row.root_abs:
            # 同じ場所だが、登録上のパスが別名（以前の版がシンボリックリンク経由のまま記録した等）なら
            # 実体のパスに直す。登録上のパスが既に実体で、大文字小文字等の表記が違うだけなら書き換えない
            # （開くたびに揺れないように）
            row.root_abs = root
        if adopting and session.get(PendingAdoption, row.id) is None:
            # 識別子の書き込み・forget の片付け（_settle_forgotten）より前に adopt 待ちにする。間で落ちると、
            # 次に開いたとき「戻ってきた保存フォルダ」として adopt 待ちでないまま通常のコマンドが通ってしまう。
            # 複製の検出など断る判定の後に置く（断る保存フォルダに印を残さない）
            # 識別子で戻ってきた保存フォルダでは、forget した記録もこのあと stored に戻るので記録として数える。
            # forget した跡（場所の一致だけ）では、残っている pending（add の中断の残り）は置かれたフォルダの
            # 中身と関係が無いので数えない（_settle_forgotten が failed にする）
            trace = not returned and _has_forgotten(session, row)
            _refuse_adopt_if_records(
                session, row, root, include_forgotten=returned, include_pending=not trace
            )
            session.add(PendingAdoption(archive_id=row.id))
            session.commit()
        # forget した記録の片付けは識別子を書く前に行う。識別子を書いた後に落ちると、次に開いたとき
        # 識別子で見つかって「戻ってきた保存フォルダ」と取り違え、跡の forget した記録を stored に戻してしまう
        _settle_forgotten(session, row, returned)
        if not uid:
            _write_uid(root, row.uid)
        row.last_used_at = utcnow()
        session.commit()

    # v0.1.x の DB が保存フォルダに残っていれば取り込む（登録済みかどうかに関係なく）
    migrated = import_legacy_db(session, row, legacy_db_path(root))
    if migrated:
        logger.info(f"Imported from legacy DB: {migrated}")
    _check_adoption_state(session, row, root, adopting)
    os.makedirs(tmp_dir(root), exist_ok=True)
    return row


def _legacy_will_import(session, row: Archive | None, root: str) -> bool:
    """このあと import_legacy_db が v0.1.x の DB から記録を作るか。

    取り込み済みの印（ar_legacy_imports、保存フォルダごとに 1 つ）がある登録では、旧 DB があっても
    改名されるだけで取り込まれない（forget した移行済みの保存フォルダの跡に、別の v0.1.x を置いた場合など）。
    """
    if not os.path.isfile(legacy_db_path(root)):
        return False
    if row is None:
        return True
    return session.query(LegacyImport).filter_by(archive_id=row.id).first() is None


def _has_forgotten(session, row: Archive) -> bool:
    return (
        session.query(ArchiveFile.id)
        .filter(ArchiveFile.archive_id == row.id, ArchiveFile.status == STATUS_FORGOTTEN)
        .first()
        is not None
    )


def _check_adoption_state(session, row: Archive, root: str, adopting: bool) -> None:
    """adopt 待ちの保存フォルダは adopt 以外で使わせません。adopt は保存記録があれば断ります（#4 / #5）。

    「中身があるのに記録が無い」を記録の状態（stored / missing / unregistered …）から推し量る判定は、
    verify・forget・別の保存フォルダの変化で状態が書き換わるたびにずれたのでやめ、adopt 待ちの印
    （ar_pending_adoptions）で決める（2026-09-28 決定）。新しく登録するフォルダに中身があるときは
    resolve_archive が識別子を書く前に断る。印が外れた後に手で入れたファイルは、従来どおり verify が扱う。
    """
    pending = session.get(PendingAdoption, row.id) is not None
    if not adopting and pending:
        raise PreflightError(
            "The files in this archive folder have not been registered yet (adopt did not finish):\n"
            f"  archive folder: {root}\n"
            f"  Run adopt again: adopt {root}"
        )
    if adopting and not pending:
        _refuse_adopt_if_records(session, row, root)


def _refuse_adopt_if_records(
    session, row: Archive, root: str, include_forgotten: bool = False, include_pending: bool = True
) -> None:
    """保存記録（stored / pending）がある保存フォルダへの adopt は断ります（実体の変化は verify）。

    include_forgotten: forget した保存フォルダが識別子ごと戻ってきたとき。forget した記録は
    _settle_forgotten が stored に戻すので記録として数える（数えないと adopt の行とぶつかる）。
    """
    statuses = [s for s in OWNING_STATUSES if include_pending or s != STATUS_PENDING]
    if include_forgotten:
        statuses.append(STATUS_FORGOTTEN)
    has_owning = (
        session.query(ArchiveFile.id)
        .filter(ArchiveFile.archive_id == row.id, ArchiveFile.status.in_(statuses))
        .first()
        is not None
    )
    if has_owning:
        raise PreflightError(
            f"This archive folder already has records; use verify to check it: {root}"
        )


# --- v0.1.x → v0.2.0 移行 ---------------------------------------------------


def _columns(table_name: str) -> set[str]:
    return {c.name for c in Base.metadata.tables[table_name].columns}


def import_legacy_db(session, archive: Archive, legacy_path: str) -> dict | None:
    """保存フォルダ内に残る v0.1.x の archive.db を正本 DB へ取り込みます。

    3 テーブルを id の対応表を作りながらコピーし、archive_id を付ける。
    取り込み後、旧 DB は削除せず `archive.db.migrated-<日時>` に改名する（-wal / -shm も）。
    取り込み済みの印（ar_legacy_imports）をデータと同じトランザクションで書くので、
    改名に失敗して旧 DB が残っても次回は再取り込みせず改名だけやり直す。
    """
    if not os.path.isfile(legacy_path):
        return None
    # WAL に残っている分を本体へ書き戻す。読み取り（mode=ro）でも改名後に残す控えでも、
    # 本体 1 ファイルだけで内容が揃うようにする
    checkpointed = _checkpoint_legacy(legacy_path)
    done = session.query(LegacyImport).filter_by(archive_id=archive.id).first()
    if done is not None:
        logger.warning(
            f"A v0.1.x DB was already imported into this archive folder at {done.imported_at}; "
            f"not importing again, only renaming: {legacy_path} "
            "(if this is a different v0.1.x DB, e.g. placed where a forgotten archive folder was, "
            "its history is not imported; its files are registered by adopt)"
        )
        _rename_legacy(legacy_path, checkpointed)
        return None
    logger.warning(f"Found a v0.1.x DB; importing it into the master DB: {legacy_path}")

    conn = sqlite3.connect(f"file:{legacy_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    counts = {"ingests": 0, "files": 0, "items": 0}
    try:
        ingest_ids: dict[int, int] = {}
        file_ids: dict[int, int] = {}

        cols = _columns("ar_ingests") - {"id", "archive_id"}
        for r in conn.execute("SELECT * FROM ar_ingests ORDER BY id"):
            data = {k: r[k] for k in r.keys() if k in cols}
            _fix_datetimes(data)
            obj = Ingest(archive_id=archive.id, **data)
            session.add(obj)
            session.flush()
            ingest_ids[r["id"]] = obj.id
            counts["ingests"] += 1

        cols = _columns("ar_archive_files") - {"id", "archive_id"}
        files: list[tuple[int, ArchiveFile]] = []
        for r in conn.execute("SELECT * FROM ar_archive_files ORDER BY id"):
            data = {k: r[k] for k in r.keys() if k in cols}
            _fix_datetimes(data)
            if data.get("ingest_id") is not None:
                data["ingest_id"] = ingest_ids.get(data["ingest_id"])
            files.append((r["id"], ArchiveFile(archive_id=archive.id, **data)))
        # stored にするかは 1 回でまとめて決める（#8。行ごとに問い合わせると stored の数だけクエリが走る）。
        # session に入れる前に決める（入れてからだと autoflush で stored のまま書かれ、判定に自分が混ざる）
        candidates = [obj for _, obj in files if obj.status == STATUS_STORED]
        promoted = {id(obj) for obj in promote_many(session, candidates)}
        for obj in candidates:
            if id(obj) not in promoted:
                # 同じ内容の stored は全体で 1 つ（#6）。実体は残るので未登録として取り込む
                obj.status = STATUS_UNREGISTERED
        # 読んだ順に入れて 1 回で flush し、明細（ingest_items）が参照する id の対応表を作る
        session.add_all([obj for _, obj in files])
        session.flush()
        for legacy_id, obj in files:
            file_ids[legacy_id] = obj.id
        counts["files"] = len(files)

        cols = _columns("ar_ingest_items") - {"id"}
        for r in conn.execute("SELECT * FROM ar_ingest_items ORDER BY id"):
            data = {k: r[k] for k in r.keys() if k in cols}
            _fix_datetimes(data)
            data["ingest_id"] = ingest_ids.get(data.get("ingest_id"))
            if data.get("archive_file_id") is not None:
                data["archive_file_id"] = file_ids.get(data["archive_file_id"])
            if data["ingest_id"] is None:
                continue  # 実行行の無い孤児は捨てる（v0.1 では起きないはず）
            session.add(IngestItem(**data))
            counts["items"] += 1

        session.add(LegacyImport(archive_id=archive.id, legacy_path=legacy_path))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        conn.close()

    _rename_legacy(legacy_path, checkpointed)
    return counts


def _checkpoint_legacy(legacy_path: str) -> bool:
    """旧 DB の WAL を本体へ書き戻します（-wal が無ければ何もしない）。成否を返す。"""
    if not os.path.exists(legacy_path + "-wal"):
        return True
    try:
        conn = sqlite3.connect(legacy_path)
        try:
            busy, _log, _done = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning(f"Could not checkpoint the v0.1.x DB's WAL: {legacy_path}: {e}")
        return False
    return busy == 0


def _rename_legacy(legacy_path: str, checkpointed: bool) -> None:
    """取り込み済みの旧 DB を `.migrated-<日時>` に改名します。

    本体から改名し、失敗したらそこで止めて次回に再試行する（本体が残るので再試行される）。
    本体の後の -wal / -shm は、WAL を書き戻せていれば中身は本体にあるので、改名に失敗しても
    そのまま残す（次回は本体が無いので再試行されない。書き戻せていなければその旨を警告する）。
    """
    stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
    try:
        os.replace(legacy_path, f"{legacy_path}.migrated-{stamp}")
    except OSError as e:
        logger.warning(f"Could not rename the imported v0.1.x DB (will retry next run): {legacy_path}: {e}")
        return
    for suffix in ("-wal", "-shm"):
        src = legacy_path + suffix
        if os.path.exists(src):
            try:
                os.replace(src, f"{legacy_path}.migrated-{stamp}{suffix}")
            except OSError as e:
                note = (
                    "its contents were already written back to the DB"
                    if checkpointed
                    else "the kept copy may lack the changes that were only in it"
                )
                logger.warning(f"Could not rename {src}; left in place ({note}): {e}")


_DT_COLUMNS = {
    "started_at", "finished_at", "origin_modified_at", "verified_at",
    "created_at", "updated_at", "resolved_at", "last_used_at",
}


def _fix_datetimes(data: dict) -> None:
    """sqlite3 が文字列で返す TIMESTAMP を datetime に直します（SQLAlchemy の型検査を通すため）。"""
    for key in list(data):
        if key in _DT_COLUMNS and isinstance(data[key], str):
            try:
                data[key] = datetime.fromisoformat(data[key])
            except ValueError:
                data[key] = None


def _settle_forgotten(session, row: Archive, returned: bool) -> None:
    """archives --forget で外した記録（forgotten）を、保存フォルダを開いたときに片付けます（#6）。

    - 識別子（uid）で見つかった = 外していた保存フォルダそのものが戻った: promote_many で stored に戻す
      （今、別の保存フォルダに stored がある内容は戻さない）。同じパスが別の行に使われているものも戻さない
    - 場所の一致だけ（消した跡に作り直したフォルダ等）: 元の保存物はもう無い
    戻さなかった行は missing にする。ファイルは触らない（実体があれば次の verify が未登録として拾う）。
    """
    rows = (
        session.query(ArchiveFile, func.lower(ArchiveFile.stored_path_rel))
        .filter(ArchiveFile.archive_id == row.id, ArchiveFile.status == STATUS_FORGOTTEN)
        .order_by(ArchiveFile.id)
        .all()
    )
    if not rows:
        return
    candidates = []
    if returned:
        # パスの衝突は mover._path_taken_in_db と同じく SQL の lower() で比べる
        taken = {
            p
            for (p,) in session.query(func.lower(ArchiveFile.stored_path_rel)).filter(
                ArchiveFile.archive_id == row.id, ArchiveFile.status.in_(PATH_HOLDING_STATUSES)
            )
        }
        for f, lowered in rows:
            if lowered not in taken:
                taken.add(lowered)
                candidates.append(f)
    restored = promote_many(session, candidates)
    for f, _ in rows:
        if f.status == STATUS_FORGOTTEN:
            f.status = STATUS_MISSING
    if not returned:
        # 跡に置かれた別のフォルダには、forget した保存フォルダの中断した予約（pending）の実体は無い。
        # 復旧（recover_pending）に回すと、同じパスの別のファイルを予約の完了と取り違えうるので failed にする
        for f in session.query(ArchiveFile).filter(
            ArchiveFile.archive_id == row.id, ArchiveFile.status == STATUS_PENDING
        ):
            f.status = STATUS_FAILED
    session.commit()
    logger.info(
        f"Archive folder #{row.id} was forgotten: restored {len(restored)} of {len(rows)} records"
        + ("" if returned else " (not the same folder; the records are left as missing)")
    )


def forget_archive(session, archive_id: int, expect_uid: str | None = None) -> int:
    """消した保存フォルダの登録を外します（#6）。戻り値は重複判定の対象から外した行数。

    重複判定は全保存フォルダ共通なので、消した保存フォルダの保存記録が残ると、その内容は二度と
    どこにも保存されない。stored の保存記録を forgotten にして重複判定から外す（行と履歴は消さない）。
    保存フォルダがその場所にある（つながっている）なら断る。NAS を外していただけ・移動しただけの
    保存フォルダを外してしまっても、次に開いたとき resolve_archive が記録を stored に戻す（_settle_forgotten）。
    pending（中断の残り）は別の保存フォルダの判定に使われないので触らない（開いたときの復旧に任せる）。
    expect_uid（UI が一覧で見た保存フォルダの uid）があれば、違う保存フォルダは外さない（#11）。ID は正本 DB ごとの
    番号なので、一覧を読んだ後に正本 DB の指定が変わると、同じ ID の別の保存フォルダを指しうる。
    """
    row = session.get(Archive, archive_id)
    if row is None:
        raise PreflightError(f"No archive folder with ID #{archive_id} is registered")
    if expect_uid is not None and row.uid != expect_uid:
        raise PreflightError(
            f"Archive folder #{archive_id} is not the one expected (uid {row.uid}, expected {expect_uid}); "
            "reload the list of archive folders"
        )
    # 識別子ファイルだけ消えた保存フォルダも「ある」とみなす（開けば場所で見つかり識別子が書き戻される）
    present = is_at_registered_location(row) or (
        os.path.isdir(row.root_abs) and read_uid(row.root_abs) is None
    )
    if present:
        raise PreflightError(
            f"Archive folder #{archive_id} is present at its location; it cannot be forgotten:\n"
            f"  {row.root_abs}"
        )
    rows = (
        session.query(ArchiveFile)
        .filter(ArchiveFile.archive_id == archive_id, ArchiveFile.status == STATUS_STORED)
        .all()
    )
    if not rows:
        # stored が無い保存フォルダは重複判定を塞がない（別の保存フォルダの pending は持ち主にしない）ので、
        # 外すものが無い。forgotten の行ができない forget 済みの状態を作らないためにも断る
        raise PreflightError(
            f"Archive folder #{archive_id} has no stored records; there is nothing to forget: {row.root_abs}"
        )
    for f in rows:
        # 消したのではなく外れていただけなら、開き直したときに戻す（_settle_forgotten）
        f.status = STATUS_FORGOTTEN
    session.commit()
    logger.info(
        f"Forgot archive folder #{archive_id} ({row.root_abs}): "
        f"{len(rows)} records are no longer used for duplicate detection"
    )
    print(f"Forgot archive folder #{archive_id}: {row.root_abs} ({len(rows)} records)")
    return len(rows)


def list_archives(session) -> list[tuple[Archive, int, int]]:
    """report/archives 用: (保存フォルダ, stored 件数, 合計サイズ) の一覧。"""
    rows = session.query(Archive).order_by(Archive.id).all()
    out = []
    for a in rows:
        count, size = (
            session.query(func.count(ArchiveFile.id), func.coalesce(func.sum(ArchiveFile.size), 0))
            .filter(ArchiveFile.archive_id == a.id, ArchiveFile.status == STATUS_STORED)
            .one()
        )
        out.append((a, int(count), int(size)))
    return out


def forgotten_counts(session) -> dict[int, int]:
    """archives --json 用: 保存フォルダごとの forgotten（登録を外した）件数。"""
    rows = (
        session.query(ArchiveFile.archive_id, func.count(ArchiveFile.id))
        .filter(ArchiveFile.status == STATUS_FORGOTTEN)
        .group_by(ArchiveFile.archive_id)
        .all()
    )
    return {archive_id: int(count) for archive_id, count in rows}


def pending_adoption_ids(session) -> set[int]:
    """archives --json 用: adopt 待ちの保存フォルダの ID。"""
    return {row.archive_id for row in session.query(PendingAdoption).all()}
