package com.thinkbuddy.app

import com.jcraft.jsch.JSch
import com.jcraft.jsch.Session
import java.io.IOException
import java.net.ServerSocket

/**
 * 远程模式：把本机一个端口转发到电脑上正在运行的 ThinkBuddy 引擎。
 *
 * 电脑侧只需 `deeptutor start`（前端默认 3782），手机端 WebView 打开
 * http://127.0.0.1:<本地端口> 即可，重能力（deep_research 全文解析、
 * math_animator、video_lesson）全部在电脑上算。
 */
class RemoteEngine {

    private var session: Session? = null

    fun connect(config: EngineConfig): Int {
        require(config.sshHost.isNotBlank()) { "远程模式需要填写 SSH 主机" }
        require(config.sshUser.isNotBlank()) { "远程模式需要填写 SSH 用户名" }

        val jsch = JSch()
        if (config.sshKeyPath.isNotBlank()) {
            jsch.addIdentity(config.sshKeyPath)
        }
        val ssh = jsch.getSession(config.sshUser, config.sshHost, config.sshPort)
        if (config.sshPassword.isNotBlank()) {
            ssh.setPassword(config.sshPassword)
        }
        ssh.setConfig("StrictHostKeyChecking", "no")
        ssh.setConfig("PreferredAuthentications", "publickey,password,keyboard-interactive")
        ssh.connect(15_000)
        session = ssh

        val localPort = ServerSocket(0).use { it.localPort }
        try {
            ssh.setPortForwardingL("127.0.0.1", localPort, "127.0.0.1", config.remoteEnginePort)
        } catch (exc: Exception) {
            ssh.disconnect()
            session = null
            throw IOException("端口转发失败：${exc.message}", exc)
        }
        return localPort
    }

    fun close() {
        runCatching { session?.disconnect() }
        session = null
    }
}
