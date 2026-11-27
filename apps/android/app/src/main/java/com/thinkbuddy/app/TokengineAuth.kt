package com.thinkbuddy.app

import android.content.Context
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.File
import java.io.InputStreamReader
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketTimeoutException
import java.net.URL
import java.net.URLDecoder
import java.net.URLEncoder
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.security.SecureRandom

/**
 * Tokengine 原生登录（OAuth 2.0 Authorization Code + PKCE S256，回环回调）。
 *
 * 与旧的 [ProotEngine] 登录路径的区别：**完全不依赖 Python / PRoot 运行时**。
 * 因此它可以在「下载 / 解压 1GB 运行时」之前完成，满足手机端首启时序：
 *
 *   冷启动 → 检查登录态 → 未登录则停在登录门（不解压）→ 登录成功 → 才启动引擎
 *
 * 登录产物写成一份种子文件 ``<home>/tokengine_seed.json``；PRoot 把 ``<home>``
 * 挂到 guest 的 ``/root``（``DEEPTUTOR_HOME=/root``），后端启动脚本
 * ``boot_backend.py`` 会读取它并调用 ``ensure_tokengine_catalog`` 走后端自己的
 * 规范化逻辑写入 model_catalog.json。这样 catalog 的字段名 / 分流规则始终由
 * 后端保证，Kotlin 侧不复制一套易错的结构。
 *
 * 协议契约与 ``deeptutor/services/config/tokengine_login.py``、桌面端
 * ``desktop-shell/desktop/auth/`` 保持一致：client_id ``edubuddy-desktop``、
 * scope ``openid relay``、回调路径 ``/authorize``、业务令牌只取 userinfo 的
 * ``ai_token``、模型名单唯一来源是中继 ``GET /v1/models``。
 */
object TokengineAuth {

    private const val OAUTH_BASE = "https://tokengine.hanyoai.com"
    private const val CLIENT_ID = "edubuddy-desktop"
    private const val SCOPE = "openid relay"
    private const val CALLBACK_PATH = "/authorize"
    private const val LOGIN_TIMEOUT_MS = 15 * 60 * 1000L
    private const val HTTP_TIMEOUT_MS = 20_000

    const val SEED_FILE = "tokengine_seed.json"
    private const val MACHINE_ID_FILE = "tokengine_machine_id.txt"

    private val RELAY_FIELDS = arrayOf(
        "relay_base", "base_url", "api_base", "api_endpoint",
        "endpoint", "relay_url", "domain",
    )

    data class Result(val ok: Boolean, val message: String, val models: Int = 0)

    @Volatile
    private var serverSocket: ServerSocket? = null

    @Volatile
    private var cancelled = false

    fun seedFile(context: Context): File =
        File(RuntimeInstaller.homeDir(context), SEED_FILE)

    /**
     * 已登录判定：种子文件存在（本次登录刚完成、引擎尚未消费）或 catalog 里
     * 已经有带 api_key + 模型的 llm profile（引擎已消费过种子）。
     */
    fun isLoggedIn(context: Context): Boolean {
        if (seedFile(context).isFile) return true
        return hasConfiguredModel(context)
    }

    private fun hasConfiguredModel(context: Context): Boolean = runCatching {
        val catalog = File(
            RuntimeInstaller.homeDir(context),
            "data/user/settings/model_catalog.json",
        )
        if (!catalog.isFile) return@runCatching false
        val profiles = JSONObject(catalog.readText())
            .optJSONObject("services")
            ?.optJSONObject("llm")
            ?.optJSONArray("profiles") ?: return@runCatching false
        (0 until profiles.length()).any { index ->
            val profile = profiles.optJSONObject(index) ?: return@any false
            val apiKey = profile.optString("api_key").trim()
            val count = profile.optJSONArray("models")?.length() ?: 0
            apiKey.isNotEmpty() && count > 0
        }
    }.getOrDefault(false)

    /** 取消进行中的登录（关闭回环监听，让等待线程退出）。 */
    fun cancel() {
        cancelled = true
        runCatching { serverSocket?.close() }
    }

