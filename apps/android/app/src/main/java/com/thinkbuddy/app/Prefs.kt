package com.thinkbuddy.app

import android.content.Context

/**
 * 引擎来源：本地（PRoot 里跑 python + node）或远程（SSH 转发到电脑上的引擎）。
 */
data class EngineConfig(
    val mode: String,
    val runtimeUrl: String,
    val sshHost: String,
    val sshPort: Int,
    val sshUser: String,
    val sshPassword: String,
    val sshKeyPath: String,
    val remoteEnginePort: Int,
) {
    companion object {
        const val MODE_LOCAL = "local"
        const val MODE_REMOTE = "remote"
    }
}

class Prefs(context: Context) {
    private val sp = context.applicationContext
        .getSharedPreferences("thinkbuddy", Context.MODE_PRIVATE)

    var mode: String
        get() = sp.getString("mode", EngineConfig.MODE_LOCAL) ?: EngineConfig.MODE_LOCAL
        set(value) = sp.edit().putString("mode", value).apply()

    var runtimeUrl: String
        get() = sp.getString("runtime_url", BuildConfig.RUNTIME_URL) ?: BuildConfig.RUNTIME_URL
        set(value) = sp.edit().putString("runtime_url", value).apply()

    var installedVersion: String
        get() = sp.getString("installed_version", "") ?: ""
        set(value) = sp.edit().putString("installed_version", value).apply()

    var sshHost: String
        get() = sp.getString("ssh_host", "") ?: ""
        set(value) = sp.edit().putString("ssh_host", value).apply()

    var sshPort: Int
        get() = sp.getInt("ssh_port", 22)
        set(value) = sp.edit().putInt("ssh_port", value).apply()

    var sshUser: String
        get() = sp.getString("ssh_user", "") ?: ""
        set(value) = sp.edit().putString("ssh_user", value).apply()

    var sshPassword: String
        get() = sp.getString("ssh_password", "") ?: ""
        set(value) = sp.edit().putString("ssh_password", value).apply()

    var sshKeyPath: String
        get() = sp.getString("ssh_key_path", "") ?: ""
        set(value) = sp.edit().putString("ssh_key_path", value).apply()

    var remoteEnginePort: Int
        get() = sp.getInt("remote_engine_port", BuildConfig.FRONTEND_PORT)
        set(value) = sp.edit().putInt("remote_engine_port", value).apply()

    fun config(): EngineConfig = EngineConfig(
        mode = mode,
        runtimeUrl = runtimeUrl,
        sshHost = sshHost,
        sshPort = sshPort,
        sshUser = sshUser,
        sshPassword = sshPassword,
        sshKeyPath = sshKeyPath,
        remoteEnginePort = remoteEnginePort,
    )
}
