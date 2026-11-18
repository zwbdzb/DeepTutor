import { afterEach, describe, expect, it, vi } from "vitest";

import { isShellHosted, systemConfirm } from "@/lib/system-confirm";

/**
 * 二次确认只有两个宿主行为：桌面壳（pywebview.api.confirm → 系统 MessageBox）
 * 与浏览器直开（window.confirm）。这里钉住两者之间的切换条件和失败兜底：
 * 拿不到明确答案时永远不能当成「确定」。
 */

function installBridge(
  confirm: (message: string, title?: string) => Promise<unknown> | unknown,
) {
  window.pywebview = { api: { confirm } };
}

afterEach(() => {
  delete window.pywebview;
  vi.restoreAllMocks();
});

describe("systemConfirm", () => {
  it("没有壳桥时回落 window.confirm", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    await expect(systemConfirm("删除这条对话？")).resolves.toBe(true);
    expect(confirmSpy).toHaveBeenCalledWith("删除这条对话？");
  });

  it("壳里走系统弹窗，不碰 window.confirm", async () => {
    const bridge = vi.fn().mockResolvedValue(true);
    installBridge(bridge);
    const confirmSpy = vi.spyOn(window, "confirm");

    await expect(systemConfirm("删除这条对话？")).resolves.toBe(true);
    expect(bridge).toHaveBeenCalledWith("删除这条对话？");
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("用户点取消就是 false", async () => {
    installBridge(vi.fn().mockResolvedValue(false));

    await expect(systemConfirm("删除这条对话？")).resolves.toBe(false);
  });

  it("桥给了非布尔值按取消处理", async () => {
    installBridge(vi.fn().mockResolvedValue(undefined));

    await expect(systemConfirm("删除这条对话？")).resolves.toBe(false);
  });

  it("桥调用失败时退回 window.confirm 再问一次", async () => {
    installBridge(vi.fn().mockRejectedValue(new Error("bridge gone")));
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    await expect(systemConfirm("删除这条对话？")).resolves.toBe(false);
    expect(confirmSpy).toHaveBeenCalledWith("删除这条对话？");
  });

  it("同一条文案弹窗未关闭前不叠第二个框", async () => {
    let answer: (value: boolean) => void = () => {};
    const bridge = vi.fn(
      () => new Promise<boolean>((resolve) => {
        answer = resolve;
      }),
    );
    installBridge(bridge);

    const first = systemConfirm("删除这条对话？");
    const second = systemConfirm("删除这条对话？");
    answer(true);

    await expect(first).resolves.toBe(true);
    await expect(second).resolves.toBe(true);
    expect(bridge).toHaveBeenCalledTimes(1);
  });
});

describe("isShellHosted", () => {
  it("只在桥到位时为真", () => {
    expect(isShellHosted()).toBe(false);
    installBridge(vi.fn());
    expect(isShellHosted()).toBe(true);
  });
});
