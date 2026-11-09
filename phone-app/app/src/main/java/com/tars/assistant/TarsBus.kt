package com.tars.assistant

import android.os.Handler
import android.os.Looper
import java.util.concurrent.CopyOnWriteArrayList

/** Conversation state shared between the voice service and the screen (same process). */
object TarsBus {
    enum class State { CONNECTING, LISTENING, SPEAKING, RECONNECTING, ENDED }

    enum class Wake { OFF, LOADING, LISTENING, PAUSED_HOME, ERROR }

    data class Snapshot(
        val state: State = State.ENDED,
        val engine: String = "",
        val userText: String = "",
        val assistantText: String = "",
        val error: String = "",
        val wake: Wake = Wake.OFF,
    )

    @Volatile
    var snapshot = Snapshot()
        private set

    private val listeners = CopyOnWriteArrayList<(Snapshot) -> Unit>()
    private val main = Handler(Looper.getMainLooper())

    fun update(change: (Snapshot) -> Snapshot) {
        val next = synchronized(this) {
            snapshot = change(snapshot)
            snapshot
        }
        main.post { listeners.forEach { it(next) } }
    }

    fun listen(listener: (Snapshot) -> Unit) {
        listeners.add(listener)
        listener(snapshot)
    }

    fun unlisten(listener: (Snapshot) -> Unit) {
        listeners.remove(listener)
    }
}
