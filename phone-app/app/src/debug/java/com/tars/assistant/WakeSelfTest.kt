package com.tars.assistant

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import org.json.JSONArray
import org.vosk.Model
import org.vosk.Recognizer
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.concurrent.thread

/** Runs every bundled test recording through the real wake detector and logs a table (tag TarsWakeTest). */
class WakeSelfTest : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val pending = goAsync()
        thread {
            try {
                val started = System.currentTimeMillis()
                val model = Model(WakeModel.ensure(context).absolutePath)
                Log.i(TAG, "model loaded in ${System.currentTimeMillis() - started} ms")
                val files = context.assets.list("selftest")!!.sorted()
                for (file in files) {
                    val pcm = readWav(context, "selftest/$file")
                    val row = Prefs.WAKE_WORDS.joinToString("  ") { word ->
                        Recognizer(model, 16000f, JSONArray(wakeGrammar(word)).toString()).use { rec ->
                            val detector = WakeDetector(rec, word)
                            var hit = false
                            var i = 0
                            while (i < pcm.size && !hit) {
                                hit = detector.feed(pcm.copyOfRange(i, minOf(i + 1600, pcm.size)))
                                i += 1600
                            }
                            // trailing silence lets the detector close the last phrase
                            repeat(20) { if (!hit) hit = detector.feed(ShortArray(1600)) }
                            "$word=${if (hit) "YES" else "-(${detector.lastHeard})"}"
                        }
                    }
                    Log.i(TAG, "${file.removeSuffix(".wav").padEnd(12)} $row")
                }
                model.close()
                Log.i(TAG, "done")
            } catch (e: Throwable) {
                Log.e(TAG, "failed", e)
            } finally {
                pending.finish()
            }
        }
    }

    private fun readWav(context: Context, path: String): ShortArray {
        val bytes = context.assets.open(path).use { it.readBytes() }
        val data = ByteBuffer.wrap(bytes, 44, bytes.size - 44).order(ByteOrder.LITTLE_ENDIAN).asShortBuffer()
        return ShortArray(data.remaining()).also { data.get(it) }
    }

    companion object {
        private const val TAG = "TarsWakeTest"
    }
}
