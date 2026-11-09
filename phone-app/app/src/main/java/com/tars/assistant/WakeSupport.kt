package com.tars.assistant

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import org.json.JSONObject
import org.vosk.Recognizer
import java.io.File
import java.net.Inet4Address
import java.util.ArrayDeque
import kotlin.math.sqrt

/** Settings changed from the screen: whether to listen for the wake word, and which word. */
object Prefs {
    val WAKE_WORDS = listOf("자비스", "에이미", "프라이데이", "컴퓨터", "타스")

    private fun prefs(context: Context) = context.getSharedPreferences("tars", Context.MODE_PRIVATE)

    fun wakeEnabled(context: Context) = prefs(context).getBoolean("wake_enabled", false)

    fun setWakeEnabled(context: Context, on: Boolean) =
        prefs(context).edit().putBoolean("wake_enabled", on).apply()

    fun wakeWord(context: Context) =
        prefs(context).getString("wake_word", WAKE_WORDS[0])!!.takeIf { it in WAKE_WORDS } ?: WAKE_WORDS[0]

    fun setWakeWord(context: Context, word: String) =
        prefs(context).edit().putString("wake_word", word).apply()
}

/**
 * Everyday Korean words offered to the recognizer next to the wake word, so ordinary speech
 * has somewhere else to land instead of being forced onto the wake word. Includes sounds
 * close to the wake words (자비, 차비, 에이 ...) for the same reason.
 */
val WAKE_FILLER = """
나 너 우리 그 이 저 거 것 게 뭐 왜 어디 언제 누구 어떻게 지금 오늘 내일 어제 아침 저녁 밤 시간 사람 친구 집 회사 학교 밥 물 불 차 길 말 일 날 해 달 비 눈 바람 날씨 우산
있 없 하 되 가 오 보 주 알 모르 좋 싫 크 작 많 적 같 다르 먹 자 일어나 졸리 피곤 배고 아프 춥 덥
다 요 야 아 어 네 응 음 그래 아니 진짜 정말 너무 조금 좀 많이 빨리 천천히 다시 또 그냥 혹시 근데 그리고 그래서 하지만
켜 꺼 줘 해줘 알려 틀어 맞춰 알람 음악 노래 전화 문자 메시지 사진 영상 게임 공부 운동 영화 뉴스
하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열 시 분 초 원 개 번
안녕 감사 미안 괜찮 잘 못 안 더 덜 제일 가장 별로 완전 대박 헐 이거 저거 그거 여기 거기 저기
비스 자비 차비 잡 제안 단 스 타 에이 미 프라 이데 데이 컴 퓨터
""".trim().split(Regex("\\s+"))

// Pieces of the wake word itself (자비, 비스 for 자비스) are left out so they can't steal it.
fun wakeGrammar(word: String) = listOf(word) + WAKE_FILLER.filter { !word.contains(it) }

/** True on the home Wi-Fi, where the desktop TARS (EMEET) answers instead of the phone. */
fun isAtHome(context: Context): Boolean = runCatching { checkHome(context) }.getOrDefault(false)

private fun checkHome(context: Context): Boolean {
    val manager = context.getSystemService(ConnectivityManager::class.java)
    for (network in manager.allNetworks) {
        val caps = manager.getNetworkCapabilities(network) ?: continue
        if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) continue
        val addresses = manager.getLinkProperties(network)?.linkAddresses ?: continue
        if (addresses.any { it.address is Inet4Address && it.address.hostAddress!!.startsWith(BuildConfig.HOME_PREFIX) }) {
            return true
        }
    }
    return false
}

/** Copies the bundled Korean Vosk model out of the APK once; Vosk needs real files. */
object WakeModel {
    private const val VERSION = "vosk-ko-1"

    fun ensure(context: Context): File {
        val dir = File(context.filesDir, "vosk-ko")
        val marker = File(dir, ".$VERSION")
        if (marker.exists()) return dir
        File(context.filesDir, "vosk-en").deleteRecursively() // earlier English model
        dir.deleteRecursively()
        val files = context.assets.open("vosk-ko.files").bufferedReader().readLines().filter { it.isNotBlank() }
        for (path in files) {
            val target = File(context.filesDir, path)
            target.parentFile?.mkdirs()
            context.assets.open(path).use { input -> target.outputStream().use { input.copyTo(it) } }
        }
        marker.createNewFile()
        return dir
    }
}

/**
 * Finds the wake word in 16 kHz PCM16 chunks. Quiet audio skips the recognizer to save
 * battery; the few chunks before speech starts are replayed so the word's start isn't lost.
 * Only finished phrases count (not guesses mid-word), which keeps ordinary speech from
 * triggering it. Time is counted in audio, so a recorded file behaves like the live mic.
 */
class WakeDetector(private val rec: Recognizer, private val word: String) {
    private val lead = ArrayDeque<ShortArray>()
    private var clockMs = 0L
    private var hotUntil = 0L
    var lastHeard = ""
        private set

    fun feed(chunk: ShortArray): Boolean {
        clockMs += chunk.size * 1000L / 16000
        if (rms(chunk) > LOUD_RMS) {
            if (clockMs >= hotUntil) lead.forEach { rec.acceptWaveForm(it, it.size) }
            hotUntil = clockMs + 1500
        }
        if (clockMs < hotUntil) {
            return rec.acceptWaveForm(chunk, chunk.size) && heard(rec.result)
        }
        if (hotUntil != 0L) {
            // Speech ended: take whatever is left, then rest until the next sound.
            val found = heard(rec.finalResult)
            rec.reset()
            hotUntil = 0L
            if (found) return true
        }
        lead.addLast(chunk)
        while (lead.size > 3) lead.removeFirst()
        return false
    }

    private fun heard(json: String): Boolean {
        val text = JSONObject(json).optString("text")
        if (text.isNotBlank()) lastHeard = text
        return word in text.split(" ")
    }

    private fun rms(samples: ShortArray): Double {
        var sum = 0.0
        for (s in samples) sum += s * s.toDouble()
        return sqrt(sum / maxOf(1, samples.size))
    }

    companion object {
        const val LOUD_RMS = 250.0
    }
}
