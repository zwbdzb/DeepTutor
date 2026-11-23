package com.thinkbuddy.app

import android.content.Context
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

enum class Phase { IDLE, INSTALLING, STARTING, READY, ERROR, STOPPED }

data class EngineStatus(
    val phase: Phase,
    val message: String = "",
    val url: String = "",
)

/**
 * 引擎生命周期：本地（下载运行时 → PRoot 启动）或远程（SSH 端口转发）。
 * 由 EngineService 驱动，MainActivity 只订阅状态。
 */
object EngineManager {

    private val _status = MutableStateFlow(EngineStatus(Phase.IDLE, "未启动"))
    val status: StateFlow<EngineStatus> = _status

    private var job: Job? = null
    private var remote: RemoteEngine? = null

    fun start(context: Context, scope: CoroutineScope) {
        if (job?.isActive == true) return
        val appContext = context.applicationContext
        job = scope.launch(Dispatchers.IO) {
            val config = Prefs(appContext).config()
            try {
                if (config.mode == EngineConfig.MODE_REMOTE) {
                    startRemote(appContext, config)
                } else {
                    startLocal(appContext, config)
                }
            } catch (exc: Throwable) {
                ProotEngine.stop()
                remote?.close()
                remote = null
                val detail = exc.message ?: exc.javaClass.simpleName
                _status.value = EngineStatus(Phase.ERROR, detail)
            }
        }
    }

    fun stop() {
        job?.cancel()
        job = null
        ProotEngine.stop()
        remote?.close()
        remote = null
        _status.value = EngineStatus(Phase.STOPPED, "已停止")
    }

    private fun startLocal(context: Context, config: EngineConfig) {
        if (!RuntimeInstaller.isInstalled(context)) {
            _status.value = EngineStatus(Phase.INSTALLING, "准备安装运行时...")
            // 完全离线包：运行时在 APK assets 里，首次启动直接解压；否则按地址下载。
            val version = if (BuildConfig.BUNDLED_RUNTIME) {
                RuntimeInstaller.installFromAssets(context) { _, message ->
                    _status.value = EngineStatus(Phase.INSTALLING, message)
                }
            } else {
                val url = config.runtimeUrl.ifBlank { BuildConfig.RUNTIME_URL }
                // 只在用 CI 内置地址时校验 sha256；用户手填的地址（自建镜像/本地调试）
                // 没有对应摘要，硬套 BuildConfig 里的值只会把安装卡在"校验失败"。
                val expectedSha = if (url == BuildConfig.RUNTIME_URL) {
                    BuildConfig.RUNTIME_SHA256
                } else {
                    ""
                }
                RuntimeInstaller.install(context, url, expectedSha) { _, message ->
                    _status.value = EngineStatus(Phase.INSTALLING, message)
                }
            }
            Prefs(context).installedVersion = version
        }

        _status.value = EngineStatus(Phase.STARTING, "正在启动本地引擎...")
        ProotEngine.start(
            context,
            BuildConfig.BACKEND_PORT,
            BuildConfig.FRONTEND_PORT,
        ) { message ->
            _status.value = EngineStatus(Phase.STARTING, message)
        }
        _status.value = EngineStatus(
            Phase.READY,
            "本地引擎已就绪",
            "http://127.0.0.1:${BuildConfig.FRONTEND_PORT}",
        )
    }

    private fun startRemote(context: Context, config: EngineConfig) {
        _status.value = EngineStatus(Phase.STARTING, "正在连接电脑引擎...")
        val engine = RemoteEngine()
        val localPort = engine.connect(config)
        remote = engine
        _status.value = EngineStatus(
            Phase.READY,
            "已连接电脑引擎（${config.sshHost}）",
            "http://127.0.0.1:$localPort",
        )
    }
}
