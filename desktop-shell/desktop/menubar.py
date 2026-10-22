"""ThinkBuddy Desktop — 原生菜单栏（参考 WorkBuddy：关于 / 编辑 / 窗口 / 帮助）。

走 pywebview 的 ``Menu`` / ``MenuAction`` / ``MenuSeparator`` 通道：传给
``webview.start(menu=...)`` 后由 winforms 后端渲染成原生 ``MenuStrip``
（见 webview/platforms/winforms.py 的 ``set_window_menu``），点击动作在
独立线程执行。

关于「检查更新」：**真正的服务器检查更新** —— 拉取 ``latest.json`` 做版本比对，有新版本则询问→下载（SHA256 校验）→静默安装（Inno
/VERYSILENT）→ 安装器自动重启新模式（见 docs/adr/ADR-006）。开发态
（非 frozen）与更新服务器不可达时降级为本地 mtime 探测（比较 exe 修改
时间与基线），保住旧版「重启生效」提示行为；服务器明确返回「已是最新」时则稳定提示最新（并刷新 mtime 基线），不再走探测，避免「重装同版本
包」把 exe 覆盖误报成新版本。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
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


def _toast(msg: str, kind: str = "info", duration_ms: int = 2000) -> None:
    """弹页面底部 toast（kind: 'info' | 'success' | 'error' 控制语义圆点色）。"""
    window = _main_window()
    if window is None:
        return
    try:
        window.evaluate_js(
            "window.__edubuddyToast && window.__edubuddyToast(%s, %d, %s)"
            % (
                json.dumps(str(msg), ensure_ascii=False),
                duration_ms,
                json.dumps(kind, ensure_ascii=False),
            )
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
            f"{APP_NAME} 桌面端：v{info['app_version']}",
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
                _toast("发现新版本已就绪，重启 ThinkBuddy 后生效。",
                       kind="info", duration_ms=4000)
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


def _check_updates(api, manual: bool = True) -> None:
    """真正的更新链路：服务器检查 → 比对 → 询问 → 下载校验 → 静默安装。

    开发态 / 无更新源 / 服务器不可达 → 降级为本地 mtime 探测（旧行为）。
    菜单回调本身跑在独立线程，这里的网络与模态对话框不会卡 UI。
    manual=False 为启动时自动检查：无更新 / 网络失败 / 开发态一律静默，
    不弹任何提示（含「已是最新」「检查失败」），仅发现新版本时弹询问框。
    """
    from desktop import dialogs

    if not getattr(sys, "frozen", False):
        if manual:
            _toast("已是最新版本", kind="success")
        return

    try:
        base = auth_config.update_base(ROOT)
    except Exception:  # noqa: BLE001
        base = ""
    if not base:
        if manual and not _probe_local_overwrite():
            _toast("已是最新版本", kind="success")
        return

    if manual:
        _toast("正在检查更新…")
    try:
        manifest = updater.check_latest(base)
    except updater.UpdateError as exc:
        log.warning("update check failed: %s", exc)
        if manual and not _probe_local_overwrite():
            _toast("检查更新失败，请检查网络后重试。",
                   kind="error", duration_ms=3500)
        return

    # 当前版本优先取正在运行的 exe 版本（与「关于」页同源）：更新是为了让
    # 运行中的程序变成最新，而注册表 DisplayVersion 只反映「最后一次通过
    # 安装器写入的版本」——直接运行新打包的免安装 exe 时两者会错位，导致
    # 关于页已是新版、检查更新却仍提示下载。
    installed = __version__ or updater.installed_version()
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
        if manual:
            _toast("已是最新版本", kind="success")
        return

    # ---- 发现新版本（双平台同流程：询问 → 下载校验 → 再确认 → 静默安装）----
    # mac 的静默安装由 updater._run_installer_darwin 编排（挂载 dmg → 覆盖
    # /Applications → 延迟重启）；更新检查走系统钥匙串信任链（见
    # updater._system_ssl_context），代理环境下也能正常完成。

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
        return
    # 安装器已放出去，本进程必须立刻自行退场：壳若还活着，Restart Manager
    # 关不掉 GUI 壳（2026-10-14 实测 RM 事件 10006「无法关闭
    # ThinkBuddyDesktop.exe」，python/node 都能正常关闭），Inno 静默模式
    # 会把被抑制的「无法关闭应用」提示按「取消」处理直接中止——表现为
    # app 已被强杀、新版没装上、也不会重启。走与关窗一致的优雅退出
    # （停后端进程树、释放单实例锁），安装器到场时已无进程可关，装完由
    # [Run] 段自动拉起新版。
    _graceful_exit_for_update(api)


def _graceful_exit_for_update(api) -> None:
    """更新安装前的自退：优先等价于用户关窗的完整优雅退出，超时硬退兜底。"""
    try:
        api.quit()
    except Exception:  # noqa: BLE001  跨线程销毁窗口失败则直接硬退
        log.exception("graceful quit for update failed; hard exit")
        os._exit(0)
    # 优雅退出链（webview 退出 → _on_closed → proc.stop → 释放单实例锁）
    # 正常几秒内结束进程；若 15s 后线程还在跑说明链路卡死，硬退兜底。遗留的
    # 后端进程交给安装器的 Restart Manager 关闭（实测 python/node 均可正常
    # 关闭），单实例锁是命名互斥体，随进程消亡由内核自动释放，无需清理。
    for _ in range(150):
        time.sleep(0.1)
    log.warning("graceful exit timed out; hard exit for update")
    os._exit(0)


# 启动自动检查的延迟：等窗口渲染、后端 bootstrap 与登录态就绪后再查，
# 避免弹窗抢在主窗口出现前盖住启动画面。
_AUTO_CHECK_DELAY_SECONDS = 8.0


def _auto_check_updates_on_startup(api) -> None:
    """启动后延迟静默检查一次更新（供 daemon 线程调用）。

    有新版本 → 与手动检查相同：弹「发现新版本」询问框，用户确认才下载。
    无更新 / 无更新源 / 网络失败 / 开发态 → 完全静默，不打扰用户。
    任何异常都不允许影响启动，这里整体兜底吞掉并记日志。
    """
    try:
        time.sleep(_AUTO_CHECK_DELAY_SECONDS)
        _check_updates(api, manual=False)
    except Exception:  # noqa: BLE001
        log.exception("startup auto update check failed")


# --------------------------------------------------------------------------- #
# macOS 应用菜单中文化 ---------------------------------------------------------- #
def localize_app_menu() -> None:
    """把 pywebview Cocoa 后端生成的英文应用菜单（第一项）替换为中文。

    必须在主线程调用（配合 AppHelper.callAfter）。动作选择器全部沿用
    系统标准（orderFrontStandardAboutPanel: / hide: / terminate: 等），
    仅替换标题文案；「服务」子菜单需 NSApp.setServicesMenu_ 挂回才能用。
    """
    if sys.platform != "darwin":
        return
    try:
        import AppKit

        app = AppKit.NSApplication.sharedApplication()
        main_menu = app.mainMenu()
        if main_menu is None or main_menu.numberOfItems() < 1:
            log.warning("app menu localize skipped: no main menu yet")
            return
        app_item = main_menu.itemAtIndex_(0)

        def _item(title: str, action: str | None, key: str = ""):
            it = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title,
                AppKit.NSSelectorFromString(action) if action else None,
                key,
            )
            return it

        sub = AppKit.NSMenu.alloc().initWithTitle_(app_item.title() or APP_NAME)
        sub.addItem_(_item(f"关于 {APP_NAME}", "orderFrontStandardAboutPanel:"))
        sub.addItem_(AppKit.NSMenuItem.separatorItem())

        services_item = _item("服务", None)
        services_menu = AppKit.NSMenu.alloc().initWithTitle_("服务")
        services_item.setSubmenu_(services_menu)
        sub.addItem_(services_item)
        app.setServicesMenu_(services_menu)

        sub.addItem_(AppKit.NSMenuItem.separatorItem())
        hide = _item(f"隐藏 {APP_NAME}", "hide:", "h")
        sub.addItem_(hide)
        hide_others = _item("隐藏其他", "hideOtherApplications:", "h")
        hide_others.setKeyEquivalentModifierMask_(
            AppKit.NSEventModifierFlagCommand | AppKit.NSEventModifierFlagOption
        )
        sub.addItem_(hide_others)
        sub.addItem_(_item("全部显示", "unhideAllApplications:"))
        sub.addItem_(AppKit.NSMenuItem.separatorItem())
        sub.addItem_(_item(f"退出 {APP_NAME}", "terminate:", "q"))

        app_item.setSubmenu_(sub)
        log.info("mac 应用菜单已中文化")
    except Exception:  # noqa: BLE001  菜单本地化失败不影响功能
        log.exception("app menu localize failed")


def _localize_app_menu_later() -> None:
    """后台线程等菜单建好后派发到主线程执行 localize_app_menu。"""
    time.sleep(3.0)
    try:
        from PyObjCTools import AppHelper  # type: ignore  (AppKit 不导出该名字)

        AppHelper.callAfter(localize_app_menu)
    except Exception:  # noqa: BLE001
        log.exception("app menu localize schedule failed")


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
# 帮助：打开官网 / 配置使用指南 / 日志目录 ---------------------------------------- #
def _open_website() -> None:
    """打开 ThinkBuddy 官网（文件菜单；URL 走 endpoints 覆盖链，默认官网首页）。"""
    try:
        import webbrowser

        from desktop.auth import config as auth_config

        url = auth_config.thinkbuddy_api_base(ROOT) or "https://thinkbuddy.hanyoai.com"
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        log.exception("open website failed")


def _open_docs() -> None:
    """打开 ThinkBuddy 配置使用指南（帮助菜单；官网域名 + ``/docs``）。"""
    try:
        import webbrowser

        from desktop.auth import config as auth_config

        base = auth_config.thinkbuddy_api_base(ROOT) or "https://thinkbuddy.hanyoai.com"
        webbrowser.open(f"{base}/docs")
    except Exception:  # noqa: BLE001
        log.exception("open docs failed")


def _open_log_dir() -> None:
    """在系统文件管理器中打开日志目录（帮助菜单）。"""
    try:
        log_dir = ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "darwin":
            import subprocess

            subprocess.Popen(["open", str(log_dir)])
        elif os.name == "nt":
            os.startfile(str(log_dir))  # noqa: S606  仅 Windows 可用
        else:
            import subprocess

            subprocess.Popen(["xdg-open", str(log_dir)])
    except Exception:  # noqa: BLE001
        log.exception("open log dir failed")


def build_native_menu(api) -> list[Menu]:
    """构造常规桌面菜单栏：文件 / 编辑 / 视图 / 帮助。

    ``api`` 是 main.Api 实例；闭包捕获它来提供关于信息 / 平台跳转。
    """
    return [
        Menu(
            "文件",
            [
                MenuAction("打开 ThinkBuddy 网站", lambda: _open_website()),
                MenuSeparator(),
                MenuAction("检查更新...", lambda: _check_updates(api)),
                MenuSeparator(),
                # 菜单文本不带快捷键提示：pywebview MenuAction 只有纯文本 label，
                # WinForms ToolStripMenuItem 不支持「\t快捷键」右对齐语法，会渲染成
                # 「退出 ThinkBuddyAlt+F4」粘连（用户实测）。Alt+F4 是系统级关窗
                # 快捷键，与菜单文本无关，去掉提示零功能损失；mac 端同位置菜单
                # （_item(f"退出 {APP_NAME}", "terminate:", "q")）本就是纯文本。
                MenuAction(f"退出 {APP_NAME}", _win_close),
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
        Menu(
            "帮助",
            [
                MenuAction("配置使用指南", lambda: _open_docs()),
                MenuAction("打开日志目录", lambda: _open_log_dir()),
                MenuSeparator(),
                MenuAction(f"关于 {APP_NAME}", lambda: _menu_about(api)),
            ],
        ),
    ]
