# packaging/collect_licenses.py - 実行中の Python 環境に入っているパッケージのライセンス表記を集める
#
#   uv run --inexact ... python packaging/collect_licenses.py <見出し> [--exclude a,b,...] >> THIRD_PARTY_LICENSES.txt
#   --exclude には配布物に入れないパッケージ（build_python.sh の --exclude-module と揃える）を渡す
#
# MIT / BSD / Apache-2.0 などは再配布時に著作権表示とライセンス文の同梱が要るため、
# 各パッケージのメタデータ（License 欄）と、同梱されているライセンスファイルの本文をそのまま出す。
from __future__ import annotations

import sys
from importlib import metadata

SKIP = {"pyinstaller", "pyinstaller-hooks-contrib", "pip", "setuptools", "wheel", "altgraph", "macholib",
        "packaging", "pytest", "ruff", "httpx", "iniconfig", "pluggy"}
LICENSE_HINTS = ("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS")


def main() -> None:
    args = sys.argv[1:]
    exclude: set[str] = set()
    if "--exclude" in args:
        i = args.index("--exclude")
        exclude = {x.strip().lower().replace("_", "-") for x in args[i + 1].split(",") if x.strip()}
        del args[i : i + 2]
    title = args[0] if args else "Python"
    print("=" * 78)
    print(f"{title}")
    print("=" * 78)
    for dist in sorted(metadata.distributions(), key=lambda d: (d.metadata["Name"] or "").lower()):
        name = dist.metadata["Name"] or "?"
        if name.lower() in SKIP or name.lower().replace("_", "-") in exclude:
            continue
        lic = dist.metadata.get("License-Expression") or dist.metadata.get("License") or ""
        if len(lic) > 80 or not lic.strip():
            classifiers = [c.split("::")[-1].strip() for c in dist.metadata.get_all("Classifier") or [] if c.startswith("License")]
            lic = ", ".join(classifiers) or (lic.splitlines()[0] if lic.strip() else "不明（下の本文を参照）")
        print(f"\n--- {name} {dist.version} — {lic}")
        for f in dist.files or []:
            base = f.name.upper()
            if any(base.startswith(h) for h in LICENSE_HINTS):
                try:
                    print(f"\n[{f}]\n{f.locate().read_text(encoding='utf-8', errors='replace').strip()}")
                except (OSError, FileNotFoundError):
                    pass


if __name__ == "__main__":
    main()
