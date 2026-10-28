"""Headless 冒烟：账号头行 ⟳ 轻刷新链路（refresh_user_info）。

覆盖（2026-10-21 新交互，无需窗口）：
  R1 轻刷新成功：access_token 过期自动轮换 → userinfo 拉取 → 用户名/余额/
     手机号落库（余额总是覆盖、ai_token 不进 raw、业务令牌不动）；
  R2 status() 透出新数据（AccountStatusSync 轮询源）；
  R3 account_menu_model 头行带 refresh_icon + 余额两位小数格式化；
  R4 MENU_ACTIONS 注册 refresh_user；
  R5 失败路径：未登录 ok=False；userinfo 抛异常 ok=False + message。

运行：
    python tools/test_refresh_user_info.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    from desktop.auth.manager import AuthManager
    from desktop.titlebar_account import MENU_ACTIONS, account_menu_model

    tmp = Path(tempfile.mkdtemp(prefix="dt-refresh-user-"))
    am = AuthManager(root=tmp / "root", home=tmp / "home")

    # ---- R5a 未登录 -------------------------------------------------------
    res = am.refresh_user_info()
    check("R5a not logged in rejected", not res.get("ok")
          and res.get("error") == "not_logged_in", str(res))

    # ---- 落一份已登录 payload（旧用户名/旧余额/旧手机号）--------------------
    am._store.save({
        "token": "sk-business",
        "access_token": "at-expired",
        "refresh_token": "rt-1",
        "account": {
            "phone": "13800000000",
            "balance": "¥1.00 额度",
            "models": ["m1"],
            "raw": {"username": "oldname", "sub": "abc"},
        },
        "relay_base": "https://relay.example.com",
        "relay_source": "local-config",
    })

    class FakeClient:
        userinfo_url = "https://x/userinfo"
        authorize_url = "https://x/authorize"   # status() 引用
        token_url = "https://x/token"

        def userinfo(self, access: str) -> dict:
            assert access == "at-new", f"userinfo 应拿到轮换后的 access_token，实得 {access}"
            return {
                "username": "newname",
                "balance": "¥16.769472 额度",
                "phone": "13900000000",
                "ai_token": "sk-should-not-rotate",
                "sub": "abc",
            }

        def refresh(self, refresh_token: str, machine_id: str) -> dict:
            return {"access_token": "at-new", "refresh_token": "rt-2",
                    "expires_in": 3600}

    am._client = FakeClient()

    # ---- R1 轻刷新成功 -----------------------------------------------------
    res = am.refresh_user_info()
    check("R1a ok with username/balance", bool(res.get("ok"))
          and res.get("username") == "newname", str(res))
    payload = am._store.load()
    check("R1b access_token rotated", payload.get("access_token") == "at-new",
          str(payload.get("access_token")))
    acc = payload.get("account") or {}
    check("R1c balance overwritten", acc.get("balance") == "¥16.769472 额度",
          str(acc.get("balance")))
    check("R1d phone updated", acc.get("phone") == "13900000000",
          str(acc.get("phone")))
    raw = acc.get("raw") or {}
    check("R1e username into raw", raw.get("username") == "newname",
          str(raw.get("username")))
    check("R1f ai_token excluded from raw update",
          raw.get("ai_token") in (None, ""), str(raw.get("ai_token")))
    check("R1g business token untouched", payload.get("token") == "sk-business",
          str(payload.get("token")))

    # ---- R2 status() 透出 --------------------------------------------------
    st = am.status()
    check("R2 status exposes fresh username/balance",
          st["account"]["username"] == "newname"
          and st["account"]["balance"] == "¥16.769472 额度", str(st["account"]))

    # ---- R3 菜单模型 -------------------------------------------------------
    model = account_menu_model(st)
    hdr = model.get("header") or {}
    check("R3a header refresh_icon flag", hdr.get("refresh_icon") is True,
          str(hdr))
    check("R3b balance formatted 2dp", hdr.get("sub") == "余额 ¥16.77",
          str(hdr.get("sub")))

    # ---- R4 动作注册 -------------------------------------------------------
    check("R4 refresh_user in MENU_ACTIONS", "refresh_user" in MENU_ACTIONS,
          str(MENU_ACTIONS))

    # ---- R5b userinfo 抛异常 ----------------------------------------------
    class BoomClient(FakeClient):
        def userinfo(self, access: str) -> dict:
            raise RuntimeError("network down")

    am._client = BoomClient()
    res = am.refresh_user_info()
    check("R5b userinfo failure surfaces message",
          not res.get("ok") and res.get("error") == "fetch_failed"
          and "network down" in str(res.get("message")), str(res))

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} -> {FAILS}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
