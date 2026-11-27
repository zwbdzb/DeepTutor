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
 * 引擎生命周期：仅本地模式（PRoot 里跑 python + node）。
 * 由 EngineService 驱动，MainActivity 只订阅状态。
 */
object EngineManager {

    private val _status = MutableStateFlow(EngineStatus(Phase.IDLE, "未启动"))
    val status: StateFlow<EngineStatus> = _status

    private var job: Job? = null

    fun start(context: Context, scope: CoroutineScope) {
        if (job?.isActive == true) return
        val appContext = context.applicationContext
        job = scope.launch(Dispatchers.IO) {
            try {
                startLocal(appContext)
            } catch (exc: Throwable) {
                ProotEngine.stop()
                val detail = exc.message ?: exc.javaClass.simpleName
                val logFile = ProotEngine.persistLog(appContext)
                _status.value = EngineStatus(
                    Phase.ERROR,
                    detail + if (logFile != null) "\n\n日志已保存：${logFile.absolutePath}" else "",
                )
            }
        }
    }

    fun stop() {
        job?.cancel()
        job = null
        ProotEngine.stop()
        _status.value = EngineStatus(Phase.STOPPED, "已停止")
    }

    private fun startLocal(context: Context) {
        if (!RuntimeInstaller.isInstalled(context)) {
            _status.value = EngineStatus(Phase.INSTALLING, "准备安装运行时...")
            val version = if (BuildConfig.BUNDLED_RUNTIME) {
                RuntimeInstaller.installFromAssets(context) { _, message ->
                    _status.value = EngineStatus(Phase.INSTALLING, message)
                }
            } else {
                val config = Prefs(context).config()
                val url = config.runtimeUrl.ifBlank { BuildConfig.RUNTIME_URL }
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
}
