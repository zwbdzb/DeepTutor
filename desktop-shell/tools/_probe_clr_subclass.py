"""探针：pythonnet 3.1.0 的 CLR 子类化是否可用（决定悬停样式修复路线）。

背景（2026-09-24 日志）：曾用 ``class C(Control): pass`` 探针得出「.NET 子类化
完全失效（运行时类型=基类，虚方法重写永不触发）」的结论，据此放弃了
ToolStripProfessionalRenderer 子类化自绘悬停药丸的方案，菜单悬停保持默认
蓝色方块。但用户 2026-09-28 明确要求统一悬停样式（菜单蓝方块 vs 账号 chip
灰药丸不一致），需要重验该结论——旧探针只测过 Control，未测过纯数据类
（ProfessionalColorTable）与渲染器（ToolStripProfessionalRenderer）。

本探针全部无头（不开窗口），三级证据：

  P1  ColorTable 属性重写：子类化 ProfessionalColorTable，重写
      MenuItemSelectedGradientBegin → 直接读 / 经 renderer.ColorTable 读 /
      反射（CLR 虚分派）读，三路比对返回色。
  P2  渲染器方法重写：子类化 ToolStripProfessionalRenderer，重写
      OnRenderMenuItemBackground → 经反射调用（CLR 虚分派），命中即证明
      子类化+虚方法重写在 CLR 侧真实生效（Python 侧直调不算数）。
  P3  GetType().FullName：子类实例的 CLR 运行时类型名——若与基类同名，
      即坐实旧结论；若是 Python 子类名，则旧结论仅对 Control 成立。

运行（无窗口，~2 秒）：
    .venv\\Scripts\\python tools\\_probe_clr_subclass.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import clr  # noqa: E402  激活 pythonnet / System 命名空间

# 与真实链路一致：先加载 winforms 平台模块，System.* 命名空间才被激活
import webview.platforms.winforms  # noqa: F401,E402

from System.Drawing import Bitmap, Color, Graphics  # noqa: E402
from System.Reflection import BindingFlags  # noqa: E402
from System.Windows.Forms import (  # noqa: E402
    ProfessionalColorTable,
    ToolStripItemRenderEventArgs,
    ToolStripMenuItem,
    ToolStripProfessionalRenderer,
)

GRAY = Color.FromArgb(0xE9, 0xE9, 0xE9)
_DEFAULT_ARGB = 0xFFC1D2EE  # 默认 professional MenuItemSelectedGradientBegin

HITS = {"prop": 0, "method": 0}


def argb(c) -> str:
    try:
        return f"0x{int(c.ToArgb()) & 0xFFFFFFFF:08X}"
    except Exception as exc:  # noqa: BLE001
        return f"<{exc!r}>"


class CT(ProfessionalColorTable):
    """P1：重写一个纯数据属性。"""

    @property
    def MenuItemSelectedGradientBegin(self):
        HITS["prop"] += 1
        return GRAY


class R(ToolStripProfessionalRenderer):
    """P2：重写一个渲染虚方法。"""

    def OnRenderMenuItemBackground(self, e):
        HITS["method"] += 1
        print(f"    [override ran] item={e.Item}")


def refl(obj, member: str, *args):
    """经 CLR 反射调用成员——强制走 CLR 虚分派，绕开 Python 属性直读。"""
    flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic
    tp = obj.GetType()
    if args:
        mi = tp.GetMethod(member, flags)
        return mi.Invoke(obj, args)
    pi = tp.GetProperty(member)
    return pi.GetValue(obj)


def main() -> int:
    import importlib.metadata

    print(f"pythonnet: {importlib.metadata.version('pythonnet')}")
    print(f"expect override color = {argb(GRAY)}, default = 0x{_DEFAULT_ARGB:08X}")
    print()

    # ---- P1: ColorTable 属性 -------------------------------------------------
    ct = CT()
    direct = ct.MenuItemSelectedGradientBegin
    print(f"P1a direct python read      : {argb(direct)}   hits={HITS['prop']}")

    rend = ToolStripProfessionalRenderer(ct)
    via_renderer = rend.ColorTable.MenuItemSelectedGradientBegin
    print(f"P1b via renderer.ColorTable : {argb(via_renderer)}   hits={HITS['prop']}")

    refl_val = refl(ct, "MenuItemSelectedGradientBegin")
    print(f"P1c reflection (CLR vtable) : {argb(refl_val)}   hits={HITS['prop']}")

    ok_p1 = (
        argb(refl_val) == argb(GRAY)
        and argb(via_renderer) == argb(GRAY)
    )
    print(f"P1 verdict: {'SUBCLASS WORKS' if ok_p1 else 'BROKEN'}")
    print()

    # ---- P2: 渲染器方法重写（反射调用 = CLR 虚分派）----------------------------
    r2 = R()
    item = ToolStripMenuItem("测试项")
    bmp = Bitmap(80, 24)
    g = Graphics.FromImage(bmp)
    args = ToolStripItemRenderEventArgs(g, item)
    refl(r2, "OnRenderMenuItemBackground", args)
    print(f"P2 reflection-invoked override: hits={HITS['method']}")
    ok_p2 = HITS["method"] >= 1
    print(f"P2 verdict: {'SUBCLASS WORKS' if ok_p2 else 'BROKEN'}")
    print()

    # ---- P3: 运行时类型名 -----------------------------------------------------
    print(f"P3 CT GetType().FullName = {ct.GetType().FullName}")
    print(f"P3 R  GetType().FullName = {r2.GetType().FullName}")

    verdict = ok_p1 and ok_p2
    print()
    print(f"FINAL: {'CLR subclassing WORKS — renderer route viable' if verdict else 'CONFIRMED BROKEN — fallback to Paint-event overpaint'}")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
