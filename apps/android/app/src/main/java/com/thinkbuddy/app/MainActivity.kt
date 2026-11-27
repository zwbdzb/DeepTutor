package com.thinkbuddy.app

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.webkit.ConsoleMessage
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebChromeClient.FileChooserParams
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.HorizontalScrollView
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.RelativeLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.google.android.material.tabs.TabLayout
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.launch
import java.io.File

class MainActivity : AppCompatActivity() {

    private var webView: WebView? = null
    private lateinit var rootContainer: FrameLayout
    private lateinit var launchScreen: LinearLayout
    private lateinit var mainScreen: FrameLayout
    private lateinit var tabLayout: TabLayout
    private lateinit var contentContainer: FrameLayout
    private var loadedUrl: String? = null
    private var fileChooserCallback: ValueCallback<Array<Uri>>? = null
    private var tokengineLoggedIn = false

    private val fileChooser = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        val callback = fileChooserCallback ?: return@registerForActivityResult
        fileChooserCallback = null
        val data = result.data
        if (result.resultCode != RESULT_OK || data == null) {
            callback.onReceiveValue(null)
            return@registerForActivityResult
        }
        val clip = data.clipData
        val uris = if (clip != null && clip.itemCount > 0) {
            Array(clip.itemCount) { clip.getItemAt(it).uri }
        } else {
            listOfNotNull(data.data).toTypedArray()
        }
        callback.onReceiveValue(uris)
    }

    private val cameraLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        if (result.resultCode == RESULT_OK) {
            toast("图片已获取，可在对话中上传")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.setFlags(
            WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
        )
        buildUi()
        requestPermissions()
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                val wv = webView
                if (wv != null && wv.canGoBack()) {
                    wv.goBack()
                } else if (mainScreen.visibility == View.VISIBLE && tabLayout.selectedTabPosition > 0) {
                    tabLayout.selectTab(tabLayout.getTabAt(0))
                } else {
                    finish()
                }
            }
        })
        checkTokengineLogin()
        observeEngine()
    }

    override fun onDestroy() {
        webView?.destroy()
        super.onDestroy()
    }

    // ── UI 构建 ──────────────────────────────────────────────

    @SuppressLint("SetJavaScriptEnabled")
    private fun buildUi() {
        rootContainer = FrameLayout(this)
        rootContainer.setBackgroundColor(Color.parseColor("#F8FAFC"))

        buildLaunchScreen()
        buildMainScreen()

        rootContainer.addView(launchScreen, matchParent())
        rootContainer.addView(mainScreen, matchParent())
        mainScreen.visibility = View.GONE

        setContentView(rootContainer)
    }

    /** 启动引导屏：Tokengine 登录 → 运行时安装 → 引擎启动状态 */
    @SuppressLint("SetJavaScriptEnabled")
    private fun buildLaunchScreen() {
        launchScreen = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundDrawable(gradientBg())
            setPadding(dp(32), dp(48), dp(32), dp(32))
            gravity = Gravity.CENTER_HORIZONTAL
        }

        // Logo / 图标区
        val logoContainer = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setPadding(0, dp(40), 0, dp(32))
        }
        val logo = ImageView(this).apply {
            setImageResource(R.drawable.ic_launcher)
            layoutParams = LinearLayout.LayoutParams(dp(96), dp(96))
        }
        val title = TextView(this).apply {
            text = getString(R.string.welcome_title)
            textSize = 32f
            setTextColor(Color.parseColor("#0F172A"))
            gravity = Gravity.CENTER
            setPadding(0, dp(16), 0, dp(4))
            typeface = android.graphics.Typeface.create(typeface, android.graphics.Typeface.BOLD)
        }
        val subtitle = TextView(this).apply {
            text = getString(R.string.welcome_subtitle)
            textSize = 14f
            setTextColor(Color.parseColor("#64748B"))
            gravity = Gravity.CENTER
        }
        logoContainer.addView(logo)
        logoContainer.addView(title)
        logoContainer.addView(subtitle)
        launchScreen.addView(logoContainer)

        // 状态/消息卡片
        val cardBg = GradientDrawable().apply {
            cornerRadius = dp(20).toFloat()
            setColor(Color.WHITE)
            setStroke(dp(1), Color.parseColor("#E2E8F0"))
        }
        val statusCard = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = cardBg
            setPadding(dp(24), dp(24), dp(24), dp(24))
            elevation = dp(4).toFloat()
        }
        statusTitle = TextView(this).apply {
            textSize = 18f
            setTextColor(Color.parseColor("#0F172A"))
            gravity = Gravity.CENTER
            typeface = android.graphics.Typeface.create(typeface, android.graphics.Typeface.BOLD)
        }
        statusText = TextView(this).apply {
            textSize = 14f
            setTextColor(Color.parseColor("#64748B"))
            gravity = Gravity.CENTER
            setPadding(0, dp(8), 0, dp(12))
        }
        progress = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            max = 100
            isIndeterminate = true
            visibility = View.GONE
        }

        statusCard.addView(statusTitle)
        statusCard.addView(statusText)
        statusCard.addView(progress)
        launchScreen.addView(statusCard, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.WRAP_CONTENT,
        ).apply { topMargin = dp(24) })

        // Tokengine 登录按钮（默认隐藏，根据状态显示）
        loginButton = styledButton(getString(R.string.login_tokengine), Color.parseColor("#6366F1")).apply {
            setOnClickListener { startTokengineLogin() }
            visibility = View.GONE
        }
        launchScreen.addView(loginButton, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.WRAP_CONTENT,
        ).apply { topMargin = dp(16) })

        // 次要按钮区
        val secondaryRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
            setPadding(0, dp(16), 0, 0)
        }
        retryButton = outlinedButton(getString(R.string.retry)).apply {
            setOnClickListener {
                EngineManager.stop()
                EngineService.stop(this@MainActivity)
                EngineService.start(this@MainActivity)
            }
            visibility = View.GONE
        }
        settingsButton = outlinedButton(getString(R.string.settings)).apply {
            setOnClickListener { showSettings() }
        }
        logsButton = outlinedButton(getString(R.string.logs)).apply {
            setOnClickListener { showLogs() }
            visibility = View.GONE
        }
        secondaryRow.addView(retryButton, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        secondaryRow.addView(settingsButton, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f).apply { leftMargin = dp(8); rightMargin = dp(8) })
        secondaryRow.addView(logsButton, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        launchScreen.addView(secondaryRow, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.WRAP_CONTENT,
        ).apply { topMargin = dp(8) })
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun buildMainScreen() {
        mainScreen = FrameLayout(this)

        // 内容容器（WebView + Tab 内容）
        contentContainer = FrameLayout(this)
        mainScreen.addView(contentContainer, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.MATCH_PARENT,
        ))

        // TabLayout（底部）
        tabLayout = TabLayout(this).apply {
            setBackgroundColor(Color.WHITE)
            setSelectedTabIndicatorColor(Color.parseColor("#6366F1"))
            setSelectedTabIndicatorHeight(dp(3))
            tabMode = TabLayout.MODE_FIXED
            isInlineLabel = false
        }

        // 添加四个 Tab
        val chatTab = tabLayout.newTab().setText("💬 ${getString(R.string.tab_chat)}")
        val skillsTab = tabLayout.newTab().setText("⚡ ${getString(R.string.tab_skills)}")
        val expertsTab = tabLayout.newTab().setText("🎯 ${getString(R.string.tab_experts)}")
        val cameraTab = tabLayout.newTab().setText("📷 ${getString(R.string.tab_camera)}")
        tabLayout.addTab(chatTab)
        tabLayout.addTab(skillsTab)
        tabLayout.addTab(expertsTab)
        tabLayout.addTab(cameraTab)

        tabLayout.addOnTabSelectedListener(object : TabLayout.OnTabSelectedListener {
            override fun onTabSelected(tab: TabLayout.Tab?) {
                tab?.let { switchToTab(it.position) }
            }
            override fun onTabUnselected(tab: TabLayout.Tab?) {}
            override fun onTabReselected(tab: TabLayout.Tab?) {}
        })

        val tabParams = FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.WRAP_CONTENT,
        ).apply {
            gravity = Gravity.BOTTOM
        }
        mainScreen.addView(tabLayout, tabParams)

        // 创建 WebView 初始隐藏
        webView = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.databaseEnabled = true
            settings.loadWithOverviewMode = true
            settings.useWideViewPort = true
            settings.setSupportZoom(false)
            settings.builtInZoomControls = false
            settings.textZoom = 100
            settings.cacheMode = WebSettings.LOAD_DEFAULT
            settings.allowFileAccess = false
            settings.allowContentAccess = false
            settings.mediaPlaybackRequiresUserGesture = false
            settings.userAgentString = settings.userAgentString + " ThinkBuddyAndroid/${BuildConfig.VERSION_NAME}"
            webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(
                    view: WebView,
                    request: WebResourceRequest,
                ): Boolean {
                    val url = request.url
                    val host = url.host ?: return false
                    if (host == "127.0.0.1" || host == "localhost") return false
                    runCatching { startActivity(Intent(Intent.ACTION_VIEW, url)) }
                    return true
                }
            }
            webChromeClient = object : WebChromeClient() {
                override fun onConsoleMessage(message: ConsoleMessage): Boolean {
                    Log.d("ThinkBuddyWeb", message.message())
                    return true
                }

                override fun onShowFileChooser(
                    view: WebView?,
                    callback: ValueCallback<Array<Uri>>?,
                    params: FileChooserParams?,
                ): Boolean {
                    fileChooserCallback?.onReceiveValue(null)
                    fileChooserCallback = callback
                    val intent = params?.createIntent() ?: return false
                    return runCatching { fileChooser.launch(intent) }.isSuccess
                }
            }
        }
        webView!!.visibility = View.GONE
        contentContainer.addView(webView, matchParent())

        // 创建各 Tab 的原生内容页
        buildSkillsPage()
        buildExpertsPage()
        buildCameraPage()
    }

    private var skillsPage: LinearLayout? = null
    private var expertsPage: LinearLayout? = null
    private var cameraPage: LinearLayout? = null

    private fun buildSkillsPage() {
        skillsPage = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(20), dp(20), dp(80))
            setBackgroundColor(Color.parseColor("#FDF4FF"))
            visibility = View.GONE

            addView(pageHeader("⚡", "技能", "选择一个技能来增强你的对话"))

            val scroll = ScrollView(this@MainActivity)
            val list = LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.VERTICAL
            }

            val skills = listOf(
                "深度研究" to "自动分解复杂主题，生成研究报告",
                "头脑风暴" to "广度优先的创意探索，带来源依据",
                "数学求解" to "逐步推理解决数学问题",
                "代码编写" to "帮助编写、审查和重构代码",
                "论文搜索" to "在 arXiv 上搜索最新预印本",
                "联网搜索" to "实时搜索互联网获取最新信息",
                "图片生成" to "根据描述生成图像",
                "视频生成" to "根据描述生成短视频",
            )
            skills.forEachIndexed { index, (title, desc) ->
                list.addView(skillCard(title, desc, index))
            }
            scroll.addView(list)
            addView(scroll, LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                0,
                1f,
            ))
        }
        contentContainer.addView(skillsPage, matchParent())
    }

    private fun buildExpertsPage() {
        expertsPage = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(20), dp(20), dp(80))
            setBackgroundColor(Color.parseColor("#FFF7ED"))
            visibility = View.GONE

            addView(pageHeader("🎯", "专家", "选择一种专家角色来引导学习"))

            val scroll = ScrollView(this@MainActivity)
            val list = LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.VERTICAL
            }

            val experts = listOf(
                "苏格拉底式导师" to "通过提问引导你自己思考",
                "耐心讲解者" to "用最简单的语言解释复杂概念",
                "严格教练" to "高标准、精确、不留模糊空间",
                "创意伙伴" to "发散思维、联想、跨领域连接",
                "科研助手" to "引用论文、方法、实验设计",
                "编程导师" to "代码示例、调试、最佳实践",
                "数学老师" to "一步步推导、几何直觉",
                "语言教练" to "语法、词汇、真实场景练习",
            )
            experts.forEachIndexed { index, (title, desc) ->
                list.addView(expertCard(title, desc, index))
            }
            scroll.addView(list)
            addView(scroll, LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                0,
                1f,
            ))
        }
        contentContainer.addView(expertsPage, matchParent())
    }

    private fun buildCameraPage() {
        cameraPage = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(20), dp(20), dp(80))
            setBackgroundColor(Color.parseColor("#ECFDF5"))
            visibility = View.GONE

            addView(pageHeader("📷", "拍照", "拍一张照片让 AI 帮你理解"))

            // 相机预览占位
            val previewContainer = FrameLayout(this@MainActivity).apply {
                setBackgroundDrawable(GradientDrawable().apply {
                    cornerRadius = dp(20).toFloat()
                    setColor(Color.parseColor("#D1FAE5"))
                })
                setPadding(dp(16), dp(16), dp(16), dp(16))
                elevation = dp(2).toFloat()
            }
            val placeholder = TextView(this@MainActivity).apply {
                text = "📸"
                textSize = 72f
                gravity = Gravity.CENTER
                setPadding(0, dp(40), 0, dp(20))
            }
            val placeholderText = TextView(this@MainActivity).apply {
                text = "点击下方按钮拍照或选择图片"
                textSize = 14f
                setTextColor(Color.parseColor("#065F46"))
                gravity = Gravity.CENTER
                setPadding(0, 0, 0, dp(16))
            }
            previewContainer.addView(placeholder, FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT,
            ))
            previewContainer.addView(placeholderText, FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT,
            ).apply { gravity = Gravity.CENTER })
            addView(previewContainer, LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                0,
                1f,
            ).apply { topMargin = dp(24) })

            // 按钮区
            val buttonRow = LinearLayout(this@MainActivity).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER
                setPadding(0, dp(24), 0, 0)
            }
            val camBtn = styledButton("拍照", Color.parseColor("#10B981")).apply {
                layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
                setOnClickListener { openCamera() }
            }
            val galleryBtn = outlinedButton("相册").apply {
                layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
                (layoutParams as LinearLayout.LayoutParams).leftMargin = dp(12)
                setOnClickListener { openGallery() }
            }
            buttonRow.addView(camBtn)
            buttonRow.addView(galleryBtn)
            addView(buttonRow, LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
            ))

            // 提示
            val tip = TextView(this@MainActivity).apply {
                text = "支持拍照解题、识别图形、文档分析等"
                textSize = 12f
                setTextColor(Color.parseColor("#047857"))
                gravity = Gravity.CENTER
                setPadding(0, dp(16), 0, 0)
            }
            addView(tip)
        }
        contentContainer.addView(cameraPage, matchParent())
    }

    // ── Tab 切换 ──────────────────────────────────────────────

    private fun switchToTab(position: Int) {
        val wv = webView ?: return
        wv.visibility = if (position == 0) View.VISIBLE else View.GONE
        skillsPage?.visibility = if (position == 1) View.VISIBLE else View.GONE
        expertsPage?.visibility = if (position == 2) View.VISIBLE else View.GONE
        cameraPage?.visibility = if (position == 3) View.VISIBLE else View.GONE
    }

    // ── 引擎状态观察 ────────────────────────────────────────────

    private fun observeEngine() {
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                EngineManager.status.collect { render(it) }
            }
        }
    }

    private fun render(status: EngineStatus) {
        runOnUiThread {
            when (status.phase) {
                Phase.READY -> {
                    launchScreen.visibility = View.GONE
                    mainScreen.visibility = View.VISIBLE
                    val wv = webView
                    if (wv != null && loadedUrl != status.url) {
                        loadedUrl = status.url
                        wv.loadUrl(status.url)
                    }
                    // 默认选中对话 Tab
                    if (tabLayout.selectedTabPosition != 0) {
                        tabLayout.selectTab(tabLayout.getTabAt(0))
                    }
                    switchToTab(0)
                }
                Phase.INSTALLING -> showLaunchOverlay(
                    getString(R.string.installing_runtime),
                    status.message,
                    showProgress = true,
                    showLogin = true,
                )
                Phase.STARTING -> showLaunchOverlay(
                    getString(R.string.starting_engine),
                    status.message,
                    showProgress = true,
                    showLogin = false,
                )
                Phase.ERROR -> {
                    val log = ProotEngine.logSnapshot().takeLast(6).joinToString("\n")
                    showLaunchOverlay(
                        getString(R.string.engine_error),
                        status.message + if (log.isBlank()) "" else "\n\n$log",
                        showProgress = false,
                        showLogin = false,
                        showRetry = true,
                        showLogs = true,
                    )
                }
                Phase.IDLE, Phase.STOPPED -> showLaunchOverlay(
                    "引擎未启动",
                    "点击「开始使用」启动引擎",
                    showProgress = false,
                    showLogin = !tokengineLoggedIn,
                    showRetry = true,
                )
            }
        }
    }

    private fun showLaunchOverlay(
        title: String,
        message: String,
        showProgress: Boolean,
        showLogin: Boolean,
        showRetry: Boolean = false,
        showLogs: Boolean = false,
    ) {
        launchScreen.visibility = View.VISIBLE
        mainScreen.visibility = View.GONE
        statusTitle.text = title
        statusText.text = message
        loginButton.visibility = if (showLogin && !tokengineLoggedIn) View.VISIBLE else View.GONE
        retryButton.visibility = if (showRetry) View.VISIBLE else View.GONE
        logsButton.visibility = if (showLogs) View.VISIBLE else View.GONE

        val percent = Regex("(\\d+)%").find(message)?.groupValues?.get(1)?.toIntOrNull()
        if (percent != null) {
            progress.visibility = View.VISIBLE
            progress.isIndeterminate = false
            progress.progress = percent
        } else if (showProgress) {
            progress.visibility = View.VISIBLE
            progress.isIndeterminate = true
        } else {
            progress.visibility = View.GONE
        }
    }

    // ── Tokengine 登录（在运行时之前） ─────────────────────────

    /**
     * 启动前检查 Tokengine 登录状态。
     *
     * Tokengine 登录脚本需要运行时（PRoot + Python）才能执行，所以"登录在运行时之前"
     * 只能做到：在 WebView 显示之前通过原生引导完成登录，避免用户进 WebView 才发现
     * 没有可用模型。
     *
     * 引擎无论如都会启动（下载运行时、拉起前后端），登录引导只是：
     *   a) 已登录 → 静默通过，用户无感知
     *   b) 未登录 → 引擎启动后遮罩仍停留在引导态，等用户主动点「登录」后再切到 READY
     *      也可以让 READY 直接过，进入主界面后用户再从设置登录
     */
    private fun checkTokengineLogin() {
        tokengineLoggedIn = hasConfiguredModel()
        // 无论是否登录都启动引擎 - 运行时下载和安装不依赖登录
        EngineService.start(this)
        if (tokengineLoggedIn) {
            // 已登录：等引擎就绪后直接进入主界面
            showLaunchOverlay(
                "正在准备 ThinkBuddy",
                "已检测到 Tokengine 登录，正在启动引擎…",
                showProgress = true,
                showLogin = false,
            )
        } else {
            // 未登录：显示引导 + 登录按钮，同时后台继续安装/启动引擎
            showLaunchOverlay(
                "欢迎使用 ThinkBuddy",
                "登录 Tokengine 账号以获取可用模型\n引擎正在后台启动，你可以稍后从设置登录",
                showProgress = true,
                showLogin = true,
            )
        }
    }

    /**
     * Tokengine 账号登录：引擎侧完成 OAuth（回环回调）并写入 model catalog。
     *
     * 登录脚本 (tokengine_login.py) 跑在 PRoot guest 里，需要运行时已经安装
     * （rootfs + proot + python）才能执行。所以用户必须等 INSTALLING 阶段完成
     * 后再点登录按钮。
     */
    private fun startTokengineLogin() {
        if (!RuntimeInstaller.isInstalled(this)) {
            toast("运行时尚未安装完成，请稍候…")
            return
        }
        val progressDialog = AlertDialog.Builder(this)
            .setTitle("登录 Tokengine")
            .setMessage("正在准备登录…")
            .setCancelable(true)
            .setNegativeButton("取消") { dialog, _ ->
                ProotEngine.stopLogin()
                dialog.dismiss()
            }
            .create()
        progressDialog.show()
        ProotEngine.loginTokengine(
            this,
            onAuthorize = { url ->
                runOnUiThread {
                    if (progressDialog.isShowing) {
                        progressDialog.setMessage("已在浏览器打开登录页，完成授权后会自动返回。")
                    }
                    runCatching { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url))) }
                        .onFailure { toast("无法打开浏览器：${it.message}") }
                }
            },
            onResult = { ok, message ->
                runOnUiThread {
                    if (progressDialog.isShowing) progressDialog.dismiss()
                    if (ok) {
                        tokengineLoggedIn = true
                        toast("登录成功，已写入模型：$message")
                        // 登录成功后自动启动引擎
                        EngineService.start(this@MainActivity)
                    } else {
                        toast("登录失败：$message")
                    }
                }
            },
        )
    }

    /** 读沙盒里的 model_catalog.json，判断是否存在带模型的 llm profile。 */
    private fun hasConfiguredModel(): Boolean = runCatching {
        val dir = RuntimeInstaller.homeDir(this)
        if (!dir.exists()) return@runCatching false
        val catalog = File(dir, "data/user/settings/model_catalog.json")
        if (!catalog.isFile) return@runCatching false
        val root = org.json.JSONObject(catalog.readText())
        val profiles = root.optJSONObject("services")
            ?.optJSONObject("llm")
            ?.optJSONArray("profiles")
            ?: return@runCatching false
        (0 until profiles.length()).any { index ->
            val profile = profiles.optJSONObject(index) ?: return@any false
            val apiKey = profile.optString("api_key").trim()
            val models = profile.optJSONArray("models")?.length() ?: 0
            apiKey.isNotEmpty() && models > 0
        }
    }.getOrDefault(false)

    // ── 相机 / 拍照 ─────────────────────────────────────────────

    private fun openCamera() {
        val intent = Intent(android.provider.MediaStore.ACTION_IMAGE_CAPTURE)
        if (intent.resolveActivity(packageManager) != null) {
            cameraLauncher.launch(intent)
        } else {
            toast("无法打开相机")
        }
    }

    private fun openGallery() {
        val intent = Intent(Intent.ACTION_PICK, android.provider.MediaStore.Images.Media.EXTERNAL_CONTENT_URI)
        intent.type = "image/*"
        if (intent.resolveActivity(packageManager) != null) {
            cameraLauncher.launch(intent)
        } else {
            toast("无法打开相册")
        }
    }

    private fun requestPermissions() {
        val perms = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) !=
                PackageManager.PERMISSION_GRANTED
            ) {
                perms += Manifest.permission.POST_NOTIFICATIONS
            }
        }
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            perms += Manifest.permission.CAMERA
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.READ_MEDIA_IMAGES) !=
                PackageManager.PERMISSION_GRANTED
            ) {
                perms += Manifest.permission.READ_MEDIA_IMAGES
            }
        } else {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.READ_EXTERNAL_STORAGE) !=
                PackageManager.PERMISSION_GRANTED
            ) {
                perms += Manifest.permission.READ_EXTERNAL_STORAGE
            }
        }
        if (perms.isNotEmpty()) {
            requestPermissions(perms.toTypedArray(), 1)
        }
    }

    // ── 设置 / 日志 ──────────────────────────────────────────────

    private fun showSettings() {
        val prefs = Prefs(this)
        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(8), dp(24), dp(8))
        }

        val runtimeUrl = edit(prefs.runtimeUrl, "运行时下载地址（.zip）")
        content.addView(label("运行时地址"))
        content.addView(runtimeUrl)

        content.addView(label("Tokengine 账号"))
        content.addView(Button(this).apply {
            text = if (tokengineLoggedIn) "已登录（点击重新登录）" else "登录 Tokengine 账号"
            setOnClickListener {
                AlertDialog.Builder(this@MainActivity)
                    .setTitle("Tokengine")
                    .setMessage("登录前需要运行时已安装。当前状态：${if (RuntimeInstaller.isInstalled(this@MainActivity)) "已就绪" else "未安装"}")
                    .setPositiveButton("继续") { _, _ -> startTokengineLogin() }
                    .setNegativeButton("取消", null)
                    .show()
            }
        })

        AlertDialog.Builder(this)
            .setTitle("设置")
            .setView(ScrollView(this).apply { addView(content) })
            .setPositiveButton("保存") { _, _ ->
                prefs.runtimeUrl = runtimeUrl.text.toString().trim()
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun showLogs() {
        val log = ProotEngine.logSnapshot().joinToString("\n")
        val body = TextView(this).apply {
            text = log.ifBlank { "（暂无日志）" }
            textSize = 11f
            setTextColor(Color.parseColor("#222222"))
            setTextIsSelectable(true)
            setPadding(dp(16), dp(12), dp(16), dp(12))
        }
        val scroll = ScrollView(this).apply { addView(body) }
        AlertDialog.Builder(this)
            .setTitle("引擎日志")
            .setView(scroll)
            .setPositiveButton("关闭", null)
            .show()
    }

    private fun toast(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show()
    }

    // ── 动态视图创建辅助 ────────────────────────────────────────

    private var statusTitle: TextView = TextView(this)
    private var statusText: TextView = TextView(this)
    private var progress: ProgressBar = ProgressBar(this)
    private var loginButton: Button = Button(this)
    private var retryButton: Button = Button(this)
    private var settingsButton: Button = Button(this)
    private var logsButton: Button = Button(this)

    private fun gradientBg(): GradientDrawable {
        return GradientDrawable(
            GradientDrawable.Orientation.TOP_BOTTOM,
            intArrayOf(
                Color.parseColor("#EEF2FF"),
                Color.parseColor("#E0E7FF"),
                Color.parseColor("#EEF2FF"),
            ),
        ).apply { cornerRadius = 0f }
    }

    private fun styledButton(text: String, color: Int): Button = Button(this).apply {
        this.text = text
        setBackgroundColor(color)
        setTextColor(Color.WHITE)
        setPadding(dp(24), dp(14), dp(24), dp(14))
        textSize = 15f
        setAllCaps(false)
        elevation = dp(2).toFloat()
        background = GradientDrawable().apply {
            cornerRadius = dp(12).toFloat()
            setColor(color)
        }
    }

    private fun outlinedButton(text: String): Button = Button(this).apply {
        this.text = text
        setTextColor(Color.parseColor("#6366F1"))
        setPadding(dp(16), dp(10), dp(16), dp(10))
        textSize = 13f
        setAllCaps(false)
        background = GradientDrawable().apply {
            cornerRadius = dp(10).toFloat()
            setColor(Color.WHITE)
            setStroke(dp(1), Color.parseColor("#C7D2FE"))
        }
    }

    private fun label(text: String): TextView = TextView(this).apply {
        this.text = text
        textSize = 12f
        setTextColor(Color.parseColor("#64748B"))
        setPadding(0, dp(12), 0, dp(4))
    }

    private fun edit(value: String, hint: String): EditText = EditText(this).apply {
        setText(value)
        this.hint = hint
        inputType = android.text.InputType.TYPE_CLASS_TEXT
        setSingleLine()
        background = GradientDrawable().apply {
            cornerRadius = dp(10).toFloat()
            setColor(Color.parseColor("#F1F5F9"))
            setStroke(dp(1), Color.parseColor("#E2E8F0"))
        }
        setPadding(dp(12), dp(10), dp(12), dp(10))
    }

    private fun pageHeader(emoji: String, title: String, subtitle: String): View {
        return LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(0, dp(12), 0, dp(20))
            addView(TextView(this@MainActivity).apply {
                text = emoji
                textSize = 36f
                gravity = Gravity.CENTER
            })
            addView(TextView(this@MainActivity).apply {
                text = title
                textSize = 22f
                setTextColor(Color.parseColor("#0F172A"))
                gravity = Gravity.CENTER
                typeface = android.graphics.Typeface.create(typeface, android.graphics.Typeface.BOLD)
            })
            addView(TextView(this@MainActivity).apply {
                text = subtitle
                textSize = 13f
                setTextColor(Color.parseColor("#64748B"))
                gravity = Gravity.CENTER
                setPadding(0, dp(4), 0, 0)
            })
        }
    }

    private fun skillCard(title: String, desc: String, index: Int): View {
        val colors = listOf(
            Color.parseColor("#DBEAFE") to Color.parseColor("#1D4ED8"),
            Color.parseColor("#FEF3C7") to Color.parseColor("#B45309"),
            Color.parseColor("#FCE7F3") to Color.parseColor("#BE185D"),
            Color.parseColor("#DDD6FE") to Color.parseColor("#6D28D9"),
            Color.parseColor("#D1FAE5") to Color.parseColor("#047857"),
            Color.parseColor("#FFEDD5") to Color.parseColor("#C2410C"),
            Color.parseColor("#E0E7FF") to Color.parseColor("#4338CA"),
            Color.parseColor("#FEE2E2") to Color.parseColor("#B91C1C"),
        )
        val (bg, fg) = colors[index % colors.size]
        return cardRow(title, desc, bg, fg)
    }

    private fun expertCard(title: String, desc: String, index: Int): View {
        val colors = listOf(
            Color.parseColor("#FEF9C3") to Color.parseColor("#854D0E"),
            Color.parseColor("#FFE4E6") to Color.parseColor("#9F1239"),
            Color.parseColor("#E0E7FF") to Color.parseColor("#3730A3"),
            Color.parseColor("#F0FDF4") to Color.parseColor("#166534"),
            Color.parseColor("#FDF4FF") to Color.parseColor("#7E22CE"),
            Color.parseColor("#EFF6FF") to Color.parseColor("#1D4ED8"),
            Color.parseColor("#F0F9FF") to Color.parseColor("#0369A1"),
            Color.parseColor("#FFF1F2") to Color.parseColor("#BE123C"),
        )
        val (bg, fg) = colors[index % colors.size]
        return cardRow(title, desc, bg, fg)
    }

    private fun cardRow(title: String, desc: String, bgColor: Int, fgColor: Int): View {
        val cardBg = GradientDrawable().apply {
            cornerRadius = dp(16).toFloat()
            setColor(bgColor)
        }
        return LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = cardBg
            setPadding(dp(20), dp(16), dp(20), dp(16))
            elevation = dp(2).toFloat()
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
            ).apply { bottomMargin = dp(10) }
            setOnClickListener {
                tabLayout.selectTab(tabLayout.getTabAt(0))
                toast("已选择：$title\n（引擎就绪后会自动应用）")
            }
            addView(TextView(this@MainActivity).apply {
                text = title
                textSize = 16f
                setTextColor(fgColor)
                typeface = android.graphics.Typeface.create(typeface, android.graphics.Typeface.BOLD)
            })
            addView(TextView(this@MainActivity).apply {
                text = desc
                textSize = 12f
                setTextColor(Color.parseColor("#475569"))
                setPadding(0, dp(4), 0, 0)
            })
        }
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    private fun matchParent() = FrameLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT,
        ViewGroup.LayoutParams.MATCH_PARENT,
    )
}
