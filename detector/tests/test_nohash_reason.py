# test_nohash_reason.py - ハッシュが無いファイルの理由を CSV・DB・警告の行に出す（#25）
#
# 単一ファイルの取り込みでハッシュが無くなる理由は 2 つある。読んでいる間に変わった（もう一度実行すれば判定し直せる）と、
# 読めなかった。どちらも skipped_nohash だが、以前は判定側の一般的な「ハッシュが無い」という文言だけで、CSV と UI から
# 区別できなかった（ログファイルの WARNING を見るしかなかった）。
import csv
import glob
import logging
import os

import crawler_client
import ingest
import main
from conftest import archive_db_path, write_file
from database import get_session
from crawler_client import ScannedFile
from models import RESULT_SKIPPED_NOHASH, IngestItem
from utl import hashing


def _csv_rows(config):
    files = sorted(glob.glob(os.path.join(config.log_dir, "add_result_*.csv")))
    assert files, "no CSV report"
    with open(files[-1], encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))[1:]


def _items(tmp_path):
    session, engine = get_session(archive_db_path(tmp_path))
    try:
        return [(i.result, i.message) for i in session.query(IngestItem).all()]
    finally:
        session.close()
        engine.dispose()


def test_changed_while_reading_says_so_and_to_run_again(make_config, archive, tmp_path, monkeypatch, caplog):
    path = write_file(str(tmp_path / "one" / "a.txt"), b"AAA")
    real_hash = hashing.file_hash

    def rewrite_then_hash(p, *a, **k):
        with open(p, "ab") as f:
            f.write(b"MORE")  # ハッシュを取っている間に書き足された
        return real_hash(p, *a, **k)

    monkeypatch.setattr(hashing, "file_hash", rewrite_then_hash)
    config = make_config(archive_root=archive, source_path=path)
    with caplog.at_level(logging.WARNING):
        assert main.run(config) == main.EXIT_OK
    assert os.path.exists(path)  # 移動していない
    [row] = _csv_rows(config)
    assert row[2] == RESULT_SKIPPED_NOHASH
    note = row[5]
    assert "changed while it was being read" in note
    assert "run again" in note
    assert _items(tmp_path) == [(RESULT_SKIPPED_NOHASH, note)]
    # UI の列の行に残る警告の行（詳細ログがたたまれていても読める）にも、もう一度実行すれば判定し直せることを出す
    assert "run again" in caplog.text


def test_unreadable_file_is_told_apart_from_a_changed_one(make_config, archive, tmp_path, monkeypatch):
    path = write_file(str(tmp_path / "one" / "a.txt"), b"AAA")

    def unreadable(p, *a, **k):
        raise PermissionError(13, "Permission denied", p)

    monkeypatch.setattr(hashing, "file_hash", unreadable)
    config = make_config(archive_root=archive, source_path=path)
    assert main.run(config) == main.EXIT_OK
    [row] = _csv_rows(config)
    assert row[2] == RESULT_SKIPPED_NOHASH
    note = row[5]
    assert "could not be read" in note
    assert "Permission denied" in note
    assert "changed while" not in note
    assert _items(tmp_path) == [(RESULT_SKIPPED_NOHASH, note)]


def test_hash_missing_from_the_crawler_keeps_the_generic_message(make_config, archive, session):
    # crawler が読み取りエラーで filehash=NULL にした行（フォルダの取り込み）は理由が分からない。今の文言のまま
    scanned = ScannedFile(name="a.txt", path_abs="/x/a.txt", path_rel="a.txt", size=3, filehash=None, hash_algo=None)
    result, _, message = ingest.judge(session, scanned, make_config(archive_root=archive))
    assert result == RESULT_SKIPPED_NOHASH
    assert message == "Cannot classify: hash unavailable"


def test_file_gone_after_hashing_counts_as_changed(make_config, archive, tmp_path, monkeypatch):
    # 読み終えた後に消えた・差し替わった（stat し直せない）ものは、読めなかったのではなく読んでいる間に変わったもの
    path = write_file(str(tmp_path / "one" / "a.txt"), b"AAA")
    real_hash = hashing.file_hash
    real_stat = os.stat
    hashed = []

    def hash_then_mark(p, *a, **k):
        h = real_hash(p, *a, **k)
        hashed.append(p)
        return h

    def stat_fails_after_hash(p, *a, **k):
        if hashed and os.path.normcase(str(p)) == os.path.normcase(path):
            raise FileNotFoundError(2, "No such file or directory", p)
        return real_stat(p, *a, **k)

    monkeypatch.setattr(hashing, "file_hash", hash_then_mark)
    monkeypatch.setattr(crawler_client.os, "stat", stat_fails_after_hash)
    config = make_config(archive_root=archive, source_path=path)
    assert main.run(config) == main.EXIT_OK
    [row] = _csv_rows(config)
    assert row[2] == RESULT_SKIPPED_NOHASH
    assert "changed while it was being read" in row[5]
    assert "could not be read" not in row[5]


