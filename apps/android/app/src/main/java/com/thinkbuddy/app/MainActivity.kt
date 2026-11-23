package com.thinkbuddy.app

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
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
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.launch

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var overlay: LinearLayout
    private lateinit var statusTitle: TextView
    private lateinit var statusText: TextView
    private lateinit var progress: ProgressBar
    private var loadedUrl: String? = null
    private var fileChooserCallback: ValueCallback<Array<Uri>>? = null

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

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        buildUi()
        requestNotificationPermission()
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) webView.goBack() else finish()
            }
        })
        EngineService.start(this)
        observeEngine()
    }

    override fun onDestroy() {
        webView.destroy()
        super.onDestroy()
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun buildUi() {
        val root = FrameLayout(this)

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
        root.addView(webView, matchParent())

        overlay = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(Color.WHITE)
            setPadding(dp(28), dp(28), dp(28), dp(28))
        }
        statusTitle = TextView(this).apply {
            text = "ThinkBuddy"
            textSize = 26f
            setTextColor(0xFF111111.toInt())
            gravity = Gravity.CENTER
        }
        statusText = TextView(this).apply {
            textSize = 14f
            setTextColor(0xFF666666.toInt())
            gravity = Gravity.CENTER
            setPadding(0, dp(16), 0, dp(16))
        }
        progress = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            max = 100
            isIndeterminate = true
            visibility = View.GONE
        }
        val buttons = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
        }
        buttons.addView(Button(this@MainActivity).apply {
            text = "重试"
            setOnClickListener {
                EngineManager.stop()
                EngineService.stop(this@MainActivity)
                EngineService.start(this@MainActivity)
            }
        })
        buttons.addView(Button(this@MainActivity).apply {
            text = "设置"
            setOnClickListener { showSettings() }
        })

        overlay.addView(statusTitle, wrapContent())
        overlay.addView(statusText, wrapContent())
        overlay.addView(progress, wrapContent())
        overlay.addView(buttons, wrapContent())
        root.addView(overlay, matchParent())
        setContentView(root)
    }

    private fun observeEngine() {
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                EngineManager.status.collect { render(it) }
            }
        }
    }

    private fun render(status: EngineStatus) {
        when (status.phase) {
            Phase.READY -> {
                overlay.visibility = View.GONE
                if (loadedUrl != status.url) {
                    loadedUrl = status.url
                    webView.loadUrl(status.url)
                }
            }
            Phase.INSTALLING -> showOverlay("首次使用需要下载运行时", status.message, status.message)
            Phase.STARTING -> showOverlay("正在启动引擎", status.message, null)
            Phase.ERROR -> {
                val log = ProotEngine.logSnapshot().takeLast(6).joinToString("\n")
                showOverlay(
                    "启动失败",
                    status.message + if (log.isBlank()) "" else "\n\n$log",
                    null,
                )
            }
            Phase.IDLE, Phase.STOPPED -> showOverlay("引擎未启动", "点击「设置」选择引擎来源", null)
        }
    }

    private fun showOverlay(title: String, message: String, progressMessage: String?) {
        overlay.visibility = View.VISIBLE
        statusTitle.text = title
        statusText.text = message
        val percent = progressMessage?.let { Regex("(\\d+)%").find(it)?.groupValues?.get(1)?.toIntOrNull() }
        if (percent != null) {
            progress.visibility = View.VISIBLE
            progress.isIndeterminate = false
            progress.progress = percent
        } else if (progressMessage != null) {
            progress.visibility = View.VISIBLE
            progress.isIndeterminate = true
        } else {
            progress.visibility = View.GONE
        }
    }

    private fun showSettings() {
        val prefs = Prefs(this)
        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(8), dp(24), dp(8))
        }

        val modeGroup = RadioGroup(this).apply { orientation = RadioGroup.VERTICAL }
        val localRadio = RadioButton(this).apply {
            text = "本地引擎（在手机上运行）"
            isChecked = prefs.mode != EngineConfig.MODE_REMOTE
        }
        val remoteRadio = RadioButton(this).apply {
            text = "远程引擎（SSH 连电脑）"
            isChecked = prefs.mode == EngineConfig.MODE_REMOTE
        }
        modeGroup.addView(localRadio)
        modeGroup.addView(remoteRadio)
        content.addView(label("引擎来源"))
        content.addView(modeGroup)

        val runtimeUrl = edit(prefs.runtimeUrl, "运行时下载地址（.zip）")
        val sshHost = edit(prefs.sshHost, "SSH 主机")
        val sshPort = edit(prefs.sshPort.toString(), "SSH 端口")
        val sshUser = edit(prefs.sshUser, "SSH 用户名")
        val sshPassword = edit(prefs.sshPassword, "SSH 密码")
        val sshKeyPath = edit(prefs.sshKeyPath, "SSH 私钥路径（可选）")
        val remotePort = edit(prefs.remoteEnginePort.toString(), "电脑上引擎端口")
        listOf(
            "运行时地址" to runtimeUrl,
            "SSH 主机" to sshHost,
            "SSH 端口" to sshPort,
            "SSH 用户名" to sshUser,
            "SSH 密码" to sshPassword,
            "SSH 私钥路径" to sshKeyPath,
            "远程引擎端口" to remotePort,
        ).forEach { (title, field) ->
            content.addView(label(title))
            content.addView(field)
        }

        AlertDialog.Builder(this)
            .setTitle("设置")
            .setView(ScrollView(this).apply { addView(content) })
            .setPositiveButton("保存并重启引擎") { _, _ ->
                prefs.mode = if (remoteRadio.isChecked) {
                    EngineConfig.MODE_REMOTE
                } else {
                    EngineConfig.MODE_LOCAL
                }
                prefs.runtimeUrl = runtimeUrl.text.toString().trim()
                prefs.sshHost = sshHost.text.toString().trim()
                prefs.sshPort = sshPort.text.toString().trim().toIntOrNull() ?: 22
                prefs.sshUser = sshUser.text.toString().trim()
                prefs.sshPassword = sshPassword.text.toString()
                prefs.sshKeyPath = sshKeyPath.text.toString().trim()
                prefs.remoteEnginePort = remotePort.text.toString().trim().toIntOrNull()
                    ?: BuildConfig.FRONTEND_PORT
                EngineManager.stop()
                EngineService.stop(this)
                loadedUrl = null
                EngineService.start(this)
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun label(text: String): TextView = TextView(this).apply {
        this.text = text
        textSize = 12f
        setTextColor(0xFF888888.toInt())
        setPadding(0, dp(12), 0, dp(4))
    }

    private fun edit(value: String, hint: String): EditText = EditText(this).apply {
        setText(value)
        this.hint = hint
        inputType = android.text.InputType.TYPE_CLASS_TEXT
        setSingleLine()
    }

    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    private fun matchParent() = FrameLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT,
        ViewGroup.LayoutParams.MATCH_PARENT,
    )

    private fun wrapContent() = LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.WRAP_CONTENT,
        ViewGroup.LayoutParams.WRAP_CONTENT,
    ).apply { gravity = Gravity.CENTER_HORIZONTAL }
}