    /**
     * 启动一次登录。回调均在后台线程发出，调用方需要自行切到主线程更新 UI。
     *
     * @param onAuthorize 拿到授权页 URL，调用方用系统浏览器打开
     * @param onResult    成功 / 失败；成功时 message 为中继地址，models 为可用模型数
     */
    fun login(
        context: Context,
        onAuthorize: (String) -> Unit,
        onResult: (Result) -> Unit,
    ) {
        cancel()
        cancelled = false
        val appContext = context.applicationContext
        Thread({
            var socket: ServerSocket? = null
            try {
                socket = ServerSocket(0, 16, InetAddress.getByName("127.0.0.1")).apply {
                    soTimeout = 5_000
                }
                serverSocket = socket
                val port = socket.localPort
                val redirectUri = "http://127.0.0.1:$port$CALLBACK_PATH"

                val state = randomHex(24)
                val verifier = randomHex(64)
                val challenge = b64url(sha256(verifier.toByteArray(StandardCharsets.US_ASCII)))
                val machine = machineId(appContext)

                val params = linkedMapOf(
                    "client_id" to CLIENT_ID,
                    "response_type" to "code",
                    "redirect_uri" to redirectUri,
                    "scope" to SCOPE,
                    "state" to state,
                    "code_challenge" to challenge,
                    "code_challenge_method" to "S256",
                    "auth_type" to "local",
                    "login_channel" to "native_desktop",
                    "machine_id" to machine,
                    "x_machine_id" to machine,
                )
                onAuthorize("$OAUTH_BASE/oauth/authorize?" + formEncode(params))

                val code = awaitCallback(socket, state)
                val tokens = postForm(
                    "$OAUTH_BASE/oauth/token",
                    linkedMapOf(
                        "grant_type" to "authorization_code",
                        "code" to code,
                        "redirect_uri" to redirectUri,
                        "client_id" to CLIENT_ID,
                        "code_verifier" to verifier,
                        "machine_id" to machine,
                    ),
                )
                val accessToken = unwrap(tokens).optString("access_token").trim()
                if (accessToken.isEmpty()) {
                    throw IllegalStateException("平台未返回 access_token")
                }

                val account = unwrap(JSONObject(getJson("$OAUTH_BASE/oauth/userinfo", accessToken)))
                val aiToken = account.optString("ai_token").trim()
                if (aiToken.isEmpty()) {
                    throw IllegalStateException("平台未返回业务令牌（ai_token）")
                }

                val relayBase = normalizeRelay(firstRelay(account)) ?: "$OAUTH_BASE/v1"
                // 中继可能返回 {"data":[...]}，也可能直接返回数组（Python 侧两种都支持）。
                val rawModels = getJson("$relayBase/models", aiToken).trim()
                val modelsPayload = if (rawModels.startsWith("[")) {
                    JSONObject().put("data", JSONArray(rawModels))
                } else {
                    JSONObject(rawModels)
                }
                val (models, types, windows) = parseModels(modelsPayload)
                if (models.isEmpty()) throw IllegalStateException("模型列表为空")

                writeSeed(appContext, aiToken, relayBase, models, types, windows, account.optString("phone"))
                Prefs(appContext).tokengineLoggedIn = true
                onResult(Result(true, relayBase, models.size))
            } catch (exc: Throwable) {
                if (!cancelled) {
                    onResult(Result(false, exc.message ?: exc.toString()))
                }
            } finally {
                runCatching { socket?.close() }
                serverSocket = null
            }
        }, "tokengine-native-login").apply { isDaemon = true }.start()
    }

    // ── OAuth 回环回调 ────────────────────────────────────────────