def test_empty_file_written_while_reading_is_not_called_empty(make_config, archive, tmp_path, monkeypatch):
    # stat の時点で 0 バイトでも、読んでいる間に書き足されたら「空」ではなく「もう一度実行すれば判定し直せる」
    path = write_file(str(tmp_path / "one" / "a.txt"), b"")
    real_hash = hashing.file_hash

    def fill_then_hash(p, *a, **k):
        with open(p, "ab") as f:
            f.write(b"DATA")
        return real_hash(p, *a, **k)

    monkeypatch.setattr(hashing, "file_hash", fill_then_hash)
    config = make_config(archive_root=archive, source_path=path)
    assert main.run(config) == main.EXIT_OK
    [row] = _csv_rows(config)
    assert row[2] == RESULT_SKIPPED_NOHASH
    assert "run again" in row[5]


def test_reasons_share_the_cannot_classify_prefix_and_omit_the_path(make_config, archive, tmp_path, monkeypatch):
    # 以前の skipped_nohash の文言はすべて「Cannot classify:」で始まっていた（それで絞り込む人がいても取りこぼさない）。
    # パスは CSV の別の列にあるので、Note に重ねて書かない
    path = write_file(str(tmp_path / "one" / "a.txt"), b"AAA")

    def unreadable(p, *a, **k):
        raise PermissionError(13, "Permission denied", p)

    monkeypatch.setattr(hashing, "file_hash", unreadable)
    config = make_config(archive_root=archive, source_path=path)
    main.run(config)
    [row] = _csv_rows(config)
    assert row[5].startswith("Cannot classify:")
    assert "a.txt" not in row[5]
    assert crawler_client.NOHASH_CHANGED_WHILE_READING.startswith("Cannot classify:")


def test_file_gone_before_reading_counts_as_changed(make_config, archive, tmp_path, monkeypatch):
    # 先の stat の後・読む前に消えたものも、読み終えた後に消えたものと同じく「変わった」（読めなかったではない）
    path = write_file(str(tmp_path / "one" / "a.txt"), b"AAA")

    def gone(p, *a, **k):
        raise FileNotFoundError(2, "No such file or directory", p)

    monkeypatch.setattr(hashing, "file_hash", gone)
    config = make_config(archive_root=archive, source_path=path)
    assert main.run(config) == main.EXIT_OK
    [row] = _csv_rows(config)
    assert "changed while it was being read" in row[5]


def test_stat_failing_for_another_reason_after_hashing_is_unreadable(make_config, archive, tmp_path, monkeypatch):
    # 読み終えた後に stat できない理由が消えた以外（権限・つながっていない共有）なら、読めなかったもの。
    # 「変わった」とは言わない
    path = write_file(str(tmp_path / "one" / "a.txt"), b"AAA")
    real_hash = hashing.file_hash
    real_stat = os.stat
    hashed = []

    def hash_then_mark(p, *a, **k):
        h = real_hash(p, *a, **k)
        hashed.append(p)
        return h

    def denied_after_hash(p, *a, **k):
        if hashed and os.path.normcase(str(p)) == os.path.normcase(path):
            raise PermissionError(13, "Permission denied", p)
        return real_stat(p, *a, **k)

    monkeypatch.setattr(hashing, "file_hash", hash_then_mark)
    monkeypatch.setattr(crawler_client.os, "stat", denied_after_hash)
    config = make_config(archive_root=archive, source_path=path)
    assert main.run(config) == main.EXIT_OK
    [row] = _csv_rows(config)
    assert row[2] == RESULT_SKIPPED_NOHASH
    assert "could not be read (PermissionError: Permission denied)" in row[5]
    assert "changed" not in row[5]


