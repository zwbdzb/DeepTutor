# ADR-004: 账号入口迁移到原生标题栏

## Status

**Accepted** — 2026-09-23：取代 ADR-002 v2 的「页面内左下角按钮 + 自绘菜单」方案。

- 决策者：EduBuddy 桌面端
- 相关：`docs/adr/ADR-002-login-context-menu.md`、`desktop/native_menu_backend.py`、
  `desktop/titlebar_account.py`、`desktop/inject.py`

---

## Context

ADR-002 的方案把登录/账号按钮注入 DeepTutor 页面（`position:fixed` 悬浮在
左下角）。实际运行中暴露出两层问题：

1. **空间冲突**（用户反馈，2026-09-23）：悬浮按钮永远压在 DeepTutor 内容页
   上方，遮挡侧边栏底部（设置区）；页面自身的布局演变换不来它的让位。
2. **对抗性维护成本**：SPA 路由/整页刷新会重建 DOM，按钮与菜单靠 1s 定时器
   自愈；事件走「隐藏 input + evaluate_js 轮询」通道；登录态翻转要防空窗期
   误弹旧菜单。整套机制能工作，但每一环都在和页面抢地盘。

而壳自己已经有了一块**稳定的自绘标题栏**（ADR-003 之后的
`native_menu_backend.py`：MenuStrip 顶到第一排 + 右上角自绘窗口按钮）。
账号入口天然属于窗口 chrome——就像最小化按钮一样，不该住在网页里。

## Decision

- **账号区画在标题栏上**（窗口按钮左侧，同一套自绘哲学：矩形命中 +
  Paint 自绘，不走 ToolStrip 布局器）：
  - 未登录 → 显示「登录」，点击直接发起 OAuth（与旧按钮一致）；
  - 已登录 → 显示用户名（回退脱敏手机号），点击弹**原生 ContextMenuStrip**
    账号菜单：刷新可用模型 / 打开 Tokengine 平台 / 切换账号 / 退出登录 /
    关于 EduBuddy，顶部灰色只读项显示 用户名 · 余额 · 模型数；
  - 登录进行中 → 显示「等待浏览器…」，点击弹「重新发起登录」菜单；
  - 已配置令牌（本机残留）→ 显示「已配置令牌」，点击直发登录（configured
    不算登录，与旧版一致）。
- **拆除全部页面注入**：`inject.py` 里的按钮 ENSURE_JS、菜单 ENSURE_JS、
  事件槽轮询、按钮文案同步全部删除；仅保留 toast 浮层的周期 ensure
  （菜单动作反馈仍用页面轻提示）。
- **状态同步线程保留**：`titlebar_account.AccountStatusSync` 每 0.4s 轮询
  `AuthManager.status()`，模型变化才 marshal 到 UI 线程推送账号区；登录
  成功/账号身份变化的页面刷新逻辑原样保留（仅在应用页面上触发，门控页
  由会话主循环自己导航）。
- **退出登录确认方式变化**：旧版「再点一次确认」是自绘 HTML 的交互，原生
  菜单改用系统 MessageBox（确定/取消）。
- **危险项红色**、Esc/点击外部关闭等菜单行为由 WinForms 原生提供，不再自绘。

## Consequences

**更容易的**：
- 内容页 100% 归 DeepTutor，壳不再遮挡任何页面元素；
- 按钮不再随 SPA 刷新丢失——账号区活在窗口 chrome 里，页面刷新对它无感；
- 点击直达 Python 回调，事件槽轮询通道整体退役；
- 菜单渲染/悬停/关闭全是系统原生行为，删掉约 200 行自绘 JS/CSS。

**更困难的 / 需要留意**：
- 账号区是 WinForms 自绘，非 Windows 平台无此实现（壳本来就只面向 Windows）；
- 原生菜单没有「再点一次」的两段式确认，退出登录改走 MessageBox（交互
  习惯变化，需在发布说明里提一句）；
- 账号区宽度随文案变化，条带右缩进必须动态让位（`extra_right_padding`），
  与窗口按钮的 `sync()` 共享同一套让位计算，改 padding 公式时两处要同步。

## Implementation

| 组件 | 文件 |
| --- | --- |
| 账号区自绘与命中 | `desktop/native_menu_backend.py` → `AccountChip` |
| 状态模型 / 同步线程 | `desktop/titlebar_account.py` |
| 动作分发（登录/刷新/退出/关于…） | `desktop/main.py` → `_chip_actions` |
| toast 残留通道 | `desktop/inject.py` → `ToastInjector` |

## 交互打磨（2026-09-24）：下拉圆角 + 点外部收回

用户对菜单栏提出两点体验要求：① 下拉打开后点击页面其他地方要收回；
② 菜单与下拉的直角换圆角。实现与验证记录如下。

### 下拉圆角（双路径）

| 路径 | 条件 | 机制 |
| --- | --- | --- |
| DWM | Windows 11 | `DwmSetWindowAttribute(hwnd, 33, DWMWCP_ROUND=2)`，系统出原生圆角 + 投影 |
| Region | Windows 10 | `GraphicsPath` 四角圆弧裁剪 `Region`；必须先 `DropShadowEnabled=False`，否则方形阴影从四角外露 |

模式字符串写入 `dd.Tag`（`"dwm"` / `"region"`），由 Opened 事件里的
`_apply_dropdown_shape` 统一挂接（菜单下拉与账号区下拉同一入口）。

