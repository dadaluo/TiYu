import java.io.File

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
    id("org.jetbrains.kotlin.plugin.serialization")
    id("com.google.devtools.ksp")
}

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

    // 固定签名：本地（环境变量 DQ_KS_PATH/DQ_KS_STORE_PASS）与 GitHub Actions（secrets）共用同一 keystore，
    // 保证所有渠道构建的 APK 签名一致，可覆盖安装、无需卸载旧版。
    val ksPath = System.getenv("DQ_KS_PATH")
    val ksStorePass = System.getenv("DQ_KS_STORE_PASS")
    val hasDqKeystore = !ksPath.isNullOrBlank() && File(ksPath).exists() && !ksStorePass.isNullOrBlank()

    signingConfigs {
        if (hasDqKeystore) {
            create("dq") {
                storeFile = File(ksPath!!)
                storePassword = ksStorePass
                keyAlias = "dronequiz"
                keyPassword = ksStorePass
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = if (hasDqKeystore) signingConfigs.getByName("dq") else signingConfigs.getByName("debug")
        }
        debug {
            if (hasDqKeystore) signingConfig = signingConfigs.getByName("dq")
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
