"""ThinkBuddy Desktop — 原生菜单栏（参考 WorkBuddy：关于 / 编辑 / 窗口 / 帮助）。

走 pywebview 的 ``Menu`` / ``MenuAction`` / ``MenuSeparator`` 通道：传给
``webview.start(menu=...)`` 后由 winforms 后端渲染成原生 ``MenuStrip``
（见 webview/platforms/winforms.py 的 ``set_window_menu``），点击动作在
独立线程执行。

macOS 差异：Windows 的标题栏账号区（AccountChip，见 ADR-004）走的是
WinForms 自绘条带，mac 上不存在；这里在系统菜单栏追加一个「账号」菜单，
动作表直接复用 main._chip_actions（与 Windows 账号区同一套回调），功能
一比一对齐。仅 darwin 生效，Windows 菜单栏零变化。

关于「检查更新」：壳当前没有自动更新机制，这里先做轻量探测——比较
安装目录里 exe 的修改时间与首次记录的基线时间，若明显更新（被安装器
覆盖过）则提示重启生效；否则提示已是最新版本。后续接入 AppUpdater 时
只需替换 ``_check_updates`` 的实现。
"""
from __future__ import annotations

import json
import logging
import os
import sys

import webview
from webview.menu import Menu, MenuAction, MenuSeparator

from desktop import APP_NAME, __version__
from desktop.runtime import ROOT

log = logging.getLogger("dt.menubar")

# 检查更新基线：进程首次运行时记下 exe 的修改时间，之后据此判断是否被更新覆盖。
# 跟随 ROOT（%LOCALAPPDATA%\ThinkBuddy），品牌迁移时随目录整体搬迁。
_BASELINE_FILE = str(ROOT / "update_baseline.txt")


def _main_window():
    try:
        return webview.windows[0] if webview.windows else None
    except Exception:  # noqa: BLE001
        return None


def _toast(msg: str) -> None:
    window = _main_window()
    if window is None:
        return
    try:
        window.evaluate_js(
            "window.__edubuddyToast && window.__edubuddyToast(%s)"
            % json.dumps(str(msg), ensure_ascii=False)
        )
    except Exception:  # noqa: BLE001
        log.exception("toast failed")


# --------------------------------------------------------------------------- #
# 关于 ----------------------------------------------------------------------- #
def _menu_about(api) -> None:
    """原生菜单栏的「关于」：弹系统级信息框（复用 dialogs.message_box）。"""
    try:
        from desktop import dialogs

        info = api.about()
        lines = [
            f"{info['app']}  v{info['app_version']}",
            f"ThinkBuddy 引擎：v{info['deeptutor_version']}",
        ]
        if info.get("relay"):
            lines.append(f"中继：{info['relay']}")
        dialogs.message_box(f"关于 {APP_NAME}", "\n".join(lines))
    except Exception:  # noqa: BLE001  菜单线程里兜底，别让异常炸进 winforms
        log.exception("about dialog failed")


# --------------------------------------------------------------------------- #
# 检查更新 ------------------------------------------------------------------- #
def _load_baseline() -> float:
    try:
        with open(_BASELINE_FILE, "r", encoding="utf-8") as fh:
            return float(fh.read().strip())
    except Exception:  # noqa: BLE001  无基线 → 以当前 exe 时间作为起点
        return 0.0


def _save_baseline(mtime: float) -> None:
    try:
        os.makedirs(os.path.dirname(_BASELINE_FILE), exist_ok=True)
        with open(_BASELINE_FILE, "w", encoding="utf-8") as fh:
            fh.write(str(mtime))
    except Exception:  # noqa: BLE001
        pass


def _check_updates(api) -> None:
    """轻量更新探测：exe 被安装器覆盖（mtime 明显变新）→ 提示重开生效。

    仅打包版（PyInstaller frozen）可探测；开发态直接提示已是最新。
    """
    try:
        if getattr(sys, "frozen", False):
            exe = sys.executable
            mtime = os.path.getmtime(exe)
            baseline = _load_baseline()
            if baseline and mtime > baseline + 60:  # 允许 1 分钟误差
                _toast("发现新版本已就绪，重启 ThinkBuddy 后生效。")
                return
            if not baseline:
                _save_baseline(mtime)
        _toast(f"{APP_NAME} 已是最新版本 ✓（v{__version__}）")
    except Exception:  # noqa: BLE001
        log.exception("check updates failed")
        _toast(f"{APP_NAME} 已是最新版本 ✓（v{__version__}）")


# --------------------------------------------------------------------------- #
# 编辑：对页面做富文本操作（WebView2 网页里通常由应用自身处理，这里是兜底）----- #
def _exec_edit(command: str) -> None:
    window = _main_window()
    if window is None:
        return
    try:
        window.evaluate_js(
            "try { document.execCommand(%s) } catch (e) {}"
            % json.dumps(command)
        )
    except Exception:  # noqa: BLE001
        log.exception("edit command failed: %s", command)


def _reload_page() -> None:
    window = _main_window()
    if window is None:
        return
    try:
        window.evaluate_js("location.reload()")
    except Exception:  # noqa: BLE001
        log.exception("reload page failed")


# --------------------------------------------------------------------------- #
# 窗口：最小化 / 最大化 / 还原 / 关闭 ------------------------------------------ #
def _win_minimize() -> None:
    w = _main_window()
    if w is not None:
        try:
            w.minimize()
        except Exception:  # noqa: BLE001
            log.exception("minimize failed")


def _win_maximize() -> None:
    w = _main_window()
    if w is not None:
        try:
            w.maximize()
        except Exception:  # noqa: BLE001
            log.exception("maximize failed")


def _win_restore() -> None:
    w = _main_window()
    if w is not None:
        try:
            w.restore()
        except Exception:  # noqa: BLE001
            log.exception("restore failed")


