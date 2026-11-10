"""标题栏「签到+100积分」药丸的显示状态（纯逻辑 + 本地落盘，不碰 UI）。

为什么需要这份状态（而不是每次问平台「今天签到了吗」）：
    平台侧 ``/v1/checkin`` 只有「提交签到」一个动作，没有「今日是否已签到」
    的查询接口；每次启动都拿一次 409 来判断既慢又会在弱网下闪一下药丸。
    所以本地记一份**最少必要**的按用户 + 按自然日记录：

        {"version": 1,
         "users": {"u_314": {"date": "2026-11-05",
                             "checked_in": true,
                             "dismissed": false}}}

语义（与产品需求逐条对应）：
    * 记录日期不是今天  → 视为「今天还没签到」→ 显示药丸（跨天自动复活）；
    * 今天 checked_in   → 不显示（两条签到入口共享：药丸主体 / 账号菜单）；
    * 今天 dismissed    → 不显示（用户点 ✕ 收起，今天内不再打扰）；
    * 没有记录          → 显示。

落盘落在 ``<root>/checkin_pill.json``（``root`` 即 ``desktop.runtime.ROOT``，
Windows 上是 ``%LOCALAPPDATA%\\ThinkBuddy``），原子替换写入，读写全程不抛
异常——这份状态纯属「提示优化」，任何 IO 故障都只是让药丸多显示一次，绝不
能拦住启动或签到本身。文件里只存 user id 与日期布尔值，不含 token / 手机号
等敏感信息。
"""
from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path
from typing import Callable

log = logging.getLogger("dt.checkin_pill")


def _local_today() -> str:
    """本地自然日 ISO 文本（``YYYY-MM-DD``）。"""
    return datetime.date.today().isoformat()


class CheckinPillState:
    """按 (provider user id, 自然日) 记录签到药丸的显示状态。

    参数
      root  : 落盘目录（不存在时写入前自动创建）；
      today : 可注入的「今天」来源，默认本地自然日——测试用它模拟跨天。
    """

    FILE_NAME = "checkin_pill.json"

    def __init__(self, root: Path, today: Callable[[], str] | None = None) -> None:
        self._root = Path(root)
        self._path = self._root / self.FILE_NAME
        self._today = today or _local_today

    # ---- 对外 ---------------------------------------------------------- #
    def should_prompt(self, provider_user_id: str) -> bool:
        """今天是否还该向该用户显示签到药丸（无 uid 时永不显示）。"""
        uid = self._uid(provider_user_id)
        if not uid:
            return False
        record = self._records().get(uid) or {}
        if str(record.get("date") or "") != self._current_date():
            return True     # 没有今天的记录 = 今天还没签到
        if bool(record.get("checked_in")):
            return False
        if bool(record.get("dismissed")):
            return False
        return True

    def mark_checked_in(self, provider_user_id: str) -> None:
        """记下「今天已签到」，药丸从此刻起不再出现（跨天后重新可提示）。"""
        uid = self._uid(provider_user_id)
        if not uid:
            return
        self._write_record(uid, checked_in=True, dismissed=False)

    def mark_dismissed(self, provider_user_id: str) -> None:
        """记下「今天不想看到药丸（用户点了 ✕）」。

        今天已签到时收起动作没有意义（药丸本就不在），此时不动记录，避免
        把已签到状态冲成「只收起了、没签到」。
        """
        uid = self._uid(provider_user_id)
        if not uid:
            return
        record = self._records().get(uid) or {}
        if (str(record.get("date") or "") == self._current_date()
                and bool(record.get("checked_in"))):
            log.debug("签到药丸收起忽略：今天已签到")
            return
        self._write_record(uid, checked_in=False, dismissed=True)

    # ---- 内部 ---------------------------------------------------------- #
    def _current_date(self) -> str:
        """取「今天」；注入的 today 抛异常时退回本地自然日。"""
        try:
            return str(self._today() or "") or _local_today()
        except Exception:  # noqa: BLE001  时钟/注入异常不应影响 UI
            log.exception("读取今天日期失败，回退本地自然日")
            return _local_today()

    @staticmethod
    def _uid(provider_user_id: str) -> str:
        return str(provider_user_id or "").strip()

    def _records(self) -> dict:
        """当前全部用户记录（损坏/缺失 → 空表，绝不抛出）。"""
        users = self._load().get("users")
        if not isinstance(users, dict):
            return {}
        return {str(k): v for k, v in users.items() if isinstance(v, dict)}

    def _load(self) -> dict:
        """读磁盘；任何异常（缺失/损坏/无权限）都退化为空记录。"""
        try:
            if not self._path.exists():
                return {"version": 1, "users": {}}
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                log.warning("签到药丸状态文件格式异常，按空记录处理")
                return {"version": 1, "users": {}}
            users = data.get("users")
            if not isinstance(users, dict):
                users = {}
            return {"version": 1, "users": users}
        except Exception as exc:  # noqa: BLE001  损坏/无权限 → 视为空记录
            log.warning("签到药丸状态读取失败，按空记录处理：%s", exc)
            return {"version": 1, "users": {}}

    def _write_record(self, uid: str, *, checked_in: bool,
                      dismissed: bool) -> None:
        """写入该用户今天的记录（原子替换；失败只记日志）。"""
        payload = self._load()
        users = payload.get("users")
        if not isinstance(users, dict):
            users = {}
        users[uid] = {
            "date": self._current_date(),
            "checked_in": bool(checked_in),
            "dismissed": bool(dismissed),
        }
        self._save({"version": 1, "users": users})

    def _save(self, payload: dict) -> None:
        """落盘（tmp + replace，与 ``auth/store.py`` 的原子写法一致）。"""
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self._path)
        except Exception as exc:  # noqa: BLE001  落盘失败不影响签到与 UI
            log.warning("签到药丸状态写入失败：%s", exc)
