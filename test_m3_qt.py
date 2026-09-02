# -*- coding: utf-8 -*-
"""M3 批量回归（内嵌引擎）：8 篇混合下载 → 二次运行应 0 新下载、全部“已存在(跳过)”"""
import shutil
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QMetaObject, QTimer, Qt
from PySide6.QtWidgets import QApplication, QMainWindow

from xhs_app.embed import EngineView

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / ".m3test"


def log(m):
    print("[m3]", m, flush=True)


class Emit:
    def __init__(self, fn):
        self.fn = fn

    def emit(self, *a):
        self.fn(*a)


class FB:
    """download_job 所需的最小信号桥"""

    def __init__(self):
        self.statuses = []
        self.notices = []

        def on_status(nid, s):
            self.statuses.append((nid, s))

        def on_notice(m):
            self.notices.append(m)
        self.log = Emit(lambda m: print("   ", m))
        self.note_update = Emit(on_status)
        self.notice = Emit(on_notice)


class Win(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("M3 批量回归")
        self.resize(900, 600)
        self.engine = EngineView(self)
        self.setCentralWidget(self.engine)

    def run_job(self):
        threading.Thread(target=self._job, daemon=True).start()

    def _job(self):
        try:
            from xhs_app.collector import open_and_collect
            from xhs_app.resolver import resolve
            from xhs_app.service import download_job

            dp = self.engine.attach()
            meta = resolve(dp, "442561078", log)
            res = open_and_collect(dp, meta, log)
            notes = res.notes
            # 前 8 篇 + 保证含视频
            pick = notes[:8]
            if not any(n.kind == "video" for n in pick):
                v = next((n for n in notes[8:] if n.kind == "video"), None)
                if v:
                    pick[-1] = v
            log(f"批量目标 {len(pick)} 篇：视频 {sum(1 for n in pick if n.kind == 'video')} 个")
            if TARGET.exists():
                shutil.rmtree(TARGET)

            fb1 = FB()
            download_job(meta, pick, str(TARGET), fb1, self.engine.attach)
            s1 = [s for s in (st for _, st in fb1.statuses) if s != "获取详情…"]
            log(f"run1 statuses: {s1}")

            fb2 = FB()
            download_job(meta, pick, str(TARGET), fb2, self.engine.attach)
            s2 = [s for s in (st for _, st in fb2.statuses) if s != "获取详情…"]
            log(f"run2 statuses: {s2}")

            folders = list(TARGET.rglob("*_*"))
            dirs = [p for p in folders if p.is_dir()]
            log(f"下载目录数: {len(dirs)}")
            ok = (all(s == "完成" for s in s1)
                  and len(s2) == len(pick) and all(s == "已存在(跳过)" for s in s2))
            log("== M3 " + ("OK" if ok else "CHECK-FAIL") + " ==")
        except Exception as e:
            import traceback
            traceback.print_exc()
            log("== M3 FAIL: %s" % e)
        finally:
            QMetaObject.invokeMethod(app, "quit", Qt.QueuedConnection)


def main():
    global app
    app = QApplication([])
    w = Win()
    w.show()
    QTimer.singleShot(8000, w.run_job)
    QTimer.singleShot(600000, lambda: (log("TIMEOUT"), app.quit()))
    app.exec()


if __name__ == "__main__":
    sys.exit(main())
