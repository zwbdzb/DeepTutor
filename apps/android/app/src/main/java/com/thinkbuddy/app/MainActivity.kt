package com.thinkbuddy.app

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.ConsoleMessage
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebChromeClient.FileChooserParams
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.io.File

/**
 * 手机端主界面（本地引擎形态）。
 *
 * ## 为什么这样写
 *
 * 1. **所有 View 都是 lateinit，绝不在属性初始化器里 ``TextView(this)``**。
 *    Activity 由系统反射构造，``attachBaseContext`` 之前 base context 为 null，
 *    View 构造函数一取 ``getResources()/getTheme()`` 就 NPE —— 这是旧版本
 *    「打包出来直接闪退、进都进不去」的根因。
 * 2. **底部导航用纯LinearLayout 自绘，不用 Material TabLayout**：AppCompat 主题
 *    与 MaterialComponents 主题约束是另一类启动期崩溃源，且自定义导航才能做
 *    dsh-Go 那种图标 + 标签 + 选中态。
 * 3. **手机专用 UI 放在 assets/mobile，通过 shouldInterceptRequest 注入到
 *    ``http://127.0.0.1:<frontendPort>/__thinkbuddy_mobile__/``**：与 Next
 *    standalone 同源，``/api/*`` 与 ``/ws`` 直接走 ``web/proxy.ts`` 的重写，
 *    零 CORS 问题，也不用重新构建 1GB 运行时包。
 * 4. **登录门在引擎之前**：未登录只停在引导页，不下载/解压运行时；登录成功
 *    后才 ``EngineService.start()``。见 [TokengineAuth]。
 */
class MainActivity : AppCompatActivity() {

    // 所有视图字段都延迟到 buildUi() 里、Context 完全 attach 之后才创建。
    private lateinit var rootContainer: FrameLayout
    private lateinit var launchScreen: LinearLayout
    private lateinit var mainScreen: FrameLayout
    private lateinit var webView: WebView
    private lateinit var topBar: LinearLayout
    private lateinit var navContainer: LinearLayout
    private lateinit var statusTitle: TextView
    private lateinit var statusText: TextView
    private lateinit var progress: ProgressBar
    private lateinit var loginButton: Button
    private lateinit var skipButton: Button
    private lateinit var retryButton: Button
    private lateinit var settingsButton: Button
    private lateinit var logsButton: Button
    private lateinit var statusDot: TextView
    private lateinit var palette: Palette

