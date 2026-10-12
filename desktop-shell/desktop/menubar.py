"""ThinkBuddy Desktop — 原生菜单栏（参考 WorkBuddy：关于 / 编辑 / 窗口 / 帮助）。

走 pywebview 的 ``Menu`` / ``MenuAction`` / ``MenuSeparator`` 通道：传给
``webview.start(menu=...)`` 后由 winforms 后端渲染成原生 ``MenuStrip``
（见 webview/platforms/winforms.py 的 ``set_window_menu``），点击动作在
独立线程执行。

macOS 差异：Windows 的标题栏账号区（AccountChip，见 ADR-004）走的是
WinForms 自绘条带，mac 上不存在；这里在系统菜单栏追加一个「账号」菜单，
动作表直接复用 main._chip_actions（与 Windows 账号区同一套回调），功能
一比一对齐。仅 darwin 生效，Windows 菜单栏零变化。

关于「检查更新」：**真正的服务器检查更新** —— 拉取 ``latest.json`` 做
版本比对，有新版本则询问→下载（SHA256 校验）→静默安装（Inno
/VERYSILENT）→ 安装器自动重启新模式（见 docs/adr/ADR-006）。开发态
（非 frozen）与更新服务器不可达时降级为本地 mtime 探测（比较 exe 修改
时间与基线），保住旧版「重启生效」提示行为；服务器明确返回「已是最新」
时则稳定提示最新（并刷新 mtime 基线），不再走探测，避免「重装同版本
包」把 exe 覆盖误报成新版本。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.parse

import webview
from webview.menu import Menu, MenuAction, MenuSeparator

from desktop import APP_NAME, __version__, updater
from desktop.auth import config as auth_config
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


def _probe_local_overwrite() -> bool:
    """本地覆盖探测（旧「轻量探测」语义，降级兜底用）。

    exe 被安装器覆盖（mtime 明显变新）→ 提示重启生效并返回 True。
    仅打包版（PyInstaller frozen）可探测；开发态无覆盖，返回 False。
    """
    try:
        if getattr(sys, "frozen", False):
            exe = sys.executable
            mtime = os.path.getmtime(exe)
            baseline = _load_baseline()
            if baseline and mtime > baseline + 60:  # 允许 1 分钟误差
                _toast("发现新版本已就绪，重启 ThinkBuddy 后生效。")
                return True
            if not baseline:
                _save_baseline(mtime)
    except Exception:  # noqa: BLE001
        log.exception("local overwrite probe failed")
    return False


def _download_progress(received: int, total: int) -> None:
    if total > 0:
        _toast(f"正在下载更新… {min(99, round(received * 100 / total))}%")
    else:
        _toast(f"正在下载更新…（{received // 1024 // 1024} MB）")


def _update_prompt_text(manifest: dict, installed: str | None) -> str:
    lines = [f"发现新版本 v{manifest['version']}", ""]
    if installed:
        lines.append(f"当前版本：v{installed}")
    changelog = manifest.get("changelog")
    if changelog:
        lines += ["", "本次更新："] + changelog.splitlines()[:8]
    lines += ["", "是否立即下载并安装？"]
    return "\n".join(lines)


def _download_and_verify(manifest: dict):
    """下载到 ROOT/updates 并做 SHA256 校验；失败弹错误框并返回 None。"""
    from desktop import dialogs

    dest_dir = ROOT / "updates"
    filename = os.path.basename(urllib.parse.urlparse(manifest["url"]).path) \
        or "ThinkBuddySetup.exe"
    dest = dest_dir / filename
    try:
        updater.download(
            manifest["url"],
            dest,
            expected_sha256=manifest.get("sha256"),
            on_progress=_download_progress,
        )
    except updater.UpdateError as exc:
        log.warning("update download failed: %s", exc)
        dialogs.message_box("更新失败", str(exc), 0x10)  # MB_ICONERROR
        return None
    finally:
        updater.cleanup_updates_dir(dest_dir, keep=dest)
    return dest


def _check_updates(api) -> None:
    """真正的更新链路：服务器检查 → 比对 → 询问 → 下载校验 → 静默安装。

    开发态 / 无更新源 / 服务器不可达 → 降级为本地 mtime 探测（旧行为）。
    菜单回调本身跑在独立线程，这里的网络与模态对话框不会卡 UI。
    """
    from desktop import dialogs

    if not getattr(sys, "frozen", False):
        _toast(f"{APP_NAME} 已是最新版本 ✓（v{__version__}）")
        return

    try:
        base = auth_config.update_base(ROOT)
    except Exception:  # noqa: BLE001
        base = ""
    if not base:
        if not _probe_local_overwrite():
            _toast(f"{APP_NAME} 已是最新版本 ✓（v{__version__}）")
        return

    _toast("正在检查更新…")
    try:
        manifest = updater.check_latest(base)
    except updater.UpdateError as exc:
        log.warning("update check failed: %s", exc)
        if not _probe_local_overwrite():
            _toast("检查更新失败，请检查网络后重试。")
        return

    installed = updater.installed_version()
    if not updater.is_newer(manifest["version"], installed):
        # 服务器明确说没有更高的版本 → 稳定提示「已是最新」，不再走本地 mtime 兜底。
        # 原因：旧兜底在「用户重装同版本包后首查」场景会被误触发成
        #  「发现新版本已就绪」——那只说明 exe 被覆盖过，不代表服务器有新版本。
        # 顺手把 baseline 刷新为当前 exe 时间，消除陈旧基线带来的长期误报。
        if getattr(sys, "frozen", False):
            try:
                _save_baseline(os.path.getmtime(sys.executable))
            except Exception:  # noqa: BLE001  刷新失败不影响本次提示
                pass
        shown = installed or manifest["version"]
        _toast(f"{APP_NAME} 已是最新版本 ✓（v{shown}）")
        return

    # ---- 发现新版本：询问 → 下载校验 → 再确认 → 静默安装 ----
    # MB_OKCANCEL | MB_ICONQUESTION == 0x21；IDOK == 1
    if dialogs.message_box("发现新版本", _update_prompt_text(manifest, installed), 0x21) != 1:
        return

    dest = _download_and_verify(manifest)
    if dest is None:
        return

    if dialogs.message_box(
        "更新就绪",
        "更新包已下载并校验通过。\n"
        "点击「确定」将关闭 ThinkBuddy 并开始安装，安装完成后会自动重启进入新版。\n"
        "若未自动重启，请手动重新打开。",
        0x21,
    ) != 1:
        return

    _toast("正在安装更新…")
    try:
        updater.run_installer(dest)
    except updater.UpdateError as exc:
        dialogs.message_box("更新失败", str(exc), 0x10)  # MB_ICONERROR


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
            lines = ["已配置令牌", "本机已配置令牌，可通过「切换账号」登录"]
        elif st.get("in_progress"):
            lines = ["等待浏览器完成…", "请在浏览器中完成登录与授权"]
        else:
            lines = ["未登录 Tokengine", "通过「切换账号」发起登录"]
        dialogs.message_box("账号信息", "\n".join(lines))
    except Exception:  # noqa: BLE001  菜单线程里兜底，别让异常炸进菜单
        log.exception("account info failed")


# --------------------------------------------------------------------------- #
# macOS：菜单栏「账号」菜单标题动态跟随登录态 ------------------------------------ #
_ACCT_MENU_MARKER = "账号"  # 子菜单标题恒为「账号」，作为查找菜单条目的稳定标记


def _darwin_set_account_menu_title(title: str) -> None:
    try:
        import AppKit  # type: ignore
        from PyObjCTools import AppHelper  # type: ignore
    except Exception:  # noqa: BLE001  非 mac / 无 pyobjc 环境
        return

    def _apply() -> None:
        try:
            main_menu = AppKit.NSApplication.sharedApplication().mainMenu()
            if main_menu is None:
                return
            for i in range(main_menu.numberOfItems()):
                item = main_menu.itemAtIndex_(i)
                sub = item.submenu()
                if sub is not None and str(sub.title() or "") == _ACCT_MENU_MARKER:
                    if str(item.title() or "") != title:
                        item.setTitle_(title)
                    return
        except Exception:  # noqa: BLE001
            log.exception("update account menu title failed")

    AppHelper.callAfter(_apply)  # AppKit 的 UI 变更必须在主线程执行


class _DarwinAccountMenuTitle:
    """账号区模型的 mac 落点：把 chip 文案写到菜单栏「账号」菜单标题上。

    AccountStatusSync 只认 ``set_model()``；Windows 上是标题栏 AccountChip，
    mac 没有标题栏 chip，用菜单标题顶替——已登录显示用户名，
    未登录显示「登录」，文案与 Windows 账号区逐字一致。
    """

    def set_model(self, model: dict) -> None:
        label = str((model or {}).get("label") or "").strip()
        if label:
            _darwin_set_account_menu_title(label)


_DARWIN_TITLE_CHIP = _DarwinAccountMenuTitle()


def account_title_chip():
    """供 AccountStatusSync 的 chip_fn 使用：仅 darwin 返回菜单标题适配器。"""
    return _DARWIN_TITLE_CHIP if sys.platform == "darwin" else None


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
            MenuAction("邀请好友赢积分", guarded(actions["invite"])),
            MenuAction("签到得积分", guarded(actions["checkin"])),
            MenuAction("刷新可用模型", guarded(actions["refresh"])),
            MenuSeparator(),
            MenuAction("打开 Tokengine 平台", actions["platform"]),
            MenuAction("切换账号", actions["switch"]),
            MenuAction("退出登录", guarded(actions["logout"])),
            MenuSeparator(),
            MenuAction(f"关于 {APP_NAME}", actions["about"]),
        ],
    )
