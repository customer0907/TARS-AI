import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Server address and shared token live outside the code, in tars.properties.
val tars = Properties().apply { rootProject.file("tars.properties").inputStream().use { load(it) } }

android {
    namespace = "com.tars.assistant"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.tars.assistant"
        minSdk = 29
        targetSdk = 35
        versionCode = 4
        versionName = "0.4"
        ndk { abiFilters += "arm64-v8a" } // Galaxy S24 only; keeps the wake engine small
        buildConfigField("String", "TARS_URL", "\"${tars.getProperty("tars.url")}\"")
        buildConfigField("String", "TARS_TOKEN", "\"${tars.getProperty("tars.token")}\"")
        buildConfigField("String", "HOME_PREFIX", "\"${tars.getProperty("tars.home_prefix")}\"")
    }

    buildFeatures { buildConfig = true }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    // On-device wake word (same Vosk English model the desktop uses for "amy").
    implementation("com.alphacephei:vosk-android:0.3.75")
    implementation("net.java.dev.jna:jna:5.19.1@aar")
}
