import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// 运行时下载地址/版本号由 CI 通过 -P 传入（或本地 app/runtime.properties 覆盖）。
// 完全离线分发：把运行时 zip 放进 app/src/main/assets/thinkbuddy-runtime.zip，
// 构建时自动检测并打进 APK（APK ≈ 1GB），首次启动从 assets 解压、不联网。
val bundledRuntime = file("src/main/assets/thinkbuddy-runtime.zip").isFile

val props = Properties().apply {
    val file = file("runtime.properties")
    if (file.exists()) file.inputStream().use { load(it) }
}

fun prop(name: String, fallback: String): String =
    providers.gradleProperty(name).orNull ?: props.getProperty(name) ?: fallback

android {
    namespace = "com.thinkbuddy.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.thinkbuddy.app"
        minSdk = 26
        // 必须是 28：API 29 起禁止从 app 可写目录执行二进制（W^X），
        // 而 PRoot 里的 python / node 就装在 filesDir 下。
        // 本项目通过 GitHub 分发，不进 Play，targetSdk 28 是可接受的取舍。
        targetSdk = 28
        versionCode = prop("versionCode", "1").toInt()
        versionName = prop("versionName", "1.0.0")

        buildConfigField("String", "RUNTIME_URL", "\"${prop("runtimeUrl", "")}\"")
        buildConfigField("String", "RUNTIME_SHA256", "\"${prop("runtimeSha256", "")}\"")
        buildConfigField("String", "ENGINE_VERSION", "\"${prop("engineVersion", "")}\"")
        buildConfigField("boolean", "BUNDLED_RUNTIME", bundledRuntime.toString())
        buildConfigField("int", "BACKEND_PORT", prop("backendPort", "8001"))
        buildConfigField("int", "FRONTEND_PORT", prop("frontendPort", "3782"))
    }

    androidResources {
        // 运行时本身就是 zip：不二次压缩既省构建时间，也让 AssetManager 能走
        // openFd（未压缩条目才能直接拿文件描述符读长度）。
        noCompress += "zip"
    }

    buildFeatures {
        buildConfig = true
    }

    // 可选正式签名：CI 提供密钥时出 release APK，否则退回 debug 签名（可直接安装，
    // 但每次换 runner 签名会变，不适合作为长期升级渠道）。
    val keystorePath = System.getenv("ANDROID_KEYSTORE_PATH")
    signingConfigs {
        if (!keystorePath.isNullOrBlank()) {
            create("release") {
                storeFile = File(keystorePath)
                storePassword = System.getenv("ANDROID_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("ANDROID_KEY_ALIAS")
                keyPassword = System.getenv("ANDROID_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (!keystorePath.isNullOrBlank()) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    packaging {
        resources.excludes += setOf("META-INF/DEPENDENCIES", "META-INF/LICENSE*", "META-INF/NOTICE*")
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-ktx:1.9.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    // 手机端运行包是 zip + 符号链接 + 权限位，java.util.zip 读不到 external
    // attributes，用 commons-compress 才能重建 python3 -> python3.12 这类链接。
    implementation("org.apache.commons:commons-compress:1.26.2")
    implementation("commons-io:commons-io:2.16.1")
    implementation("org.tukaani:xz:1.9")
    // 远程模式：SSH 本地端口转发到电脑上的引擎。
    implementation("com.jcraft:jsch:0.1.55")
}
