# -*- coding: utf-8 -*-
"""GUI E2E self-test: drive MainWindow through the real thread/signal/table path"""
import sys
import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from xhs_app.ui import MainWindow


def main():
    kw = sys.argv[1] if len(sys.argv) > 1 else "442561078"
    app = QApplication([])
    w = MainWindow()
    w.show()
    w._bridge.log.connect(lambda m: print("[gui-log]", m))
    job = {"thread": None}
    state = {"ok": None}

    def on_done(result, err):
        if err:
            print("[gui] ERROR:", err)
            state["ok"] = False
        else:
            rows = w.table.rowCount()
            kinds = set()
            for r in range(rows):
                it = w.table.item(r, 1)
                if it:
                    kinds.add(it.text())
            print(f"[gui] rows={rows} kinds={sorted(kinds)} status_lb='{w.lb_state.text()}'")
            state["ok"] = rows > 0
        # 等登录态复检（done 后 1.6s 触发）完成再退出
        QTimer.singleShot(4200, lambda: (
            print(f"[gui] btn_login.enabled={w.btn_login.isEnabled()} "
                  f"tip={w.btn_login.toolTip()!r}"),
            app.quit()))

    w._bridge.done.connect(on_done)

    def start():
        w.input.setText(kw)
        w.on_get()
        job["thread"] = threading.enumerate()  # 占位记录

    QTimer.singleShot(600, start)
    QTimer.singleShot(180000, app.quit)  # 兜底超时
    app.exec()
    print("[gui] state ok =", state["ok"])
    sys.exit(0 if state["ok"] else 1)


if __name__ == "__main__":
    main()
