package com.thinkbuddy.app

import android.content.Context
import android.system.Os
import org.apache.commons.compress.archivers.zip.ZipFile
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/**
 * 手机端运行时的下载 / 校验 / 解压。
 *
 * 运行包结构（由 desktop-shell/tools/build_android_bundle.py 组装）：
 *   python/       python-build-standalone linux-aarch64
 *   node/         node linux-arm64
 *   rootfs/       Debian bookworm arm64（PRoot 的根文件系统）
 *   proot         静态 aarch64 PRoot
 *   android-bundle.json
 *
 * 解压必须用 commons-compress：运行包里有符号链接（python3 -> python3.12、
 * rootfs 里的 /bin -> usr/bin），java.util.zip 读不到 unix 模式位。
 */
object RuntimeInstaller {

    private const val S_IFMT = 0xF000
    private const val S_IFLNK = 0xA000
    private const val S_IFDIR = 0x4000

    fun runtimeDir(context: Context): File = File(context.filesDir, "runtime")

    fun homeDir(context: Context): File = File(context.filesDir, "home")

    fun tmpDir(context: Context): File = File(context.filesDir, "tmp")

    fun isInstalled(context: Context): Boolean {
        val dir = runtimeDir(context)
        return File(dir, "android-bundle.json").isFile &&
            File(dir, "python/bin/python3").exists() &&
            File(dir, "proot").exists() &&
            File(dir, "rootfs/usr/bin/env").exists()
    }

    /**
     * 下载并解压运行时（阻塞调用，请在 IO 线程执行）。返回 deeptutor 版本。
     */
    fun install(
        context: Context,
        url: String,
        expectedSha256: String,
        onProgress: (percent: Int, message: String) -> Unit,
    ): String {
        require(url.isNotBlank()) { "运行时下载地址为空，请先在设置里填写" }
        val cache = File(context.cacheDir, "runtime-install").apply {
            deleteRecursively()
            mkdirs()
        }
        val archive = File(cache, "runtime.zip")

        onProgress(0, "正在下载运行时（首次约 1GB）...")
        download(url, archive) { percent ->
            onProgress(percent * 80 / 100, if (percent in 0..100) "正在下载运行时 $percent%" else "正在下载运行时...")
        }

        if (expectedSha256.isNotBlank()) {
            onProgress(81, "正在校验运行时...")
            val actual = sha256(archive)
            if (!actual.equals(expectedSha256, ignoreCase = true)) {
                archive.delete()
                throw IOException("运行时校验失败：sha256 不匹配")
            }
        }

        onProgress(82, "正在解压运行时（这一步较慢）...")
        val staging = File(context.filesDir, "runtime.new")
        staging.deleteRecursively()
        staging.mkdirs()
        extract(archive, staging)
        tmpDir(context).mkdirs()

        val target = runtimeDir(context)
        target.deleteRecursively()
        if (!staging.renameTo(target)) {
            staging.copyRecursively(target, overwrite = true)
            staging.deleteRecursively()
        }
        archive.delete()
        cache.deleteRecursively()

        val meta = File(target, "android-bundle.json")
        val version = if (meta.isFile) {
            runCatching { JSONObject(meta.readText()).optString("deeptutor_version") }.getOrDefault("")
        } else ""
        onProgress(88, "运行时已就绪")
        return version
    }

    private fun extract(archive: File, root: File) {
        val rootPath = root.canonicalPath
        ZipFile.builder().setFile(archive).get().use { zip ->
            val entries = zip.entries
            while (entries.hasMoreElements()) {
                val entry = entries.nextElement()
                val name = entry.name
                if (name.startsWith("__MACOSX") || name == "/") continue
                val dest = File(root, name)
                val destPath = dest.canonicalPath
                if (destPath != rootPath && !destPath.startsWith(rootPath + File.separator)) {
                    throw IOException("非法路径: $name")
                }
                val mode = entry.unixMode
                val kind = mode and S_IFMT
                if (entry.isDirectory || kind == S_IFDIR || name.endsWith("/")) {
                    dest.mkdirs()
                    applyMode(dest, mode, 0x1ED) // 0755
                    continue
                }
                dest.parentFile?.mkdirs()
                if (kind == S_IFLNK) {
                    val target = zip.getInputStream(entry).use { String(it.readBytes()) }
                    dest.delete()
                    Os.symlink(target, destPath)
                    continue
                }
                zip.getInputStream(entry).use { input ->
                    FileOutputStream(dest).use { output -> input.copyTo(output, 256 * 1024) }
                }
                applyMode(dest, mode, 0x1A4) // 0644
            }
        }
    }

    /** 权限位缺失（0）时回退到 fallback；确保执行位不会丢。 */
    private fun applyMode(file: File, unixMode: Int, fallback: Int) {
        val mode = if (unixMode and 0x1FF != 0) unixMode and 0x1FF else fallback
        try {
            Os.chmod(file.absolutePath, mode)
        } catch (_: Throwable) {
            if (mode and 0x49 != 0) file.setExecutable(true, false)
        }
    }

    private fun download(url: String, dest: File, onProgress: (Int) -> Unit) {
        var current = URL(url)
        var redirects = 0
        while (redirects++ < 6) {
            val conn = (current.openConnection() as HttpURLConnection).apply {
                instanceFollowRedirects = false
                connectTimeout = 30_000
                readTimeout = 60_000
                setRequestProperty("User-Agent", "ThinkBuddy-Android")
            }
            try {
                val code = conn.responseCode
                if (code in 300..399) {
                    val location = conn.getHeaderField("Location")
                        ?: throw IOException("下载重定向缺少 Location")
                    current = URL(current, location)
                    continue
                }
                if (code != HttpURLConnection.HTTP_OK) {
                    throw IOException("下载失败：HTTP $code")
                }
                val total = conn.contentLengthLong
                conn.inputStream.use { input ->
                    FileOutputStream(dest).use { output ->
                        val buffer = ByteArray(256 * 1024)
                        var read = 0L
                        var lastPercent = -2
                        while (true) {
                            val count = input.read(buffer)
                            if (count < 0) break
                            output.write(buffer, 0, count)
                            read += count
                            val percent = if (total > 0) (read * 100 / total).toInt() else -1
                            if (percent != lastPercent) {
                                lastPercent = percent
                                onProgress(percent)
                            }
                        }
                    }
                }
                return
            } finally {
                conn.disconnect()
            }
        }
        throw IOException("下载重定向次数过多")
    }

    private fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(1024 * 1024)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                digest.update(buffer, 0, count)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }
}
