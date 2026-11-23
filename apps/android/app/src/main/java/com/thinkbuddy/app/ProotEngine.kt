package com.thinkbuddy.app

import android.content.Context
import java.io.File
import java.io.IOException
import java.net.InetSocketAddress
import java.net.Socket

/**
 * 在 PRoot 的 Debian rootfs 里拉起后端（python/uvicorn）与前端（Next.js
 * standalone server.js）。
 *
 * Android 是 bionic libc，glibc 的 python/node 不能直接执行，所以整条链路
 * 跑在 Debian rootfs 里；宿主的 runtime 目录通过 -b 挂到 /opt/python、
 * /opt/node、/root/.deeptutor。
 */
object ProotEngine {

    private const val MAX_LOG_LINES = 200
    private val logs = ArrayDeque<String>()
    private var backend: Process? = null
    private var frontend: Process? = null

    fun isRunning(): Boolean = backend?.isAlive == true

    fun logSnapshot(): List<String> = synchronized(logs) { logs.toList() }

    fun start(
        context: Context,
        backendPort: Int,
        frontendPort: Int,
        onStatus: (String) -> Unit,
    ) {
        if (isRunning()) return
        val runtime = RuntimeInstaller.runtimeDir(context)
        // 老安装包的 rootfs 里没有 opt/python、opt/node；不补的话 PRoot 会丢弃
        // 这两条绑定，python/node 就完全找不到（升级用户最容易踩到）。
        RuntimeInstaller.ensureMountPoints(runtime)
        val home = RuntimeInstaller.homeDir(context).apply { mkdirs() }
        val tmp = RuntimeInstaller.tmpDir(context).apply { mkdirs() }

        writeBootScript(home, backendPort)

        ensureExecutable(runtime)
        onStatus("正在检查运行环境...")
        // 运行包里同时带 Termux（bionic）与静态 glibc 两个 proot：谁能真正拉起
        // guest 就用谁，失败的一方也留在报告里，便于定位。
        val selection = selectProot(runtime, home, tmp)
        val prootArgs = selection.args
        val baseEnv = selection.env
        runProbe(prootArgs, baseEnv)

        onStatus("正在启动本地引擎...")
        backend = launch(
            prootArgs + listOf(
                "/opt/python/bin/python3.12",
                "/root/boot_backend.py",
            ),
            baseEnv + mapOf("BACKEND_PORT" to backendPort.toString()),
            "backend",
        )
        waitForPort(backendPort, backend, 240_000, "后端")

        onStatus("正在启动界面...")
        frontend = launch(
            prootArgs + listOf(
                "/opt/node/bin/node",
                "/opt/python/lib/python3.12/site-packages/deeptutor_web/server.js",
            ),
            baseEnv + mapOf(
                "PORT" to frontendPort.toString(),
                "HOSTNAME" to "127.0.0.1",
                "DEEPTUTOR_API_BASE_URL" to "http://127.0.0.1:$backendPort",
                "DEEPTUTOR_AUTH_ENABLED" to "false",
                "NEXT_TELEMETRY_DISABLED" to "1",
            ),
            "frontend",
        )
        waitForPort(frontendPort, frontend, 180_000, "界面")
    }

    fun stop() {
        listOf(frontend, backend).forEach { process ->
            if (process == null) return@forEach
            // --kill-on-exit 让 PRoot 退出时带走被 trace 的子进程。
            process.destroy()
            try {
                if (!process.waitFor(3, java.util.concurrent.TimeUnit.SECONDS)) {
                    process.destroyForcibly()
                }
            } catch (_: InterruptedException) {
                process.destroyForcibly()
                Thread.currentThread().interrupt()
            }
        }
        frontend = null
        backend = null
    }

    private fun launch(
        command: List<String>,
        environment: Map<String, String>,
        tag: String,
    ): Process {
        val builder = ProcessBuilder(command)
        builder.directory(File("/"))
        builder.redirectErrorStream(true)
        builder.environment().clear()
        builder.environment().putAll(environment)
        val process = try {
            builder.start()
        } catch (exc: IOException) {
            throw IOException("无法启动 $tag 进程：${exc.message}", exc)
        }
        appendLog("[cmd] ${command.joinToString(" ")}")
        Thread({ pump(process, tag) }, "engine-$tag").apply { isDaemon = true }.start()
        return process
    }

    private fun pump(process: Process, tag: String) {
        try {
            process.inputStream.bufferedReader().useLines { lines ->
                lines.forEach { line -> appendLog("[$tag] $line") }
            }
        } catch (_: IOException) {
            // 进程结束时流会被关闭，忽略。
        }
    }

    private fun appendLog(line: String) {
        synchronized(logs) {
            logs.addLast(line)
            while (logs.size > MAX_LOG_LINES) logs.removeFirst()
        }
        android.util.Log.d("ThinkBuddy", line)
    }

