# ADR-007: 应用页二次确认改走壳的系统弹窗

## Status

**Accepted** — 2026-11-11。

- 决策者：ThinkBuddy 桌面端
- 相关：`desktop/dialogs.py`、`desktop/main.py` → `Api.confirm`、
  `web/lib/system-confirm.ts`、`web/tests/system-confirm.spec.ts`

---

## Context

DeepTutor 应用页里的危险动作（删除对话及其导师子对话等）用
`window.confirm()` 询问。这个 API 由**宿主 WebView2** 实现，桌面壳里渲染成：

```
127.0.0.1:3782 显示
永久删除此对话及其导师子对话？ 此操作无法撤销。        [确定] [取消]
```

问题有三层，逐层加深：

1. **品牌缺失**：标题是 URL（`127.0.0.1:3782 显示`），用户看到的是「一个
   网站」而不是 ThinkBuddy。壳的自定义标题栏（ADR-003/004）已经把窗口
   chrome 做成品牌化的，弹窗却把它捅破了。
2. **观感错位**：深色网页样式的按钮 + 右上角「显示」字样，是浏览器 chrome，
   不是 Windows 对话框。壳内其他确认（退出登录、关于）都是系统 MessageBox，
   同一个应用里出现两套确认样式。
3. **不受壳控制**：图标、按钮文案、默认按钮、是否可撤销提示，全在浏览器手里，
   后续想统一危险动作的视觉语言没有抓手。

## Decision

**把确认交回壳**：壳通过既有 `js_api` 桥暴露 `pywebview.api.confirm(message,
title?)`，用 `dialogs.message_box` 弹系统 MessageBox；页面侧统一走
`web/lib/system-confirm.ts` 的 `systemConfirm()`。

| 层 | 位置 | 行为 |
| --- | --- | --- |
| 壳 | `desktop/main.py` → `Api.confirm` | 标题缺省 `APP_NAME`（ThinkBuddy），空文案直接返回 False |
| 壳 | `desktop/dialogs.py` → `confirm` | `MB_OKCANCEL`（0x01）；`warning=True` 才叠加 `MB_ICONWARNING`（0x31） |
| 页 | `web/lib/system-confirm.ts` | 有桥 → 系统弹窗；无桥（浏览器直开）→ `window.confirm`，行为与改造前一致 |

**为什么不是别处**：

- **不用 WebView2 的 `ScriptDialogOpening` 拦截**：那是「一处改、全站生效」
  的优雅方案（所有 `window.confirm/alert` 自动品牌化），但要在 pywebview 的
  私有栈里摸到 `CoreWebView2`、在事件里跑嵌套消息循环再 `Accept/Cancel`，
  与 pywebview 版本强耦合，且 macOS 完全没有对应物。壳现有约定是
  「页面 ↔ 壳走 js_api / evaluate_js」（ADR-002 ~ 006 一致），不为一个弹窗
  破例。
- **不用页面自绘 modal**：`components/ui/ConfirmDialog.tsx` 已经是站内统一
  弹窗，但用户实测要的正是**系统**弹窗（与退出登录同观感）；且自绘 modal
  要每个调用点挂 React 状态，危险动作分散在 6+ 个组件里。
- **不改 `window.confirm` 本身**：同步签名无法返回异步桥的结果，任何
  monkeypatch 都只能伪造一个立即值，等于替用户点按钮。

## Consequences

**更容易的**：
- 桌面端所有「删除对话」入口（会话菜单、两个侧边栏、学习空间历史、导入
  对话、归档设置批量/单条）观感一致，标题固定 ThinkBuddy；
- 壳侧只有一个 20 行的桥方法，图标/默认按钮/文案策略将来集中在
  `dialogs.confirm` 一处调；
- 浏览器直开没有任何回归：没桥就退 `window.confirm`。

**更困难的 / 需要留意**：
- **桥是异步的**：所有调用点必须 `await`，同步 `onClick` 里的用法要包一层
  `void (async () => …)()` 或抽成具名 async 函数；
- **拿不到明确答案一律按取消**（`typeof answer === "boolean"` 才认），失败
  兜底再问一次 `window.confirm`——绝不能把「不确定」当「确定」；
- **弹窗不是 owner-modal**：`MessageBoxW(0, …)` 不 hold 住主窗口，网页仍可
  交互。页面侧按**文案去重**已开的 Promise，防连点叠框；要真正禁用窗口得
  传 owner HWND（pywebview 私有对象），当前收益不值这个耦合；
- **`MB_DEFBUTTON2` 未启用**：回车默认落在「确定」，与改造前的
  `window.confirm` 一致；若危险动作要「默认取消」，在 `dialogs.confirm`
  里按 `warning` 位分支加 0x100 即可；
- macOS 走 `osascript`，按钮文案是系统英文（`OK / Cancel`），与壳内其他
  弹窗同源，本次不单独处理。

## Verification

- `web/tests/system-confirm.spec.ts`：7 条用例覆盖桥存在/缺席/取消/非布尔/
  桥异常/同文案去重。
- 壳侧 `dialogs.confirm` 用打桩 `message_box` 验过图标位与按钮映射：
  默认 → `0x01` / `IDOK=1 → True`；`warning` → `0x31`；`IDCANCEL=2 → False`。
- **端到端**：`desktop-shell/tools/_probe_confirm_bridge.py` 起本地 http 服务，
  先 `html=` 出启动页再 `load_url` 导航（与壳进应用页同构），页面在
  `pywebviewready` 后调 `api.confirm`；Python 线程按标题 `ThinkBuddy` 找到
  `#32770` 窗口并按按钮。实测（pywebview 6.2.1 / Windows 11）：
  确定 → `RESULT:true:boolean`，取消 → `RESULT:false:boolean`。
  这条探针同时钉住了两个易翻车点：导航后的页面**确实**有 js_api 桥、返回值
  **确实**是严格 boolean。
