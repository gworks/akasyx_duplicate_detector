# conftest.py - テストからトップレベル import（import config 等）を解決する（設計書 §5）
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
