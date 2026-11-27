package com.thinkbuddy.app

import android.content.Context
import kotlinx.coroutines.CancellationException
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

    /**
     * 运行代号。stop() 会自增，让「上一轮还在阻塞里跑」的协程无法再写状态。
     *
     * 为什么需要：ProotEngine.start 里全是 Thread.sleep/端口轮询这类阻塞调用，
     * 协程被 cancel 不会立刻中断它们。旧实现里重启引擎时，旧协程会在 waitForPort
     * 抛 IOException 后把状态改成 ERROR，把新实例的「正在启动」盖掉；如果新实例
     * 又因为 `job?.isActive == true` 提前返回，就变成「点了重试没反应」。
     */
    @Volatile
    private var runToken = 0

    fun start(context: Context, scope: CoroutineScope) {
        if (job?.isActive == true) return
        val token = ++runToken
        val appContext = context.applicationContext
        job = scope.launch(Dispatchers.IO) {
            val publish: (EngineStatus) -> Unit = { status ->
                if (token == runToken) _status.value = status
            }
            try {
                startLocal(appContext, publish)
            } catch (exc: CancellationException) {
                throw exc
            } catch (exc: Throwable) {
                ProotEngine.stop()
                val detail = exc.message ?: exc.javaClass.simpleName
                val logFile = ProotEngine.persistLog(appContext)
                publish(
                    EngineStatus(
                        Phase.ERROR,
                        detail + if (logFile != null) "\n\n日志已保存：${logFile.absolutePath}" else "",
                    ),
                )
            }
        }
    }

    fun stop() {
        runToken++
        job?.cancel()
        job = null
        ProotEngine.stop()
        _status.value = EngineStatus(Phase.STOPPED, "已停止")
    }

    private fun startLocal(context: Context, publish: (EngineStatus) -> Unit) {
        if (!RuntimeInstaller.isInstalled(context)) {
            publish(EngineStatus(Phase.INSTALLING, "准备安装运行时..."))
            val version = if (BuildConfig.BUNDLED_RUNTIME) {
                RuntimeInstaller.installFromAssets(context) { _, message ->
                    publish(EngineStatus(Phase.INSTALLING, message))
                }
            } else {
                val url = Prefs(context).runtimeUrl.ifBlank { BuildConfig.RUNTIME_URL }
                val expectedSha = if (url == BuildConfig.RUNTIME_URL) {
                    BuildConfig.RUNTIME_SHA256
                } else {
                    ""
                }
                RuntimeInstaller.install(context, url, expectedSha) { _, message ->
                    publish(EngineStatus(Phase.INSTALLING, message))
                }
            }
            Prefs(context).installedVersion = version
        }

        publish(EngineStatus(Phase.STARTING, "正在启动本地引擎..."))
        ProotEngine.start(
            context,
            BuildConfig.BACKEND_PORT,
            BuildConfig.FRONTEND_PORT,
        ) { message ->
            publish(EngineStatus(Phase.STARTING, message))
        }
        publish(
            EngineStatus(
                Phase.READY,
                "本地引擎已就绪",
                "http://127.0.0.1:${BuildConfig.FRONTEND_PORT}",
            ),
        )
    }
}
