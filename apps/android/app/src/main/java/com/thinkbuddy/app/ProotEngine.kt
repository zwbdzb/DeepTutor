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

        val prootArgs = mutableListOf(
            File(runtime, "proot").absolutePath,
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

        val baseEnv = mapOf(
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
        )

        onStatus("正在检查运行环境...")
        ensureExecutable(runtime)
        // 先跑几条最小命令，把问题锁死在「PRoot 二进制」「rootfs」还是「参数/env」
        // 这三层里的哪一层，免得所有失败都报成同一个含糊的「后端进程提前退出」。
        runProbe(runtime, prootArgs, baseEnv)

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

    /**
     * 逐层加参数地跑 PRoot，一次把问题定位到具体一层：
     *   version  —— 只验证静态二进制本身能否被 exec（不碰 rootfs）
     *   sh       —— -r rootfs 跑 /bin/sh（验证 rootfs 与 chdir）
     *   sh-root  —— 再加 -0 假 root
     *   sh-link  —— 再加 --link2symlink / --kill-on-exit（Android 10+ 必需）
     *   full     —— 完整参数 + 解释器 + home/site-packages 检查
     * 任一层失败，报告里会带上每层的 exit code 与输出。
     */
    private fun runProbe(
        runtime: File,
        prootArgs: List<String>,
        environment: Map<String, String>,
    ) {
        val proot = File(runtime, "proot").absolutePath
        val rootfs = File(runtime, "rootfs").absolutePath
        // 决定性实验 1：完全不碰我们的目录，验证 ProcessBuilder 本身能跑
        // 决定性实验 2：把系统的 echo 复制进 App 私有目录再执行，验证「能否从
        //   filesDir 执行二进制」（W^X / ROM 限制）。两条都过而 proot 挂，就是
        //   proot 二进制本身的问题。
        val dataEcho = File(runtime, "probe-echo").absolutePath
        runCatching {
            File("/system/bin/echo").copyTo(File(dataEcho), overwrite = true)
            android.system.Os.chmod(dataEcho, 493) // 0755
        }
        val fullScript = """
            echo PROBE-OK
            /opt/python/bin/python3.12 -V
            test -f /root/boot_backend.py && echo BOOT-OK
            /opt/python/bin/python3.12 -c "import deeptutor, uvicorn; print('IMPORT-OK')"
        """.trimIndent()
        val cases = listOf(
            "sys-echo" to listOf("/system/bin/echo", "SYS-OK"),
            "data-echo" to listOf(dataEcho, "DATA-OK"),
            "version" to listOf(proot, "--version"),
            "sh" to listOf(proot, "-r", rootfs, "/bin/sh", "-c", "echo MIN-OK"),
            "sh-root" to listOf(proot, "-0", "-r", rootfs, "/bin/sh", "-c", "echo ROOT-OK"),
            "sh-link" to listOf(
                proot, "--link2symlink", "--kill-on-exit", "-0",
                "-r", rootfs, "/bin/sh", "-c", "echo LINK-OK",
            ),
            "full" to prootArgs + listOf("/bin/sh", "-c", fullScript),
        )

        val report = StringBuilder()
        val prootMode = runCatching { android.system.Os.stat(proot).st_mode }.getOrDefault(0)
        report.append(
            "proot: ${File(proot).length()}B mode=${Integer.toOctalString(prootMode)}" +
                " canExecute=${File(proot).canExecute()}\n",
        )
        report.append("rootfs/bin/sh: ${File(rootfs, "bin/sh").length()}B\n")
        report.append("data-echo: ${File(dataEcho).length()}B\n")
        var fullOutput: List<String> = emptyList()
        var fullCode = -1
        for ((label, command) in cases) {
            val process = launch(command, environment, "probe-$label")
            val finished = runCatching {
                process.waitFor(60, java.util.concurrent.TimeUnit.SECONDS)
            }.getOrDefault(false)
            if (!finished) {
                process.destroyForcibly()
            }
            Thread.sleep(150)
            val code = if (finished) runCatching { process.exitValue() }.getOrDefault(-1) else -1
            val output = logSnapshot().filter { it.startsWith("[probe-$label]") }
            if (label == "full") {
                fullOutput = output
                fullCode = if (finished) code else -1
            }
            report.append("· $label: ")
                .append(if (finished) "exit=$code" else "超时")
                .append(if (output.isEmpty()) "（无输出）" else "\n  " + output.joinToString("\n  "))
                .append('\n')
        }
        runCatching { File(dataEcho).delete() }
        val passed = fullCode == 0 &&
            fullOutput.any { it.contains("PROBE-OK") } &&
            fullOutput.any { it.contains("BOOT-OK") } &&
            fullOutput.any { it.contains("IMPORT-OK") }
        if (!passed) {
            throw IOException("环境自检失败\n$report")
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