    /** 等待浏览器把 code 送回 127.0.0.1；校验 state、返回一次性 code。 */
    private fun awaitCallback(server: ServerSocket, expectedState: String): String {
        val deadline = System.currentTimeMillis() + LOGIN_TIMEOUT_MS
        while (!cancelled && System.currentTimeMillis() < deadline) {
            val client: Socket = try {
                server.accept()
            } catch (_: SocketTimeoutException) {
                continue
            }
            try {
                client.soTimeout = HTTP_TIMEOUT_MS
                val reader = BufferedReader(
                    InputStreamReader(client.getInputStream(), StandardCharsets.US_ASCII),
                )
                val requestLine = reader.readLine() ?: continue
                // 消费掉剩余请求头（不读 body：OAuth 回调是 GET）。
                while (true) {
                    val header = reader.readLine() ?: break
                    if (header.isEmpty()) break
                }
                val target = requestLine.split(" ").getOrNull(1).orEmpty()
                val query = target.substringAfter("?", "")
                val values = parseQuery(query)

                val error = values["error"]
                val ok = error == null &&
                    values["state"] == expectedState &&
                    !values["code"].isNullOrEmpty() &&
                    target.substringBefore("?") == CALLBACK_PATH

                respond(
                    client,
                    if (ok) "登录成功，可以返回 ThinkBuddy。" else "登录失败，请回到 App 重试。",
                )
                when {
                    error != null -> throw IllegalStateException("平台返回错误：$error")
                    target.substringBefore("?") != CALLBACK_PATH -> continue
                    values["state"] != expectedState -> throw IllegalStateException("回调 state 不匹配")
                    values["code"].isNullOrEmpty() -> throw IllegalStateException("回调缺少 code")
                    else -> return values["code"]!!
                }
            } finally {
                runCatching { client.close() }
            }
        }
        throw IllegalStateException(if (cancelled) "登录已取消" else "授权超时（15 分钟）")
    }

    private fun respond(socket: Socket, message: String) {
        val html = (
            "<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>" +
                "<title>ThinkBuddy</title><body style='font-family:system-ui;padding:48px;" +
                "text-align:center'><h2 style='font-weight:600'>" + message + "</h2></body>"
            ).toByteArray(StandardCharsets.UTF_8)
        val head = (
            "HTTP/1.1 200 OK\r\n" +
                "Content-Type: text/html; charset=utf-8\r\n" +
                "Content-Length: ${html.size}\r\n" +
                "Connection: close\r\n\r\n"
            ).toByteArray(StandardCharsets.US_ASCII)
        val out: OutputStream = socket.getOutputStream()
        out.write(head)
        out.write(html)
        out.flush()
    }

    // ── HTTP ──────────────────────────────────────────────────────

    private fun postForm(url: String, form: Map<String, String>): JSONObject {
        val conn = (URL(url).openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            connectTimeout = HTTP_TIMEOUT_MS
            readTimeout = HTTP_TIMEOUT_MS
            doOutput = true
            setRequestProperty("Content-Type", "application/x-www-form-urlencoded")
            setRequestProperty("Accept", "application/json")
        }
        try {
            conn.outputStream.use {
                it.write(formEncode(form).toByteArray(StandardCharsets.UTF_8))
            }
            return readJson(conn)
        } finally {
            conn.disconnect()
        }
    }

    private fun getJson(url: String, token: String): String {
        val conn = (URL(url).openConnection() as HttpURLConnection).apply {
            requestMethod = "GET"
            connectTimeout = HTTP_TIMEOUT_MS
            readTimeout = HTTP_TIMEOUT_MS
            setRequestProperty("Authorization", "Bearer $token")
            setRequestProperty("Accept", "application/json")
        }
        try {
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            val text = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
            if (code !in 200..299) {
                throw IllegalStateException("平台返回 HTTP $code ${text.take(200)}")
            }
            return text
        } finally {
            conn.disconnect()
        }
    }

    private fun readJson(conn: HttpURLConnection): JSONObject {
        val code = conn.responseCode
        val stream = if (code in 200..299) conn.inputStream else conn.errorStream
        val text = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
        if (code !in 200..299) {
            throw IllegalStateException("平台返回 HTTP $code ${text.take(200)}")
        }
        return if (text.isBlank()) JSONObject() else JSONObject(text)
    }

    private fun formEncode(values: Map<String, String>): String =
        values.entries.joinToString("&") { (key, value) ->
            URLEncoder.encode(key, "UTF-8") + "=" + URLEncoder.encode(value, "UTF-8")
        }

    private fun parseQuery(query: String): Map<String, String> =
        query.split("&").mapNotNull { pair ->
            val index = pair.indexOf('=')
            if (index < 0) {
                null
            } else {
                URLDecoder.decode(pair.substring(0, index), "UTF-8") to
                    URLDecoder.decode(pair.substring(index + 1), "UTF-8")
            }
        }.toMap()

    /** 网关常见 ``{"success":true,"data":{...}}`` 外壳；含令牌的对象直接返回。 */
    private fun unwrap(payload: JSONObject): JSONObject {
        if (payload.has("access_token") || payload.has("ai_token")) return payload
        return payload.optJSONObject("data") ?: payload
    }

