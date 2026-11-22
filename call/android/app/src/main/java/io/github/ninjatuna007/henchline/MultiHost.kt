package io.github.ninjatuna007.henchline

import android.os.Build
import androidx.annotation.RequiresApi
import org.json.JSONObject

/** Henchline on more than one computer: off unless switched on, and only on Android 13+ (API 33), whose platform
 *  verifies the hosts' Ed25519 signatures, and only when that verification passes a self-test on this phone.
 *  Older phones, or one whose platform fails the test, keep the single-laptop app with all of this hidden. */
object MultiHost {
    const val PREF = "multihost"
    const val MIN_SDK = 33

    fun available(): Boolean = Build.VERSION.SDK_INT >= MIN_SDK && selfTest

    /** The switch first: with it off this is one preference read, and the Ed25519 self-test never runs (it is on
     *  every request's path, through LegacySigner). */
    fun enabled(context: android.content.Context): Boolean =
        context.getSharedPreferences("call", android.content.Context.MODE_PRIVATE).getBoolean(PREF, false) && available()

    /** Once per process: this phone's Ed25519 and org.json agree with the laptop's vectors. */
    val selfTest: Boolean by lazy {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            val why = SelfTest.run()
            if (why.isNotEmpty()) android.util.Log.w("MultiHost", "multi-host stays off: $why")  // once: lazy
            why.isEmpty()
        } else false
    }
}

/** The descriptor vector of tests/fixtures/host-signatures.json (public test data, no real key), checked on the
 *  phone itself: the platform's Ed25519 must verify it, and a tampered copy must fail, after a round trip through
 *  Android's own org.json and the phone's RFC 8785 encoder. VectorsTest keeps these equal to the fixture. */
@RequiresApi(MultiHost.MIN_SDK)
object SelfTest {
    const val HOST_KEY = "Kay64UG8yvCyLhqU000LxzYeUm0L_hLIl5S8kyKWbdc"
    const val SIGNED_DESCRIPTOR = "{\"schema\":\"henchline.host/v1\",\"host_id\":\"00000000-0000-4000-8000-000000000001\",\"label\":\"Test host \u2603\ud83d\ude00\",\"base_url\":\"https://host-a.example.ts.net/\",\"tls_authority\":\"host-a.example.ts.net\",\"api_version\":\"1.0\",\"descriptor_seq\":17,\"updated_at\":\"2026-11-20T21:00:00Z\",\"host_signing_key\":\"Kay64UG8yvCyLhqU000LxzYeUm0L_hLIl5S8kyKWbdc\",\"host_key_algorithm\":\"ed25519\",\"min_app_version\":1,\"capabilities\":{\"calls\":true,\"chat\":true,\"files\":false},\"ordering\":{\"\ud83d\ude00\":1,\"\uff61\":2,\"a\":3,\"B\":4},\"signature\":\"L0DER7_3fENQ_VVfOsCgJlqjhzDSNOPGaQEmAzqVTuNUWiW2hBrnSQ0zudEiTfa7_SdCHbSuwi2eiSs53SIsBA\"}"
    const val CANONICAL = "{\"api_version\":\"1.0\",\"base_url\":\"https://host-a.example.ts.net/\",\"capabilities\":{\"calls\":true,\"chat\":true,\"files\":false},\"descriptor_seq\":17,\"host_id\":\"00000000-0000-4000-8000-000000000001\",\"host_key_algorithm\":\"ed25519\",\"host_signing_key\":\"Kay64UG8yvCyLhqU000LxzYeUm0L_hLIl5S8kyKWbdc\",\"label\":\"Test host \u2603\ud83d\ude00\",\"min_app_version\":1,\"ordering\":{\"B\":4,\"a\":3,\"\ud83d\ude00\":1,\"\uff61\":2},\"schema\":\"henchline.host/v1\",\"tls_authority\":\"host-a.example.ts.net\",\"updated_at\":\"2026-11-20T21:00:00Z\"}"

    /** What failed, or "" when all is well. */
    fun run(): String = try {
        val descriptor = JSONObject(JSONObject(SIGNED_DESCRIPTOR).toString())  // parsed, written, parsed: org.json's way
        val unsigned = JSONObject(descriptor.toString()).apply { remove("signature") }
        when {
            String(Canonical.jcs(unsigned), Charsets.UTF_8) != CANONICAL -> "RFC 8785 bytes differ from the laptop's"
            !HostDescriptor.verify(descriptor, HOST_KEY) -> "the platform's Ed25519 did not verify the vector"
            HostDescriptor.verify(JSONObject(descriptor.toString()).put("descriptor_seq", 18), HOST_KEY) ->
                "a tampered descriptor verified"
            else -> ""
        }
    } catch (e: Throwable) {
        "self-test failed: ${e.javaClass.simpleName}"
    }
}