    private fun waitForPort(port: Int, process: Process?, timeoutMs: Long, label: String) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (process != null && !process.isAlive) {
                // 进程刚退出时日志泵线程可能还没读完最后几行；直接取快照会拿到
                // 空日志（界面上只剩"提前退出："）。先 waitFor 再给它一点时间收尾。
                val code = runCatching { process.waitFor() }.getOrDefault(-1)
                Thread.sleep(400)
                throw IOException("$label 进程提前退出（exit=$code）：\n" + tailLog(30))
            }
            try {
                Socket().use { socket ->
                    socket.connect(InetSocketAddress("127.0.0.1", port), 500)
                    return
                }
            } catch (_: IOException) {
                Thread.sleep(500)
            }
        }
        throw IOException("$label 启动超时（端口 $port）")
    }

    private class ProotVariant(
        val label: String,
        val executable: File,
        val extraEnv: Map<String, String>,
    )

    private class ProotSelection(val args: List<String>, val env: Map<String, String>)

    /**
     * 运行包里可能同时带 Termux 的 bionic proot 与 proot-me 的静态 glibc proot。
     * Termux 版用外部 loader（PROOT_LOADER）+ 自带 libtalloc/libandroid-shmem，
     * 是 Android 上被 proot-distro 长期验证的组合；静态版作为兜底。
     */
    private fun prootVariants(runtime: File): List<ProotVariant> {
        val variants = mutableListOf<ProotVariant>()
        val termux = File(runtime, "proot-termux")
        if (File(termux, "bin/proot").isFile) {
            variants += ProotVariant(
                "termux",
                File(termux, "bin/proot"),
                mapOf(
                    "LD_LIBRARY_PATH" to File(termux, "lib").absolutePath,
                    "PROOT_LOADER" to File(termux, "libexec/proot/loader").absolutePath,
                ),
            )
        }
        val static = File(runtime, "proot")
        if (static.isFile) {
            variants += ProotVariant("static", static, emptyMap())
        }
        if (variants.isEmpty()) {
            throw IOException("运行包里没有 proot（proot-termux/bin/proot 与 proot 都不存在）")
        }
        return variants
    }

    private fun prootArgs(
        variant: ProotVariant,
        runtime: File,
        home: File,
        tmp: File,
    ): List<String> = listOf(
        variant.executable.absolutePath,
        "--link2symlink",
        "--kill-on-exit",
        "-0",
        "-r", File(runtime, "rootfs").absolutePath,
        "-b", "/dev",
        "-b", "/proc",
        "-b", "/sys",
        "-b", "${File(runtime, "python").absolutePath}:/opt/python",
        "-b", "${File(runtime, "node").absolutePath}:/opt/node",
        // 直接挂在 /root 上：PRoot 的 bind 目标必须在 rootfs 里已存在，
        // /root/.deeptutor 这种新目录不存在会导致挂载失败。
        "-b", "${home.absolutePath}:/root",
        "-b", "${tmp.absolutePath}:/tmp",
        "-w", "/root",
    )

    private fun prootEnv(variant: ProotVariant, tmp: File): Map<String, String> =
        mapOf(
            "PROOT_NO_SECCOMP" to "1",
            "PROOT_TMP_DIR" to tmp.absolutePath,
            "HOME" to "/root",
            "TMPDIR" to "/tmp",
            // 进程环境被整体清空；不补 PATH 的话，guest 里任何 subprocess
            // （exec 工具、git、ffmpeg）都会直接 "No such file"。
            "PATH" to "/opt/python/bin:/opt/node/bin:/usr/local/sbin:/usr/local/bin:" +
                "/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG" to "C.UTF-8",
            "LC_ALL" to "C.UTF-8",
            "PYTHONUNBUFFERED" to "1",
            "PYTHONIOENCODING" to "utf-8:replace",
            // 手机端只监听回环，登录/鉴权没有意义（与桌面壳的相对路径代理保持一致）。
            "DEEPTUTOR_AUTH_ENABLED" to "false",
            "DEEPTUTOR_HOME" to "/root",
        ) + variant.extraEnv

    /**
     * 用 `sh -c "echo MIN-OK"` 逐个子试：能真正拉起 guest 的那一个才用于后端/前端。
     * 失败的一方（典型是静态 proot 的 exit=255 无输出）也会写进报告。
     */
    private fun selectProot(runtime: File, home: File, tmp: File): ProotSelection {
        val report = StringBuilder()
        for (variant in prootVariants(runtime)) {
            val args = prootArgs(variant, runtime, home, tmp)
            val env = prootEnv(variant, tmp)
            val process = launch(
                args + listOf("/bin/sh", "-c", "echo MIN-OK"),
                env,
                "min-${variant.label}",
            )
            val finished = runCatching {
                process.waitFor(30, java.util.concurrent.TimeUnit.SECONDS)
            }.getOrDefault(false)
            if (!finished) {
                process.destroyForcibly()
            }
            Thread.sleep(150)
            val code = if (finished) runCatching { process.exitValue() }.getOrDefault(-1) else -1
            val output = logSnapshot().filter { it.startsWith("[min-${variant.label}]") }
            report.append("· ${variant.label}: ")
                .append(if (finished) "exit=$code" else "超时")
                .append(if (output.isEmpty()) "（无输出）" else " " + output.joinToString(" | "))
                .append('\n')
            if (finished && code == 0 && output.any { it.contains("MIN-OK") }) {
                appendLog("[proot] 使用 ${variant.label} 版")
                return ProotSelection(args, env)
            }
        }
        val details = prootVariants(runtime).joinToString("\n") { variant ->
            val mode = runCatching {
                android.system.Os.stat(variant.executable.absolutePath).st_mode
            }.getOrDefault(0)
            "  - ${variant.label}: ${variant.executable.length()}B " +
                "mode=${Integer.toOctalString(mode)}"
        }
        throw IOException("PRoot 无法启动 guest（两种实现都失败）\n$report$details")
    }

    /** 选定 proot 后再验一层：解释器、home 绑定和 site-packages。 */
    private fun runProbe(prootArgs: List<String>, environment: Map<String, String>) {
        val script = """
            echo PROBE-OK
            /opt/python/bin/python3.12 -V
            test -f /root/boot_backend.py && echo BOOT-OK
            /opt/python/bin/python3.12 -c "import deeptutor, uvicorn; print('IMPORT-OK')"
        """.trimIndent()
        val process = launch(prootArgs + listOf("/bin/sh", "-c", script), environment, "probe")
        val finished = runCatching {
            process.waitFor(180, java.util.concurrent.TimeUnit.SECONDS)
        }.getOrDefault(false)
        if (!finished) {
            process.destroyForcibly()
            throw IOException("环境自检超时（180s）\n" + tailLog(30))
        }
        val code = process.exitValue()
        Thread.sleep(200)
        val output = logSnapshot().filter { it.startsWith("[probe]") }
        if (code != 0) {
            throw IOException("环境自检失败（exit=$code）\n" + output.joinToString("\n"))
        }
        listOf("PROBE-OK", "BOOT-OK", "IMPORT-OK").forEach { marker ->
            if (output.none { it.contains(marker) }) {
                throw IOException("环境自检缺少 $marker\n" + output.joinToString("\n"))
            }
        }
    }

    /**
     * 解压后权限位理论上已由 RuntimeInstaller 恢复，但 exec 失败时会表现为
     * Android 侧静默退出（exit=255、无任何输出），所以启动前再把关键二进制
     * 的 0755 枪一遍，顺带在自检报告里读回真实 mode。
     */
    private fun ensureExecutable(runtime: File) {
        listOf(
            "proot",
            "proot-termux/bin/proot",
            "proot-termux/libexec/proot/loader",
            "proot-termux/lib/libtalloc.so.2",
            "proot-termux/lib/libandroid-shmem.so",
            "rootfs/bin/sh",
            "rootfs/usr/bin/env",
            "rootfs/usr/bin/dash",
            "python/bin/python3.12",
            "node/bin/node",
        ).forEach { relative ->
            val file = File(runtime, relative)
            if (file.exists()) {
                runCatching { android.system.Os.chmod(file.absolutePath, 493) } // 0755
            }
        }
    }

    private fun tailLog(lines: Int): String {
        val snapshot = logSnapshot().takeLast(lines)
        return if (snapshot.isEmpty()) "（进程没有产生任何输出）" else snapshot.joinToString("\n")
    }

    /** 把最近的引擎日志落盘，方便用户导出/截图。返回可分享的路径（尽力而为）。 */
    fun persistLog(context: Context): File? = runCatching {
        val dir = context.getExternalFilesDir("logs") ?: File(context.filesDir, "logs")
        dir.mkdirs()
        val file = File(dir, "engine.log")
        file.writeText(tailLog(MAX_LOG_LINES))
        file
    }.getOrNull()

    /**
     * 后端启动脚本：桌面端的 launcher 会做用户目录/设置初始化，这里直接跑
     * uvicorn 就必须自己补上，否则首启时 settings 文件缺失。
     */
    private fun writeBootScript(home: File, backendPort: Int) {
        val script = """
            import os

            from deeptutor.runtime.home import get_runtime_home
            from deeptutor.services.config import ensure_runtime_settings_files
            from deeptutor.services.setup import init_user_directories


            def main() -> None:
                home = get_runtime_home()
                init_user_directories(home)
                ensure_runtime_settings_files()

                import uvicorn

                uvicorn.run(
                    "deeptutor.api.main:app",
                    host="127.0.0.1",
                    port=int(os.environ.get("BACKEND_PORT", "$backendPort")),
                    log_level="info",
                    access_log=False,
                )


            if __name__ == "__main__":
                main()
        """.trimIndent()
        File(home, "boot_backend.py").writeText(script + "\n")
    }
}
