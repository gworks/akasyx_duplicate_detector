# test_offline_nas.py - 接続していないネットワークの登録で保存フォルダの照合が待たされない（#7）
#
# 止まっている NAS への stat は OS のタイムアウト（SMB では数十秒）まで戻らない。
# ① 識別子の無いフォルダを開くとき、表記の違う登録には I/O をかけない
# ② 移動した保存フォルダの元の場所の確認は期限で打ち切り、警告して移動として扱う
import logging
import os
import time

import archives
from conftest import ARCHIVE_ID
from database import archive_id_path
from models import Archive

OFFLINE = "/Volumes/offline_nas/archive"  # 止まっている NAS 上の登録を模す


def _register_offline(session):
    row = Archive(uid="offline-uid", root_abs=OFFLINE)
    session.add(row)
    session.commit()
    return row


def test_matching_without_id_does_not_touch_other_registrations(session, archive, tmp_path, monkeypatch):
    """識別子の無いフォルダを開くとき、表記（大文字小文字を除く）の違う登録は stat しない。"""
    _register_offline(session)
    touched = []
    real_stat = os.stat

    def spy(path, *a, **kw):
        if str(path).startswith("/Volumes/offline_nas"):
            touched.append(str(path))
            time.sleep(0.5)  # 止まっている NAS（実際は数十秒）
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(archives.os, "stat", spy)
    new = tmp_path / "new_archive"
    new.mkdir()
    row = archives.resolve_archive(session, str(new))
    assert row.root_abs == os.path.realpath(str(new))
    assert touched == []


def test_matching_without_id_still_finds_other_case_spelling(session, archive, tmp_path):
    """大文字小文字だけ違う表記で開いても、同じ保存フォルダの登録を見つける（識別子を失った場合）。"""
    archives.resolve_archive(session, archive)
    os.unlink(archive_id_path(archive))
    other_case = os.path.join(os.path.dirname(archive), os.path.basename(archive).upper())
    if not os.path.isdir(other_case) or os.path.realpath(other_case) != other_case:
        import pytest

        pytest.skip("大文字小文字を区別するボリューム、または realpath が表記を直すボリューム")
    row = archives.resolve_archive(session, other_case)
    assert row.id == ARCHIVE_ID


def test_unresponsive_previous_location_is_treated_as_moved(session, tmp_path, monkeypatch, caplog):
    """移動した保存フォルダの元の場所が応答しなければ、期限で打ち切り、警告して移動として扱う。"""
    old = tmp_path / "old_place"
    old.mkdir()
    row = archives.resolve_archive(session, str(old))
    new = tmp_path / "new_place"
    os.rename(old, new)

    monkeypatch.setattr(archives, "PREVIOUS_LOCATION_TIMEOUT", 0.2)
    real_stat = os.stat
    old_str = str(old)

    def hanging_stat(path, *a, **kw):
        if str(path).startswith(old_str):
            time.sleep(3)  # 止まっている NAS
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(archives.os, "stat", hanging_stat)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="archives"):
        again = archives.resolve_archive(session, str(new))
    elapsed = time.monotonic() - started
    assert elapsed < 1.5  # 期限（0.2 秒）で打ち切る。元の場所の I/O を 2 回待たない
    assert again.id == row.id
    assert again.root_abs == os.path.realpath(str(new))
    assert any("did not respond" in r.getMessage() for r in caplog.records)  # 黙って移動扱いにしない


def test_matching_without_id_finds_other_unicode_normalization(session, tmp_path):
    """Finder（NFD）とターミナル（NFC）で表記が違っても、識別子を失った同じ保存フォルダの登録を見つける。"""
    import unicodedata

    nfd = str(tmp_path / unicodedata.normalize("NFD", "データ"))
    os.mkdir(nfd)
    nfc = str(tmp_path / unicodedata.normalize("NFC", "データ"))
    if nfc == nfd or not os.path.isdir(nfc):
        import pytest

        pytest.skip("正規化の違いを同じフォルダとして扱わないボリューム")
    row = archives.resolve_archive(session, nfd)
    os.unlink(archive_id_path(nfd))
    again = archives.resolve_archive(session, nfc)
    assert again.id == row.id


def test_zero_file_id_same_folder_with_other_unicode_normalization_is_same(tmp_path, monkeypatch):
    """ファイル ID を返さない FS（一部の SMB）でも、NFC / NFD だけ違う表記の同じフォルダは同じ場所とみなす。"""
    import unicodedata

    nfd = str(tmp_path / unicodedata.normalize("NFD", "データ"))
    os.mkdir(nfd)
    nfc = str(tmp_path / unicodedata.normalize("NFC", "データ"))
    if nfc == nfd or not os.path.isdir(nfc):
        import pytest

        pytest.skip("正規化の違いを同じフォルダとして扱わないボリューム")
    real_stat = os.stat

    def _zero_ino(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        return os.stat_result((st.st_mode, 0, 0, *tuple(st)[3:]))  # st_ino と st_dev を 0 に

    monkeypatch.setattr(archives.os, "stat", _zero_ino)
    assert archives._same_location(nfd, nfc)
    other = tmp_path / "other"
    other.mkdir()
    assert not archives._same_location(nfd, str(other))

