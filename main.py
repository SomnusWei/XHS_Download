# -*- coding: utf-8 -*-
"""小红书作品批量采集下载器 — 入口"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xhs_app.ui import main  # noqa: E402

if __name__ == "__main__":
    main()