    // ── 数据整形 ──────────────────────────────────────────────────

    private fun firstRelay(account: JSONObject): String =
        RELAY_FIELDS.firstNotNullOfOrNull { field ->
            account.optString(field).trim().takeIf { it.isNotEmpty() }
        }.orEmpty()

    private fun normalizeRelay(value: String): String? {
        var text = value.trim().trimEnd('/')
        if (text.isEmpty()) return null
        if (!text.startsWith("http://") && !text.startsWith("https://")) {
            text = "https://$text"
        }
        val path = runCatching { URL(text).path }.getOrDefault("")
        if (path.isEmpty() || path == "/") text += "/v1"
        return text
    }

    /** 与 Python 侧 ``fetch_relay_models`` 同构：支持 data / models 数组与字符串条目。 */
    private fun parseModels(
        payload: JSONObject,
    ): Triple<List<String>, Map<String, Int>, Map<String, Int>> {
        val items = payload.optJSONArray("data")
            ?: payload.optJSONArray("models")
            ?: throw IllegalStateException("模型列表响应结构异常（缺少 data 数组）")
        val names = mutableListOf<String>()
        val types = mutableMapOf<String, Int>()
        val windows = mutableMapOf<String, Int>()
        for (index in 0 until items.length()) {
            val item = items.opt(index)
            val name: String
            var modelType: Int? = null
            var contextWindow: Int? = null
            when (item) {
                is String -> name = item.trim()
                is JSONObject -> {
                    name = listOf("id", "model", "model_name").firstNotNullOfOrNull { key ->
                        item.optString(key).trim().takeIf { it.isNotEmpty() }
                    }.orEmpty()
                    val rawType = item.opt("model_type")
                    if (rawType is Number) modelType = rawType.toInt()
                    val rawWindow = item.opt("max_model_len")
                    if (rawWindow is Number && rawWindow.toInt() > 0) {
                        contextWindow = rawWindow.toInt()
                    }
                }
                else -> continue
            }
            if (name.isEmpty()) continue
            if (!names.contains(name)) names.add(name)
            if (modelType != null) types[name] = modelType
            if (contextWindow != null) windows[name] = contextWindow
        }
        if (names.isEmpty()) throw IllegalStateException("模型列表为空")
        return Triple(names, types, windows)
    }

    // ── 落盘 ──────────────────────────────────────────────────────

    private fun writeSeed(
        context: Context,
        apiKey: String,
        baseUrl: String,
        models: List<String>,
        modelTypes: Map<String, Int>,
        windows: Map<String, Int>,
        phone: String,
    ) {
        val home = RuntimeInstaller.homeDir(context).apply { mkdirs() }
        val root = JSONObject().apply {
            put("api_key", apiKey)
            put("base_url", baseUrl)
            put("phone", phone)
            put("models", JSONArray().apply { models.forEach { put(it) } })
            put("model_types", JSONObject().apply {
                modelTypes.forEach { (key, value) -> put(key, value) }
            })
            put("model_context_windows", JSONObject().apply {
                windows.forEach { (key, value) -> put(key, value) }
            })
            put("written_at", System.currentTimeMillis())
        }
        val target = seedFile(context)
        if (target.exists()) target.delete()
        val tmp = File(home, "$SEED_FILE.tmp")
        tmp.writeText(root.toString())
        if (!tmp.renameTo(target)) {
            target.writeText(root.toString())
            tmp.delete()
        }
    }

    private fun machineId(context: Context): String {
        val file = File(
            RuntimeInstaller.homeDir(context).apply { mkdirs() },
            MACHINE_ID_FILE,
        )
        val existing = runCatching { file.readText().trim() }.getOrDefault("")
        if (existing.isNotEmpty()) return existing
        val value = randomHex(16)
        file.writeText(value)
        return value
    }

    private fun randomHex(bytes: Int): String {
        val raw = ByteArray(bytes)
        SecureRandom().nextBytes(raw)
        return raw.joinToString("") { "%02x".format(it) }
    }

    private fun sha256(bytes: ByteArray): ByteArray =
        MessageDigest.getInstance("SHA-256").digest(bytes)

    private fun b64url(raw: ByteArray): String =
        Base64.encodeToString(raw, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
}