**菜单项悬停保持原生方形高亮**——这是决策不是妥协：面板圆角已由 DWM/Region
达成；而自绘悬停药丸需要继承 ToolStripProfessionalRenderer，本机
pythonnet 3.1.0 的 .NET 子类化**完全失效**（探针实证：`class C(Control)` 的
CLR 运行时类型就是基类本身，虚方法重写永不触发），VS Code / Chrome 的原生
菜单同样是方形悬停，符合惯例。

### 点外部收回（三层防御）

1. **UI 线程 Timer 看门狗**（`_install_outside_click_filter`，60ms）：本想用
   `Application.AddMessageFilter`，但 pythonnet 3.1.0 无法实现 .NET 接口
   （`TypeError: interface takes exactly one argument`），改用 Timer 轮询
   `GetAsyncKeyState(VK_LBUTTON)`——0x8000 位（此刻按着）+ 0x0001 位（间隔
   内按过），再快的点击也不漏；配合全局 `GetCursorPos` 判定点击点，**物理
   点击即使被 WebView2/悬浮层吞掉也能感知**（光标位置是全局的）。豁免两处
   避免双击竞争：打开中的菜单项标题矩形（原生 toggle = 收起）、账号区矩形
   （chip 自身 toggle）。
2. **WebView2 获焦钩子**（`_attach_webview_focus_hook`）：点进页面触发
   GotFocus → 收起所有下拉，覆盖 WebView2 输入自管通路。
3. **chip toggle 守卫**：ContextMenuStrip 的外部点击关闭发生在 MouseUp 之前，
   「再点一下账号区」若不设防会立刻重开。`_closed_at` 时间戳 + 0.45s 守卫
   窗口保证「开着再点 = 收起」语义稳定。

### 连带修复：caption_hit 无限自递归

组合「窗口按钮命中」与「账号区命中」时写成
`caption_hit = lambda x, y: caption_hit(x, y) or chip.hit(x, y)`——lambda 内
名字解析到它自己，鼠标划过标题栏（WM_NCHITTEST）即打满递归栈。已修为**先
把原函数捕获到独立名字再组合**。此 bug 之前被掩盖：测试窗被宿主悬浮层盖住
时收不到 NCHITTEST，z 序抢到后才爆发。

### 验证

- `tools/test_menu_polish_headed.py`：13 项断言全过——看门狗安装、真实点击
  打开/收回（含被悬浮层吞掉点击的场景）、圆角模式 `dwm`、chip toggle 三段
  （收起 → 守卫期内不重开 → 守卫过期可再开）、看门狗收合函数的豁免区
  （下拉内部 / chip 上不误收）。
- `tools/test_login_gate.py`：48 项回归全过；`tools/_verify_fixes.py`：13 项
  快检全过。
- 像素截图在 WorkBuddy 宿主环境不可靠（置顶悬浮层污染），主证据走程序化
  断言；真机手感请启动 dev 壳人眼验收。

## 显示时序（2026-09-24 下午）：账号区只在应用页亮出

用户反馈：splash「正在启动本地服务…」阶段右上角就出现「登录」，不符合
预期——登录入口应该在门控页页面中间，标题栏只在**已登录进入应用**后
显示用户名。

### 状态机

| 阶段 | 账号区 | 理由 |
| --- | --- | --- |
| splash（启动本地服务中） | **隐藏** | 启动期没有账号概念；此时显示「登录」是时序错误 |
| 登录门控页（未登录/已配置令牌/登录进行中） | **隐藏** | 登录入口与进度反馈由门控页页面承担（页面中间按钮）；标题栏不再放第二个入口，避免语义混淆 |
| 应用页（已登录） | **显示用户名** | 标题栏 = 身份指示器 + 账号操作菜单（刷新模型/切换账号/退出…） |

### 实现

- `AccountChip` 新增 `_hidden` 形态（初始即隐藏）：隐藏时不绘制、不命中、
  宽度归零（`extra_right_padding` 联动归零，右侧让位只剩窗口按钮）；
  `set_hidden()` 跨线程安全（BeginInvoke marshal，与 `set_model` 同模式），
  隐藏时顺手收掉还开着的下拉。
- 驱动点在会话循环（`main.run_session`）的两个导航动作：`load_url(应用)`
  前 `set_account_chip_hidden(False)`；注销 `load_html(门控页)` 前
  `set_account_chip_hidden(True)`。`AccountStatusSync` 只推模型不碰可见性，
  两者正交。
- `set_model` 与 `_hidden` 解耦：同步线程的模型推送不改变可见性，避免
  splash 阶段的首次推送把账号区「推亮」。

### 两套菜单样式统一（同日）

用户要求菜单栏下拉（文件/编辑/视图/帮助）与账号下拉交互、样式一致。
探针实证（`tools/_probe_menubar_style.py`）：字体（YaHei UI 9pt，下拉继承
strip）、圆角（同一 `_apply_dropdown_shape`）、外部收回（同一看门狗）、
悬停高亮（原生）均已一致；唯一差异是菜单栏下拉的 `ShowImageMargin=True`
（左侧约 20px 空白图列）——已关，与账号下拉的紧凑布局对齐。

### 验证

- `test_menu_polish_headed.py` 新增 A0.5a-d：初始隐藏、进应用后可见、
  菜单栏下拉无图列、下拉字体与 chip 一致；连同原 13 项共 **17 项全过**。
- `test_login_gate.py` 48 项、`_verify_fixes.py` 13 项回归全过。
