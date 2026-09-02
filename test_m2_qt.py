# -*- coding: utf-8 -*-
"""M2 核心验证（生产同款内嵌 QtWebEngine）：图文+视频各 1 篇 取详情并下载"""
import shutil
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QMetaObject, QTimer, Qt
from PySide6.QtWidgets import QApplication, QMainWindow

from xhs_app.embed import EngineView  # import 时会设置 QtWebEngine 环境变量

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / ".m2test"


def log(m):
    print("[m2q]", m, flush=True)


class Win(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("M2 核心验证（内嵌引擎）")
        self.resize(1000, 700)
        self.engine = EngineView(self)
        self.setCentralWidget(self.engine)

    def run_job(self):
        threading.Thread(target=self._job, daemon=True).start()

    def _job(self):
        try:
            from xhs_app.collector import open_and_collect
            from xhs_app.detail import fetch_detail
            from xhs_app.download import build_target, folder_done
            from xhs_app.resolver import resolve
            from xhs_app.service import _save_file

            dp = self.engine.attach()
            meta = resolve(dp, "442561078", log)
            res = open_and_collect(dp, meta, log)
            log(f"列表 {len(res.notes)} 篇")
            img = next((n for n in res.notes if n.kind == "normal"), None)
            vid = next((n for n in res.notes if n.kind == "video"), None)
            assert img and vid, "缺少图文或视频样本"

            for note in (img, vid):
                fd = fetch_detail(dp, note)
                log(f"{note.kind_label} kind={fd['kind']} urls={len(fd['urls'])}")
                folder = build_target(res.meta, note, TARGET)
                if fd["kind"] == "image":
                    total = len(fd["urls"])
                    width = max(2, len(str(total)))
                    for i, u in enumerate(fd["urls"], 1):
                        _save_file(u, folder, f"{i:0{width}d}")
                else:
                    _save_file(fd["urls"][0], folder, "视频")
                files = sorted(p for p in folder.rglob("*") if p.is_file())
                log(f"  saved {len(files)}: {[f.name for f in files]}")
                for f in files:
                    assert f.stat().st_size > 0
            again = next(n for n in res.notes if n.kind == "normal")
            log("二次 folder_done=" + str(folder_done(build_target(res.meta, again, TARGET))))
            log("== M2 OK ==")
        except Exception as e:
            import traceback
            traceback.print_exc()
            log("== M2 FAIL: %s: %s" % (type(e).__name__, e))
        finally:
            QMetaObject.invokeMethod(app, "quit", Qt.QueuedConnection)


def main():
    global app
    app = QApplication([])
    if TARGET.exists():
        shutil.rmtree(TARGET)
    w = Win()
    w.show()
    QTimer.singleShot(8000, w.run_job)
    QTimer.singleShot(240000, lambda: (log("TIMEOUT"), app.quit()))
    app.exec()


if __name__ == "__main__":
    main()
