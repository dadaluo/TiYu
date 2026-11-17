import java.io.File
import java.security.KeyStore
import java.security.MessageDigest

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.plugin.serialization")
    id("com.google.devtools.ksp")
}

// 本地与 CI 只提供同一把固定密钥的存放位置和密码。
val dqStoreFile = System.getenv("DQ_KS_PATH")?.takeIf { it.isNotBlank() }?.let(::File)
    ?: rootProject.file("keystore/dronequiz.keystore")
val dqStorePassword = System.getenv("DQ_KS_STORE_PASS")?.takeIf { it.isNotBlank() }
    ?: rootProject.file("keystore/pass.txt").takeIf { it.isFile }?.readText(Charsets.UTF_8)?.trim()
val dqKeyAlias = providers.gradleProperty("tiyu.signing.keyAlias").get()
val dqCertificateSha256 = providers.gradleProperty("tiyu.signing.certificateSha256").get()

android {
    namespace = "com.drone.quiz"
    compileSdk = 37

    defaultConfig {
        applicationId = "com.drone.quiz"
        minSdk = 31
        targetSdk = 35
        versionCode = if (providers.gradleProperty("tiyuPreview").orNull == "true") 111 else 110
        versionName = if (providers.gradleProperty("tiyuPreview").orNull == "true")
            "2.19.54-preview" else "2.19.54"
        // 仅打包 ARM 双架构，沿用现有真机支持范围。
        ndk { abiFilters += listOf("armeabi-v7a", "arm64-v8a") }
    }

    signingConfigs {
        create("dq") {
            storeFile = dqStoreFile
            storePassword = dqStorePassword
            keyAlias = dqKeyAlias
            keyPassword = dqStorePassword
        }
    }

    buildTypes {
        configureEach {
            signingConfig = signingConfigs.getByName("dq")
        }
        release {
            isMinifyEnabled = false
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_21
        targetCompatibility = JavaVersion.VERSION_21
    }
    buildFeatures {
        compose = true
        buildConfig = true
    }
    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }
}

val verifyFixedSigning by tasks.registering {
    group = "verification"
    description = "Verify that APK signing uses the project's fixed certificate."
    doLast {
        check(dqStoreFile.isFile && !dqStorePassword.isNullOrBlank()) {
            "固定签名缺失：请配置 keystore/dronequiz.keystore 和 keystore/pass.txt，或 CI 的 DQ_KS_PATH / DQ_KS_STORE_PASS。"
        }
        val store = try {
            KeyStore.getInstance(dqStoreFile, dqStorePassword!!.toCharArray())
        } catch (_: Exception) {
            throw GradleException("无法解锁固定签名 keystore，请检查密码；不会回退到 debug 签名。")
        }
        check(store.isKeyEntry(dqKeyAlias)) { "固定 keystore 缺少 dronequiz 私钥。" }
        val certificate = store.getCertificate(dqKeyAlias)
        val fingerprint = MessageDigest.getInstance("SHA-256")
            .digest(certificate.encoded).joinToString("") { "%02x".format(it) }
        check(fingerprint == dqCertificateSha256) { "签名证书与项目固定证书不一致，已拒绝打包。" }
    }
}

tasks.matching { it.name.startsWith("validateSigning") }.configureEach {
    dependsOn(verifyFixedSigning)
}

kotlin {
    compilerOptions {
        freeCompilerArgs.add("-Xcontext-parameters")
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2026.09.00")
    implementation(composeBom)

    implementation("androidx.core:core-ktx:1.19.0")
    implementation("androidx.activity:activity-compose:1.13.0")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.11.0")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.11.0")

    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.ui:ui-util")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.animation:animation")

    implementation("androidx.navigation:navigation-compose:2.10.1")

    // 官方同款连续曲率形状（Capsule/RoundedRectangle）已源码 vendor 到 com.kyant.shapes，
    // 保留项目现有的 Android 单源集适配和渲染修改，避免上游 AAR 变更影响现有视觉实现。

    implementation("androidx.room:room-runtime:2.8.5")
    implementation("androidx.room:room-ktx:2.8.5")
    ksp("androidx.room:room-compiler:2.8.5")

    implementation("androidx.datastore:datastore-preferences:1.2.1")
    // v2.15.0：移除 work-runtime——每日提醒调度引擎换 AlarmManager 精确闹钟，
    // WorkManager OneTime 自续在 ROM 杀后台下蒸发（「打开 APP 才通知」根因），全仓已无 androidx.work 引用

    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.11.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.11.0")
    // 上游 v2.17.0 已移除拍照搜题及 OCR / CameraX / native 依赖。

    testImplementation("junit:junit:4.13.2")

    implementation("com.mikepenz:multiplatform-markdown-renderer:0.45.0")
    implementation("com.mikepenz:multiplatform-markdown-renderer-m3:0.45.0")
}