def test_unreadable_empty_file_keeps_its_reason(make_config, archive, tmp_path, monkeypatch):
    # 大きさ（読む前の stat）で「空」と判定して、読めなかった理由を消さない
    path = write_file(str(tmp_path / "one" / "a.txt"), b"")

    def unreadable(p, *a, **k):
        raise PermissionError(13, "Permission denied", p)

    monkeypatch.setattr(hashing, "file_hash", unreadable)
    config = make_config(archive_root=archive, source_path=path)
    assert main.run(config) == main.EXIT_OK
    [row] = _csv_rows(config)
    assert row[2] == RESULT_SKIPPED_NOHASH
    assert "could not be read" in row[5]


def test_one_warning_per_file(make_config, archive, tmp_path, monkeypatch, caplog):
    # UI は WARNING の行を数えて列の行に出す。1 つのファイルに 2 行（「読めなかった」と「変わった」）出さない
    path = write_file(str(tmp_path / "one" / "a.txt"), b"AAA")

    def gone(p, *a, **k):
        raise FileNotFoundError(2, "No such file or directory", p)

    monkeypatch.setattr(hashing, "file_hash", gone)
    with caplog.at_level(logging.WARNING):
        main.run(make_config(archive_root=archive, source_path=path))
    about_the_file = [r for r in caplog.records if r.levelno == logging.WARNING and path in r.getMessage()]
    assert len(about_the_file) == 1
    assert "changed while it was being read" in about_the_file[0].getMessage()
    assert "No such file or directory" in about_the_file[0].getMessage()  # 元のエラーも残す（共有が切れたのかを見分ける）


def test_reason_without_an_os_message_does_not_carry_the_path(make_config, archive, tmp_path, monkeypatch):
    # OS の文（strerror）が無い OSError の str() はパスなど任意の文を含みうる。Note には使わない
    path = write_file(str(tmp_path / "one" / "a.txt"), b"AAA")

    def odd_error(p, *a, **k):
        raise OSError(f"device error while reading {p}")

    monkeypatch.setattr(hashing, "file_hash", odd_error)
    config = make_config(archive_root=archive, source_path=path)
    main.run(config)
    [row] = _csv_rows(config)
    assert row[5].startswith("Cannot classify: the file could not be read (OSError")
    assert "a.txt" not in row[5]


def test_unreachable_share_is_unreadable_not_changed(make_config, archive, tmp_path, monkeypatch):
    # Windows ではつながっていない共有も FileNotFoundError になる（open は winerror を持たない）。入っていたフォルダに
    # 届かないなら、消えたのではないので「変わった・もう一度実行すれば」とは言わない
    path = write_file(str(tmp_path / "share" / "a.txt"), b"AAA")
    share = os.path.dirname(path)
    real_reachable = crawler_client._folder_reachable

    def share_down(p, *a, **k):
        monkeypatch.setattr(
            crawler_client, "_folder_reachable",
            lambda d: False if os.path.normcase(str(d)) == os.path.normcase(share) else real_reachable(d),
        )
        raise FileNotFoundError(2, "No such file or directory", p)  # 本物の open と同じ形（winerror なし）

    monkeypatch.setattr(hashing, "file_hash", share_down)
    config = make_config(archive_root=archive, source_path=path)
    main.run(config)
    [row] = _csv_rows(config)
    assert "could not be read (FileNotFoundError: No such file or directory)" in row[5]
    assert "changed" not in row[5]


def test_warning_does_not_repeat_the_path(make_config, archive, tmp_path, monkeypatch, caplog):
    path = write_file(str(tmp_path / "one" / "a.txt"), b"AAA")

    def gone(p, *a, **k):
        raise FileNotFoundError(2, "No such file or directory", p)

    monkeypatch.setattr(hashing, "file_hash", gone)
    with caplog.at_level(logging.WARNING):
        main.run(make_config(archive_root=archive, source_path=path))
    [msg] = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and path in r.getMessage()]
    assert msg.count(path) == 1


def test_file_replaced_before_reading_counts_as_changed(make_config, archive, tmp_path, monkeypatch):
    # 先の stat の後に別のもの（書き換わったファイル・フォルダ）に差し替わって読めなかったのなら、読めなかったのではなく
    # 変わったもの（stat し直して版が違うことで分かる）
    path = write_file(str(tmp_path / "one" / "a.txt"), b"AAA")

    def replaced_then_denied(p, *a, **k):
        with open(p, "ab") as f:
            f.write(b"MORE")  # 差し替わった（大きさ・更新日時が変わった）
        raise PermissionError(13, "Permission denied", p)

    monkeypatch.setattr(hashing, "file_hash", replaced_then_denied)
    config = make_config(archive_root=archive, source_path=path)
    main.run(config)
    [row] = _csv_rows(config)
    assert "changed while it was being read" in row[5]