    private var loadedUrl: String? = null
    private var fileChooserCallback: ValueCallback<Array<Uri>>? = null
    private var cameraPhotoUri: Uri? = null
    private var currentTab = 0
    private var engineStarted = false
    // 重启过渡期：屏蔽 EngineManager 立刻发出的 STOPPED/IDLE，避免界面闪成
    // 「引擎未启动」再跳回「正在启动」。
    private var suppressingIdle = false

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
            val uri = cameraPhotoUri
            if (uri != null) deliverImage(uri) else toast("拍照结果丢失")
        }
    }

    private val galleryLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        val uri = result.data?.data
        if (result.resultCode == RESULT_OK && uri != null) {
            deliverImage(uri)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        palette = buildPalette()
        applyWindowColors()
        buildUi()
        requestBasePermissions()
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (mainScreen.visibility == View.VISIBLE && currentTab != 0) {
                    switchToTab(0)
                } else if (webView.canGoBack()) {
                    webView.goBack()
                } else {
                    finish()
                }
            }
        })
        observeEngine()
        bootstrap()
    }

    override fun onDestroy() {
        // buildUi() 若在 attach 之前抛异常，lateinit 仍未初始化；直接 destroy()
        // 会在崩溃回调里再抛一次 UninitializedPropertyAccessException，把真正的
        // 启动异常盖掉。
        if (::webView.isInitialized) webView.destroy()
        super.onDestroy()
    }

    // ── 主题 ──────────────────────────────────────────────────────

    private data class Palette(
        val primary: Int,
        val primaryDark: Int,
        val bg: Int,
        val surface: Int,
        val text: Int,
        val textSub: Int,
        val divider: Int,
    )

    private fun buildPalette(): Palette {
        val dark = (resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) ==
            Configuration.UI_MODE_NIGHT_YES
        val index = Prefs(this).themeIndex.coerceIn(0, THEME_COLORS.size - 1)
        val primary = Color.parseColor(THEME_COLORS[index])
        return Palette(
            primary = primary,
            primaryDark = shade(primary, 0.78f),
            bg = Color.parseColor(if (dark) "#0B1120" else "#F5F7FB"),
            surface = Color.parseColor(if (dark) "#151E32" else "#FFFFFF"),
            text = Color.parseColor(if (dark) "#E9EEF8" else "#0F172A"),
            textSub = Color.parseColor(if (dark) "#94A3B8" else "#64748B"),
            divider = Color.parseColor(if (dark) "#25314A" else "#E4E9F2"),
        )
    }

    private fun shade(color: Int, factor: Float): Int = Color.rgb(
        (Color.red(color) * factor).toInt(),
        (Color.green(color) * factor).toInt(),
        (Color.blue(color) * factor).toInt(),
    )

    private fun applyWindowColors() {
        window.statusBarColor = palette.surface
        window.navigationBarColor = palette.surface
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            val dark = (resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) ==
                Configuration.UI_MODE_NIGHT_YES
            var flags = window.decorView.systemUiVisibility
            flags = if (dark) {
                flags and View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR.inv()
            } else {
                flags or View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR
            }
            window.decorView.systemUiVisibility = flags
        }
    }

    // ── UI 构建 ───────────────────────────────────────────────────

    private fun buildUi() {
        rootContainer = FrameLayout(this).apply { setBackgroundColor(palette.bg) }
        buildLaunchScreen()
        buildMainScreen()
        rootContainer.addView(launchScreen, matchParent())
        rootContainer.addView(mainScreen, matchParent())
        mainScreen.visibility = View.GONE
        setContentView(rootContainer)
    }

    /** 引导 / 登录屏：Tokengine 登录 →（通过后才）运行时安装 → 引擎启动状态。 */
    private fun buildLaunchScreen() {
        launchScreen = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = GradientDrawable(
                GradientDrawable.Orientation.TOP_BOTTOM,
                intArrayOf(palette.bg, palette.surface, palette.bg),
            )
            setPadding(dp(28), dp(48), dp(28), dp(28))
            gravity = Gravity.CENTER_HORIZONTAL
        }

        val logoContainer = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setPadding(0, dp(36), 0, dp(28))
        }
        logoContainer.addView(ImageView(this).apply {
            setImageResource(R.drawable.ic_launcher)
            layoutParams = LinearLayout.LayoutParams(dp(88), dp(88))
        })
        logoContainer.addView(TextView(this).apply {
            text = getString(R.string.welcome_title)
            textSize = 30f
            setTextColor(palette.text)
            gravity = Gravity.CENTER
            setPadding(0, dp(14), 0, dp(4))
            typeface = Typeface.create(typeface, Typeface.BOLD)
        })
        logoContainer.addView(TextView(this).apply {
            text = getString(R.string.welcome_subtitle)
            textSize = 14f
            setTextColor(palette.textSub)
            gravity = Gravity.CENTER
        })
        launchScreen.addView(logoContainer)

        val statusCard = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = GradientDrawable().apply {
                cornerRadius = dp(20).toFloat()
                setColor(palette.surface)
                setStroke(dp(1), palette.divider)
            }
            setPadding(dp(22), dp(22), dp(22), dp(22))
            elevation = dp(3).toFloat()
        }
        statusTitle = TextView(this).apply {
            textSize = 18f
            setTextColor(palette.text)
            gravity = Gravity.CENTER
            typeface = Typeface.create(typeface, Typeface.BOLD)
        }
        statusText = TextView(this).apply {
            textSize = 13f
            setTextColor(palette.textSub)
            gravity = Gravity.CENTER
            setPadding(0, dp(10), 0, dp(14))
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
        ).apply { topMargin = dp(20) })

        loginButton = primaryButton(getString(R.string.login_tokengine)).apply {
            setOnClickListener { startTokengineLogin() }
        }
        launchScreen.addView(loginButton, fullWidthButtonParams(dp(16)))

        skipButton = outlinedButton(getString(R.string.skip_login)).apply {
            setOnClickListener { skipLogin() }
        }
        launchScreen.addView(skipButton, fullWidthButtonParams(dp(10)))

        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
            setPadding(0, dp(18), 0, 0)
        }
        retryButton = outlinedButton(getString(R.string.retry)).apply {
            setOnClickListener { restartEngine() }
            visibility = View.GONE
        }
        settingsButton = outlinedButton(getString(R.string.settings)).apply {
            setOnClickListener { showSettings() }
        }
        logsButton = outlinedButton(getString(R.string.logs)).apply {
            setOnClickListener { showLogs() }
            visibility = View.GONE
        }
        row.addView(retryButton, linearRowParams())
        row.addView(settingsButton, linearRowParams(dp(8), dp(8)))
        row.addView(logsButton, linearRowParams())
        launchScreen.addView(row, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.WRAP_CONTENT,
        ))
    }

    @SuppressLint("SetJavaScriptEnabled", "AddJavascriptInterface")
    private fun buildMainScreen() {
        mainScreen = FrameLayout(this).apply { setBackgroundColor(palette.bg) }

        webView = WebView(this).apply {
            setBackgroundColor(palette.bg)
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.databaseEnabled = true
            settings.loadWithOverviewMode = true
            settings.useWideViewPort = true
            settings.setSupportZoom(false)
            settings.builtInZoomControls = false
            settings.textZoom = 100
            settings.cacheMode = WebSettings.LOAD_NO_CACHE
            settings.allowFileAccess = false
            settings.allowContentAccess = false
            settings.mediaPlaybackRequiresUserGesture = false
            settings.userAgentString =
                settings.userAgentString + " ThinkBuddyAndroid/" + BuildConfig.VERSION_NAME
            addJavascriptInterface(Bridge(), "ThinkBuddy")
            webViewClient = object : WebViewClient() {
                override fun shouldInterceptRequest(
                    view: WebView,
                    request: WebResourceRequest,
                ): WebResourceResponse? {
                    val url = request.url ?: return null
                    val path = url.path ?: return null
                    if (url.host != "127.0.0.1" || !path.startsWith(MOBILE_PREFIX)) return null
                    return runCatching { mobileAsset(path) }.getOrNull()
                }

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
        mainScreen.addView(webView, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.MATCH_PARENT,
        ).apply {
            topMargin = dp(56)
            bottomMargin = dp(66)
        })

        buildTopBar()
        buildBottomNav()
    }

    private fun buildTopBar() {
        topBar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setBackgroundColor(palette.surface)
            setPadding(dp(18), 0, dp(12), 0)
            elevation = dp(2).toFloat()
        }
        topBar.addView(TextView(this).apply {
            text = getString(R.string.app_name)
            textSize = 18f
            setTextColor(palette.text)
            typeface = Typeface.create(typeface, Typeface.BOLD)
        }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        statusDot = TextView(this).apply {
            text = "●"
            textSize = 11f
            setTextColor(palette.textSub)
            setPadding(0, 0, dp(6), 0)
        }
        topBar.addView(statusDot)
        topBar.addView(TextView(this).apply {
            text = getString(R.string.engine_local)
            textSize = 11f
            setTextColor(palette.textSub)
        })
        topBar.addView(Button(this).apply {
            text = "⚙"
            textSize = 17f
            setAllCaps(false)
            setBackgroundColor(Color.TRANSPARENT)
            setTextColor(palette.textSub)
            setPadding(dp(10), 0, dp(4), 0)
            setOnClickListener { showSettings() }
        })
        mainScreen.addView(topBar, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            dp(56),
        ).apply { gravity = Gravity.TOP })
    }

    private fun buildBottomNav() {
        navContainer = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setBackgroundColor(palette.surface)
            elevation = dp(6).toFloat()
            setPadding(dp(4), dp(6), dp(4), dp(6))
        }
        NAV_TABS.forEachIndexed { index, (icon, label) ->
            val item = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                gravity = Gravity.CENTER
                setPadding(dp(4), dp(4), dp(4), dp(2))
                setOnClickListener { switchToTab(index) }
            }
            item.addView(TextView(this).apply {
                text = icon
                textSize = 19f
                gravity = Gravity.CENTER
            })
            item.addView(TextView(this).apply {
                text = label
                textSize = 11f
                gravity = Gravity.CENTER
                setPadding(0, dp(2), 0, 0)
            })
            navContainer.addView(item, LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f,
            ))
        }
        mainScreen.addView(navContainer, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            dp(66),
        ).apply { gravity = Gravity.BOTTOM })
        switchToTab(0)
    }

    private fun switchToTab(position: Int) {
        currentTab = position
        for (index in 0 until navContainer.childCount) {
            val item = navContainer.getChildAt(index) as? LinearLayout ?: continue
            val icon = item.getChildAt(0) as? TextView ?: continue
            val label = item.getChildAt(1) as? TextView ?: continue
            val active = index == position
            val color = if (active) palette.primary else palette.textSub
            icon.setTextColor(color)
            label.setTextColor(color)
            item.alpha = if (active) 1f else 0.6f
        }
        // H5 自己管理 5 个视图；原生只负责导航高亮、拍照与主题。
        webView.evaluateJavascript(
            "window.__tb && window.__tb.setTab(" + position + ")", null,
        )
    }

    // ── 引擎状态 ──────────────────────────────────────────────────

    private fun observeEngine() {
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                EngineManager.status.collect { status -> render(status) }
            }
        }
    }

    private fun render(status: EngineStatus) {
        // 登录门还没放行：引擎状态一律不覆盖引导页。
        if (!engineStarted) return
        runOnUiThread {
            when (status.phase) {
                Phase.READY -> {
                    suppressingIdle = false
                    launchScreen.visibility = View.GONE
                    mainScreen.visibility = View.VISIBLE
                    statusDot.setTextColor(Color.parseColor("#10B981"))
                    val url = mobileUrl()
                    if (loadedUrl != url) {
                        loadedUrl = url
                        webView.loadUrl(url)
                    }
                    switchToTab(currentTab)
                }
                Phase.INSTALLING -> {
                    statusDot.setTextColor(palette.textSub)
                    showEngineOverlay(
                        getString(R.string.installing_runtime), status.message,
                        showProgress = true,
                    )
                }
                Phase.STARTING -> {
                    statusDot.setTextColor(Color.parseColor("#F59E0B"))
                    showEngineOverlay(
                        getString(R.string.starting_engine), status.message,
                        showProgress = true,
                    )
                }
                Phase.ERROR -> {
                    suppressingIdle = false
                    statusDot.setTextColor(Color.parseColor("#EF4444"))
                    val log = ProotEngine.logSnapshot().takeLast(6).joinToString("\n")
                    showEngineOverlay(
                        getString(R.string.engine_error),
                        status.message + if (log.isBlank()) "" else "\n\n$log",
                        showRetry = true,
                        showLogs = true,
                        showLogin = !TokengineAuth.isLoggedIn(this@MainActivity),
                    )
                }
                Phase.IDLE, Phase.STOPPED -> {
                    if (!suppressingIdle) {
                        statusDot.setTextColor(palette.textSub)
                        showEngineOverlay(
                            getString(R.string.engine_idle), status.message.ifBlank {
                                getString(R.string.engine_idle_hint)
                            },
                            showRetry = true,
                            showLogin = !TokengineAuth.isLoggedIn(this@MainActivity),
                        )
                    }
                }
            }
        }
    }

    private fun showEngineOverlay(
        title: String,
        message: String,
        showProgress: Boolean = false,
        showRetry: Boolean = false,
        showLogs: Boolean = false,
        showLogin: Boolean = false,
    ) {
        launchScreen.visibility = View.VISIBLE
        mainScreen.visibility = View.GONE
        statusTitle.text = title
        statusText.text = message
        loginButton.visibility = if (showLogin) View.VISIBLE else View.GONE
        skipButton.visibility = View.GONE
        retryButton.visibility = if (showRetry) View.VISIBLE else View.GONE
        logsButton.visibility = if (showLogs) View.VISIBLE else View.GONE

        val percent = Regex("(\\d+)%").find(message)?.groupValues?.get(1)?.toIntOrNull()
        when {
            percent != null -> {
                progress.visibility = View.VISIBLE
                progress.isIndeterminate = false
                progress.progress = percent
            }
            showProgress -> {
                progress.visibility = View.VISIBLE
                progress.isIndeterminate = true
            }
            else -> progress.visibility = View.GONE
        }
    }

    // ── 登录门（在解压运行时之前） ────────────────────────────────

    private fun bootstrap() {
        if (TokengineAuth.isLoggedIn(this)) {
            engineStarted = true
            showEngineOverlay(
                getString(R.string.boot_logged_in),
                getString(R.string.boot_logged_in_hint),
                showProgress = true,
            )
            EngineService.start(this)
        } else {
            showLoginGate()
        }
    }

    private fun showLoginGate() {
        launchScreen.visibility = View.VISIBLE
        mainScreen.visibility = View.GONE
        statusTitle.text = getString(R.string.login_gate_title)
        statusText.text = getString(R.string.login_gate_hint)
        progress.visibility = View.GONE
        loginButton.visibility = View.VISIBLE
        skipButton.visibility = View.VISIBLE
        retryButton.visibility = View.GONE
        logsButton.visibility = View.GONE
    }

    private fun showLoginProgress(message: String) {
        statusTitle.text = getString(R.string.login_in_progress)
        statusText.text = message
        progress.visibility = View.VISIBLE
        progress.isIndeterminate = true
        loginButton.visibility = View.GONE
        skipButton.visibility = View.VISIBLE
        skipButton.text = getString(R.string.cancel)
        skipButton.setOnClickListener { cancelLogin() }
    }

    private fun cancelLogin() {
        TokengineAuth.cancel()
        skipButton.text = getString(R.string.skip_login)
        skipButton.setOnClickListener { skipLogin() }
        // 从设置里发起的登录：取消后留在当前界面，不要把正在用的 App 推到登录门。
        if (mainScreen.visibility == View.VISIBLE) return
        showLoginGate()
    }

    private fun startTokengineLogin() {
        skipButton.text = getString(R.string.cancel)
        skipButton.setOnClickListener { cancelLogin() }
        showLoginProgress(getString(R.string.login_preparing))
        TokengineAuth.login(
            this,
            onAuthorize = { url ->
                runOnUiThread {
                    showLoginProgress(getString(R.string.login_browser_hint))
                    runCatching { startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url))) }
                        .onFailure {
                            toast(getString(R.string.login_browser_failed) + it.message)
                            showLoginGate()
                        }
                }
            },
            onResult = { result ->
                runOnUiThread {
                    skipButton.text = getString(R.string.skip_login)
                    skipButton.setOnClickListener { skipLogin() }
                    if (result.ok) {
                        toast(getString(R.string.login_success) + "（" + result.models + "）")
                        proceedAfterLogin()
                    } else {
                        toast(getString(R.string.login_failed) + result.message)
                        // 从设置里发起的登录失败时不要把人踢回登录门：引擎还在跑。
                        if (mainScreen.visibility != View.VISIBLE) showLoginGate()
                    }
                }
            },
        )
    }

    /**
     * 登录成功后的唯一入口。
     *
     * 关键点：种子文件（tokengine_seed.json）只在后端启动脚本里消费一次，所以
     * 如果引擎已经在跑，光写种子不会生效 —— 必须重启一次引擎，让它把种子写进
     * model_catalog.json，否则用户会看到「登录成功但没有模型」。
     */
    private fun proceedAfterLogin() {
        engineStarted = true
        showEngineOverlay(
            getString(R.string.login_success),
            getString(R.string.boot_after_login),
            showProgress = true,
        )
        if (EngineManager.status.value.phase == Phase.READY) {
            restartEngine(delayMs = 600)
        } else {
            EngineService.start(this)
        }
    }

    private fun skipLogin() {
        engineStarted = true
        showEngineOverlay(
            getString(R.string.boot_skipped),
            getString(R.string.boot_skipped_hint),
            showProgress = true,
        )
        EngineService.start(this)
    }

    /**
     * 重启引擎。`delayMs > 0` 用于「登录成功但引擎已在跑」的场景：先让
     * EngineService 的 stopService 真正走到 onDestroy，再发 startService，
     * 否则新实例可能被旧实例的 onDestroy 顺手停掉。
     */
    private fun restartEngine(delayMs: Long = 0) {
        engineStarted = true
        // 重启过程中 EngineManager 会先发 STOPPED，别让「引擎未启动 + 重试」
        // 闪一下把「正在启动」盖掉。
        suppressingIdle = true
        showEngineOverlay(
            getString(R.string.starting_engine), "",
            showProgress = true,
        )
        val restart = Runnable {
            EngineManager.stop()
            EngineService.stop(this)
            EngineService.start(this)
        }
        if (delayMs > 0) {
            Handler(Looper.getMainLooper()).postDelayed(restart, delayMs)
        } else {
            restart.run()
        }
    }

    private fun mobileUrl(): String =
        "http://127.0.0.1:" + BuildConfig.FRONTEND_PORT + MOBILE_PREFIX + "/index.html"

    // ── assets 注入（与 Next 同源） ────────────────────────────────

    private fun mobileAsset(path: String): WebResourceResponse {
        if (path.contains("photo.jpg")) {
            val file = File(filesDir, "mobile_photo.jpg")
            if (file.isFile) {
                return WebResourceResponse(
                    "image/jpeg", "utf-8", 200, "OK",
                    mapOf("Cache-Control" to "no-store"), file.inputStream(),
                )
            }
            return WebResourceResponse(
                "text/plain", "utf-8", 404, "Not Found",
                emptyMap(), "".byteInputStream(),
            )
        }
        val name = path.removePrefix(MOBILE_PREFIX).trimStart('/').ifEmpty { "index.html" }
        // 只允许白名单文件名，避免路径穿越读任意 asset。
        if (name != "index.html" && name != "app.js" && name != "style.css") {
            return WebResourceResponse(
                "text/plain", "utf-8", 404, "Not Found",
                emptyMap(), "".byteInputStream(),
            )
        }
        val mime = when {
            name.endsWith(".js") -> "application/javascript"
            name.endsWith(".css") -> "text/css"
            else -> "text/html"
        }
        return WebResourceResponse(
            mime, "utf-8", 200, "OK",
            mapOf("Cache-Control" to "no-store"), assets.open("mobile/$name"),
        )
    }

    // ── 原生桥（给 assets/mobile 用） ─────────────────────────────

    private inner class Bridge {
        @android.webkit.JavascriptInterface
        fun takePhoto() = runOnUiThread { openCamera() }

        @android.webkit.JavascriptInterface
        fun pickImage() = runOnUiThread { openGallery() }

        @android.webkit.JavascriptInterface
        // 必须显式写 this@MainActivity：不限定接收者时会解析到 Bridge.toast 自己，
        // 变成无限递归（StackOverflowError，UI 线程直接崩）。
        fun toast(message: String) = runOnUiThread { this@MainActivity.toast(message) }

        @android.webkit.JavascriptInterface
        fun openSettings() = runOnUiThread { showSettings() }

        @android.webkit.JavascriptInterface
        fun openLogs() = runOnUiThread { showLogs() }

        @android.webkit.JavascriptInterface
        fun isLoggedIn(): Boolean = TokengineAuth.isLoggedIn(this@MainActivity)

        @android.webkit.JavascriptInterface
        fun relogin() = runOnUiThread { startTokengineLogin() }

        @android.webkit.JavascriptInterface
        fun goToTab(index: Int) = runOnUiThread { switchToTab(index) }

        @android.webkit.JavascriptInterface
        fun theme(): String = JSONObject().apply {
            put("primary", String.format("#%06X", 0xFFFFFF and palette.primary))
            put("bg", String.format("#%06X", 0xFFFFFF and palette.bg))
            put("surface", String.format("#%06X", 0xFFFFFF and palette.surface))
            put("text", String.format("#%06X", 0xFFFFFF and palette.text))
            put("textSub", String.format("#%06X", 0xFFFFFF and palette.textSub))
            put("divider", String.format("#%06X", 0xFFFFFF and palette.divider))
        }.toString()
    }

    // ── 拍照 / 相册 ───────────────────────────────────────────────

    private fun openCamera() {
        if (!ensureCameraPermission()) return
        val dir = File(cacheDir, "photos").apply { mkdirs() }
        val file = File(dir, "photo_" + System.currentTimeMillis() + ".jpg")
        val uri = FileProvider.getUriForFile(this, "$packageName.fileprovider", file)
        cameraPhotoUri = uri
        val intent = Intent(MediaStore.ACTION_IMAGE_CAPTURE).apply {
            putExtra(MediaStore.EXTRA_OUTPUT, uri)
            addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
        }
        if (intent.resolveActivity(packageManager) != null) {
            cameraLauncher.launch(intent)
        } else {
            toast(getString(R.string.camera_unavailable))
        }
    }

    private fun openGallery() {
        val intent = Intent(Intent.ACTION_GET_CONTENT).apply {
            type = "image/*"
            addCategory(Intent.CATEGORY_OPENABLE)
        }
        runCatching { galleryLauncher.launch(Intent.createChooser(intent, getString(R.string.pick_image))) }
            .onFailure { toast(getString(R.string.gallery_unavailable)) }
    }

    /** 图片不通过 evaluateJavascript 传 base64（Binder 有大小限制），落盘后给 H5 一个同源 URL。 */
    private fun deliverImage(uri: Uri) {
        Thread({
            val bytes = runCatching {
                contentResolver.openInputStream(uri)?.use { it.readBytes() }
            }.getOrNull()
            if (bytes == null || bytes.isEmpty()) {
                runOnUiThread { toast(getString(R.string.image_read_failed)) }
                return@Thread
            }
            File(filesDir, "mobile_photo.jpg").writeBytes(bytes)
            runOnUiThread {
                webView.evaluateJavascript(
                    "window.__tb && window.__tb.onPhoto('" + MOBILE_PREFIX +
                        "/photo.jpg?t=" + System.currentTimeMillis() + "')",
                    null,
                )
            }
        }, "deliver-image").apply { isDaemon = true }.start()
    }

    private fun ensureCameraPermission(): Boolean {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) ==
            PackageManager.PERMISSION_GRANTED
        ) return true
        requestPermissions(arrayOf(Manifest.permission.CAMERA), REQ_CAMERA)
        return false
    }

    private fun requestBasePermissions() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQ_NOTIFICATION)
        }
    }

    // ── 设置 / 日志 ───────────────────────────────────────────────

    private fun showSettings() {
        val prefs = Prefs(this)
        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(10), dp(24), dp(10))
        }
        content.addView(sectionLabel(getString(R.string.settings_appearance)))
        content.addView(Button(this).apply {
            text = getString(R.string.settings_theme) + "：" + THEME_NAMES[prefs.themeIndex]
            setAllCaps(false)
            setTextColor(palette.primary)
            setBackgroundColor(Color.TRANSPARENT)
            setOnClickListener { showThemePicker() }
        })

        content.addView(sectionLabel(getString(R.string.settings_account)))
        content.addView(Button(this).apply {
            text = if (TokengineAuth.isLoggedIn(this@MainActivity)) {
                getString(R.string.account_logged_in)
            } else {
                getString(R.string.account_login)
            }
            setAllCaps(false)
            setTextColor(palette.primary)
            setBackgroundColor(Color.TRANSPARENT)
            setOnClickListener { startTokengineLogin() }
        })

        content.addView(sectionLabel(getString(R.string.settings_engine)))
        val runtimeUrl = EditText(this).apply {
            setText(prefs.runtimeUrl)
            hint = getString(R.string.runtime_url_hint)
            inputType = android.text.InputType.TYPE_CLASS_TEXT
            setSingleLine()
            background = GradientDrawable().apply {
                cornerRadius = dp(10).toFloat()
                setColor(palette.bg)
                setStroke(dp(1), palette.divider)
            }
            setPadding(dp(12), dp(10), dp(12), dp(10))
        }
        content.addView(runtimeUrl)
        content.addView(Button(this).apply {
            text = getString(R.string.view_logs)
            setAllCaps(false)
            setTextColor(palette.textSub)
            setBackgroundColor(Color.TRANSPARENT)
            setOnClickListener { showLogs() }
        })

        AlertDialog.Builder(this)
            .setTitle(R.string.settings)
            .setView(ScrollView(this).apply { addView(content) })
            .setPositiveButton(R.string.save) { _, _ ->
                prefs.runtimeUrl = runtimeUrl.text.toString().trim()
            }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    private fun showThemePicker() {
        val prefs = Prefs(this)
        AlertDialog.Builder(this)
            .setTitle(R.string.settings_theme)
            .setSingleChoiceItems(THEME_NAMES, prefs.themeIndex) { dialog, which ->
                prefs.themeIndex = which
                dialog.dismiss()
                recreate()
            }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    private fun showLogs() {
        val log = ProotEngine.logSnapshot().joinToString("\n")
        val body = TextView(this).apply {
            text = log.ifBlank { getString(R.string.logs_empty) }
            textSize = 11f
            setTextColor(palette.text)
            setTextIsSelectable(true)
            setPadding(dp(16), dp(12), dp(16), dp(12))
        }
        AlertDialog.Builder(this)
            .setTitle(R.string.logs)
            .setView(ScrollView(this).apply { addView(body) })
            .setPositiveButton(R.string.close, null)
            .show()
    }

    private fun toast(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show()
    }

    // ── 视图辅助 ──────────────────────────────────────────────────

    private fun sectionLabel(text: String): TextView = TextView(this).apply {
        this.text = text
        textSize = 12f
        setTextColor(palette.textSub)
        setPadding(0, dp(14), 0, dp(4))
    }

    private fun primaryButton(text: String): Button = Button(this).apply {
        this.text = text
        setTextColor(Color.WHITE)
        textSize = 15f
        setAllCaps(false)
        background = GradientDrawable().apply {
            cornerRadius = dp(14).toFloat()
            setColor(palette.primary)
        }
        setPadding(dp(20), dp(14), dp(20), dp(14))
    }

    private fun outlinedButton(text: String): Button = Button(this).apply {
        this.text = text
        setTextColor(palette.primary)
        textSize = 13f
        setAllCaps(false)
        background = GradientDrawable().apply {
            cornerRadius = dp(12).toFloat()
            setColor(palette.surface)
            setStroke(dp(1), palette.divider)
        }
        setPadding(dp(14), dp(10), dp(14), dp(10))
    }

    private fun fullWidthButtonParams(topMargin: Int) = LinearLayout.LayoutParams(
        LinearLayout.LayoutParams.MATCH_PARENT,
        LinearLayout.LayoutParams.WRAP_CONTENT,
    ).apply { this.topMargin = topMargin }

    private fun linearRowParams(left: Int = 0, right: Int = 0) = LinearLayout.LayoutParams(
        0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f,
    ).apply {
        leftMargin = left
        rightMargin = right
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    private fun matchParent() = FrameLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT,
        ViewGroup.LayoutParams.MATCH_PARENT,
    )

    companion object {
        private const val MOBILE_PREFIX = "/__thinkbuddy_mobile__"
        private const val REQ_CAMERA = 2
        private const val REQ_NOTIFICATION = 3

        // dsh-Go 风格的六套主题色 + 跟随系统深浅。
        private val THEME_COLORS = arrayOf(
            "#6366F1", "#0EA5E9", "#10B981", "#F59E0B", "#EC4899", "#8B5CF6",
        )
        private val THEME_NAMES = arrayOf("靛蓝", "天青", "翠绿", "琥珀", "粉樱", "紫罗兰")

        // 只保留用户指定的手机端功能面：对话 / 练习 / 技能 / 专家 / 拍照。
        // 伙伴、子智能体、deep_research、deep_solve、co_writer、MCP、SSH 均不出现。
        private val NAV_TABS = arrayOf(
            "💬" to "对话",
            "📝" to "练习",
            "⚡" to "技能",
            "🎯" to "专家",
            "📷" to "拍照",
        )
    }
}
