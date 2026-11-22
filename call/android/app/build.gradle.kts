import java.net.URI
import java.security.MessageDigest
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// A release is signed with a key kept outside the repository: ~/.config/henchline/signing.properties
// (storeFile, storePassword, keyAlias, keyPassword). Without it, only debug builds are signed.
val signing = Properties().apply {
    val file = File(System.getProperty("user.home"), ".config/henchline/signing.properties")
    if (file.exists()) file.inputStream().use { load(it) }
}

android {
    namespace = "io.github.ninjatuna007.henchline"
    compileSdk = 35

    defaultConfig {
        applicationId = "io.github.ninjatuna007.henchline"
        minSdk = 29
        targetSdk = 35
        versionCode = 8
        versionName = "2.5"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        ndk { abiFilters += "arm64-v8a" }  // the phones this app is for; keeps sherpa-onnx's other libraries out
    }
    signingConfigs {
        if (signing.getProperty("storeFile") != null) create("release") {
            storeFile = File(signing.getProperty("storeFile"))
            storePassword = signing.getProperty("storePassword")
            keyAlias = signing.getProperty("keyAlias")
            keyPassword = signing.getProperty("keyPassword")
        }
    }
    buildTypes {
        // A release has the default (generated) henchman phrases: your own voice is in src/debug only
        getByName("release") {
            signingConfig = signingConfigs.findByName("release")
            manifestPlaceholders["phoneVoiceBench"] = "false"  // the speed bench never ships
        }
        getByName("debug") { manifestPlaceholders["phoneVoiceBench"] = "true" }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    // The laptop's own signature vectors, read in place: the phone's tests can never drift from the server's
    sourceSets["test"].resources.srcDir("../../../tests/fixtures")
    sourceSets["androidTest"].assets.srcDir("../../../tests/fixtures")  // the same vectors, on the phone itself
}

// sherpa-onnx (onnxruntime linked in) for talkback spoken on the phone (PhoneVoice): its release AAR, fetched once
// and checked against its pinned SHA-256; only the arm64 library goes into the app (abiFilters below)
val sherpaVersion = "2.0.2"
val sherpaAar = file("libs/sherpa-onnx-static-link-onnxruntime-$sherpaVersion.aar")
val sherpaSha256 = "cb73564809268c8b96bf98f1a32b0b3b611e01d028d22f10beb57ef55418c0cb"
fun sha256(f: File): String = MessageDigest.getInstance("SHA-256").digest(f.readBytes())
    .joinToString("") { "%02x".format(it) }
val fetchSherpa by tasks.registering {
    outputs.file(sherpaAar)
    doLast {
        if (sherpaAar.exists() && sha256(sherpaAar) == sherpaSha256) return@doLast
        sherpaAar.parentFile.mkdirs()
        val url = "https://github.com/k2-fsa/sherpa-onnx/releases/download/v$sherpaVersion/${sherpaAar.name}"
        val part = File(sherpaAar.path + ".part")
        URI(url).toURL().openStream().use { input -> part.outputStream().use { out -> input.copyTo(out) } }
        check(sha256(part) == sherpaSha256) { "${sherpaAar.name} doesn't match its pinned SHA-256" }
        part.renameTo(sherpaAar)
    }
}
tasks.named("preBuild") { dependsOn(fetchSherpa) }

dependencies {
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation(files(sherpaAar))
    implementation("androidx.swiperefreshlayout:swiperefreshlayout:1.1.0")  // pull down the chat list to find every agent
    implementation("org.unifiedpush.android:connector:3.0.10")  // rung awake through the ntfy app, no Google
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")  // the real org.json on the JVM (Android's is a stub in unit tests)
    androidTestImplementation("androidx.test.ext:junit:1.2.1")  // connectedDebugAndroidTest: the vectors on a phone
    androidTestImplementation("androidx.test:runner:1.6.2")
}