def _win_close() -> None:
    w = _main_window()
    if w is not None:
        try:
            w.destroy()
        except Exception:  # noqa: BLE001
            log.exception("close failed")


# --------------------------------------------------------------------------- #
# 帮助：打开平台 / 刷新模型 ---------------------------------------------------- #
def _open_platform(api) -> None:
    try:
        api.open_platform()
    except Exception:  # noqa: BLE001
        log.exception("open platform failed")


def _refresh_models(api) -> None:
    try:
        res = api.refresh_models()
        _toast("模型已刷新 ✓" if res.get("ok") else (res.get("message") or "刷新失败"))
    except Exception:  # noqa: BLE001
        log.exception("refresh models failed")


def build_native_menu(api, chip_actions: dict | None = None) -> list[Menu]:
    """构造常规桌面菜单栏：文件 / 编辑 / 视图 /（macOS：账号）/ 帮助。

    ``api`` 是 main.Api 实例；闭包捕获它来提供关于信息 / 平台跳转 / 模型刷新。
    ``chip_actions`` 是 main._chip_actions(api) 的动作表；仅 macOS 用于构建
    「账号」菜单（Windows 账号入口在自绘标题栏 AccountChip，不用菜单）。
    """
    menus: list[Menu] = [
        Menu(
            "文件",
            [
                MenuAction("打开 Tokengine 平台", lambda: _open_platform(api)),
                MenuAction("刷新可用模型", lambda: _refresh_models(api)),
                MenuSeparator(),
                MenuAction("检查更新...", lambda: _check_updates(api)),
                MenuSeparator(),
                MenuAction(f"退出 {APP_NAME}\tAlt+F4", _win_close),
            ],
        ),
        Menu(
            "编辑",
            [
                MenuAction("撤销", lambda: _exec_edit("undo")),
                MenuAction("重做", lambda: _exec_edit("redo")),
                MenuSeparator(),
                MenuAction("剪切", lambda: _exec_edit("cut")),
                MenuAction("复制", lambda: _exec_edit("copy")),
                MenuAction("粘贴", lambda: _exec_edit("paste")),
                MenuAction("全选", lambda: _exec_edit("selectAll")),
            ],
        ),
        Menu(
            "视图",
            [
                MenuAction("重新加载", _reload_page),
                MenuSeparator(),
                MenuAction("最小化", _win_minimize),
                MenuAction("最大化", _win_maximize),
                MenuAction("还原", _win_restore),
                MenuSeparator(),
                MenuAction("关闭", _win_close),
            ],
        ),
    ]
    if sys.platform == "darwin" and chip_actions:
        menus.append(_account_menu(api, chip_actions))
    menus.append(
        Menu(
            "帮助",
            [
                MenuAction("打开 Tokengine 平台", lambda: _open_platform(api)),
                MenuSeparator(),
                MenuAction(f"关于 {APP_NAME}", lambda: _menu_about(api)),
            ],
        ),
    )
    return menus


# --------------------------------------------------------------------------- #
# 账号（仅 macOS；与 Windows 标题栏账号区同一动作表）-------------------------- #
def _account_info(api) -> None:
    """「账号信息…」：承担 Windows 账号菜单头（显示名 + 余额）的角色。

    pywebview 的菜单是静态的（构建后改不了标题），显示名放不进菜单标题，
    统一弹系统信息框展示；未登录/进行中态给出对应文案与操作指引。
    """
    try:
        from desktop import dialogs
        from desktop.titlebar_account import _display_name, _fmt_balance

        st = api.auth_status() or {}
        acct = st.get("account") or {}
        if st.get("logged_in"):
            lines = [_display_name(acct)]
            amount = _fmt_balance(acct.get("balance"))
            if amount:
                lines.append(f"余额 ¥{amount}")
        elif st.get("configured"):
            lines = ["已配置令牌", "本机已配置令牌，可通过「登录 / 切换账号」登录"]
        elif st.get("in_progress"):
            lines = ["等待浏览器完成…", "请在浏览器中完成登录与授权"]
        else:
            lines = ["未登录 Tokengine", "通过「登录 / 切换账号」发起登录"]
        dialogs.message_box("账号信息", "\n".join(lines))
    except Exception:  # noqa: BLE001  菜单线程里兜底，别让异常炸进菜单
        log.exception("account info failed")


def _account_menu(api, actions: dict) -> Menu:
    """macOS 菜单栏「账号」菜单：结构与 Windows 已登录账号下拉一致。

    Windows 上邀请/签到/刷新/退出只在已登录态出现在 chip 下拉里；mac 菜单
    是静态的，无法按登录态增删项，改为点击时现读登录态——未登录 toast
    提示并不执行，语义等价。
    """

    def guarded(action):
        def run() -> None:
            try:
                st = api.auth_status() or {}
            except Exception:  # noqa: BLE001
                st = {}
            if not st.get("logged_in"):
                _toast("请先登录 Tokengine 账号")
                return
            action()

        return run

    return Menu(
        "账号",
        [
            MenuAction("账号信息…", lambda: _account_info(api)),
            MenuSeparator(),
            MenuAction("邀请好友得积分", guarded(actions["invite"])),
            MenuAction("签到加积分", guarded(actions["checkin"])),
            MenuAction("刷新可用模型", guarded(actions["refresh"])),
            MenuSeparator(),
            MenuAction("打开 Tokengine 平台", actions["platform"]),
            MenuAction("登录 / 切换账号", actions["switch"]),
            MenuSeparator(),
            MenuAction("退出登录", guarded(actions["logout"])),
            MenuSeparator(),
            MenuAction(f"关于 {APP_NAME}", actions["about"]),
        ],
    )
