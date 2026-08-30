# errors.py - 例外定義（設計書 §11 の終了コードに対応）


class DetectorError(Exception):
    """detector の処理を止める一般的なエラー（終了コード 1）。"""


class PreflightError(DetectorError):
    """実行前チェックで拒否したときのエラー（終了コード 3）。

    入れ子・crawler 不在・crawler のスキャン不完全・書き込み不可・多重起動など、
    「1件も処理しないまま断る」ケースに使う（設計書 §12）。
    """
