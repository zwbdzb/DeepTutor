/**
 * systemConfirm — one yes/no question, asked by whichever host is showing the app.
 *
 * 桌面壳（ThinkBuddy）里，WebView2 自带的 `window.confirm` 会渲染成宿主
 * chrome：「127.0.0.1:3782 显示」标题 + 网页样式按钮，既没有品牌也不是桌面
 * 端观感。壳通过 `js_api` 暴露了 `pywebview.api.confirm(message)`，由它弹
 * 系统原生 MessageBox（标题固定 ThinkBuddy）。
 *
 * 所以二次确认统一走这里：
 *   * 在壳里 → 系统原生弹窗（确定 / 取消），与「退出登录」等壳内确认一致；
 *   * 浏览器直开（`deeptutor start` 后自己开 localhost）→ 回落 window.confirm，
 *     行为与改造前完全一致，不引入任何 React 状态。
 *
 * 注意：桥是异步的，调用点必须 await；没等到答案一律按「取消」处理——
 * 宁可不动数据，也不能把不确定当同意。
 */

type ShellApi = {
  confirm?: (message: string, title?: string) => Promise<unknown> | unknown;
};

declare global {
  interface Window {
    pywebview?: { api?: ShellApi };
  }
}

/** 页面是否跑在桌面壳里（pywebview 注入的桥存在且带 confirm）。 */
export function isShellHosted(): boolean {
  if (typeof window === "undefined") return false;
  return typeof window.pywebview?.api?.confirm === "function";
}

/**
 * 同一条文案的弹窗未关闭前复用同一个 Promise：系统弹窗**不阻塞** webview，
 * 连点/重入会叠出两个一模一样的框（还会拿到两个答案），这里按文案去重。
 */
const pending = new Map<string, Promise<boolean>>();

/** 问用户一次；返回 true 表示「确定」。 */
export function systemConfirm(message: string): Promise<boolean> {
  if (typeof window === "undefined") return Promise.resolve(false);

  const api = window.pywebview?.api;
  if (!api?.confirm) {
    // 浏览器直开：保持改造前的原生 confirm 行为。
    return Promise.resolve(window.confirm(message));
  }

  const existing = pending.get(message);
  if (existing) return existing;

  const call = (async () => {
    try {
      const answer = await api.confirm!(message);
      return typeof answer === "boolean" ? answer : false;
    } catch {
      // 桥断了（窗口销毁 / Python 侧异常）：退回浏览器原生 confirm 再问一次，
      // 宁可多问一次也不要默认「确定」。
      return window.confirm(message);
    } finally {
      pending.delete(message);
    }
  })();

  pending.set(message, call);
  return call;
}
