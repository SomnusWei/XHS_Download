# -*- coding: utf-8 -*-
"""console self-test of M1 service modules (real browser)

用法：
    python test_m1.py [小红书号] [hidden|visible]
默认 hidden 静默模式；传 visible 则显示浏览器窗口。
"""
import sys

from xhs_app import config
from xhs_app.browser import make_page
from xhs_app.collector import fill_meta_from_dom, open_and_collect
from xhs_app.models import LoginRequired
from xhs_app.resolver import resolve


def main():
    kw = sys.argv[1] if len(sys.argv) > 1 else "442561078"
    mode = sys.argv[2] if len(sys.argv) > 2 else "hidden"
    if mode == "visible":
        config.HIDDEN = False
    print(f"[svc] mode={mode} HIDDEN={config.HIDDEN}")
    page = None
    try:
        page = make_page()
        meta = resolve(page, kw, lambda m: print("[svc]", m))
        print("[svc] meta:", meta)
        res = open_and_collect(page, meta, lambda m: print("[svc]", m))
        fill_meta_from_dom(page, res.meta)
        print("[svc] meta-after:", res.meta.user_id, res.meta.red_id, res.meta.note_total)
        print("[svc] notes =", len(res.notes), "| stopped:", res.stopped)
        types = {}
        for n in res.notes:
            types[n.kind] = types.get(n.kind, 0) + 1
        print("[svc] type count:", types)
        for n in res.notes[:8]:
            print("[svc]  ", n.note_id, n.kind_label, "|", n.title[:24], "| likes", n.liked)
    except LoginRequired as e:
        print("[svc] LOGIN_REQUIRED:", e)
        sys.exit(3)
    finally:
        if page is not None:
            page.quit()
    print("[svc] done")


if __name__ == "__main__":
    main()
