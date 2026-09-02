# -*- coding: utf-8 -*-
"""下载任务队列：任务模型 + 本地持久化（断点续传基础）

状态机：queued(等待) → running(进行中) → done / skipped / failed / cancelled(终态)

断点语义：任务目标目录按“作者/标题_笔记ID”确定性生成（download.build_target），
重跑任务时，已完成文件会被 download.has_existing() 命中而跳过、
半截文件由 download.remove_prefix() 清理后重下 —— 无需逐文件记账即可续传。

P2 的 DownloadQueue 调度器与 UI 任务面板都在此模型之上工作。
"""
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

from xhs_app import config
from xhs_app.models import NoteItem, ProfileMeta

# 任务状态
QUEUED = "queued"        # 等待开始
RUNNING = "running"      # 进行中
DONE = "done"            # 全部文件下载完成
SKIPPED = "skipped"      # 全部文件已存在，自动跳过
FAILED = "failed"        # 单篇失败（可重试/重下）
CANCELLED = "cancelled"  # 用户取消

_TERMINAL = {DONE, SKIPPED, FAILED, CANCELLED}
_SAVE_LOCK = threading.RLock()


@dataclass
class TaskItem:
    """一条下载任务 = 单篇笔记 + 目标作者上下文 + 目标目录"""
    note: NoteItem
    meta: ProfileMeta
    target_dir: str
    state: str = QUEUED
    msg: str = ""                 # 结果/错误描述
    added: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    finished: str = ""

    @property
    def key(self) -> str:
        return f"{self.meta.user_id}|{self.note.note_id}"

    @property
    def state_label(self) -> str:
        return {
            QUEUED: "等待",
            RUNNING: "下载中",
            DONE: "完成",
            SKIPPED: "已存在",
            FAILED: "失败",
            CANCELLED: "已取消",
        }.get(self.state, self.state)

    def terminal(self) -> bool:
        return self.state in _TERMINAL

    def to_dict(self) -> dict:
        return {
            "note": asdict(self.note),
            "meta": asdict(self.meta),
            "target_dir": self.target_dir,
            "state": self.state,
            "msg": self.msg,
            "added": self.added,
            "finished": self.finished,
        }

    @staticmethod
    def from_dict(d: dict) -> Optional["TaskItem"]:
        try:
            note = NoteItem(**(d.get("note") or {}))
            meta = ProfileMeta(**(d.get("meta") or {}))
        except Exception:
            return None
        return TaskItem(
            note=note,
            meta=meta,
            target_dir=d.get("target_dir") or "",
            state=d.get("state") or QUEUED,
            msg=d.get("msg") or "",
            added=d.get("added") or "",
            finished=d.get("finished") or "",
        )


def load_tasks() -> list:
    """启动时回读持久化任务（损坏数据跳过）"""
    try:
        data = json.loads(config.TASKS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    out = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        t = TaskItem.from_dict(d)
        if t is not None:
            out.append(t)
    return out


def save_tasks(tasks: list):
    """整体落盘（线程安全）。失败静默，不阻断主流程。"""
    with _SAVE_LOCK:
        try:
            payload = {"version": 1, "tasks": [t.to_dict() for t in tasks]}
            config.TASKS_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
