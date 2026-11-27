package com.thinkbuddy.app

import android.content.Context

/**
 * 手机端偏好设置。
 *
 * 本期只做**本地引擎**（PRoot 里跑 python + node），远程 / SSH 形态已移除，
 * 因此这里不再保留 sshHost / sshPassword / 端口转发等字段 —— 留着只会让
 * 「登录 → 解压 → 启动」这条唯一路径出现第二个入口。
 */
class Prefs(context: Context) {
    private val sp = context.applicationContext
        .getSharedPreferences("thinkbuddy", Context.MODE_PRIVATE)

    /** 运行时包下载地址；BUNDLED_RUNTIME=true 时此值不生效。 */
    var runtimeUrl: String
        get() = sp.getString("runtime_url", BuildConfig.RUNTIME_URL) ?: BuildConfig.RUNTIME_URL
        set(value) = sp.edit().putString("runtime_url", value).apply()

    var installedVersion: String
        get() = sp.getString("installed_version", "") ?: ""
        set(value) = sp.edit().putString("installed_version", value).apply()

    /** dsh-Go 风格的六套主题色索引。 */
    var themeIndex: Int
        get() = sp.getInt("theme_index", 0)
        set(value) = sp.edit().putInt("theme_index", value).apply()

    /** Tokengine 登录态（仅供展示；真正的判定以种子文件 / catalog 为准）。 */
    var tokengineLoggedIn: Boolean
        get() = sp.getBoolean("tokengine_logged_in", false)
        set(value) = sp.edit().putBoolean("tokengine_logged_in", value).apply()
}
