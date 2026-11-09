package com.tars.assistant

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.media.ToneGenerator
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.NoiseSuppressor
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONArray
import org.json.JSONObject
import org.vosk.Model
import org.vosk.Recognizer
import java.util.ArrayDeque
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/**
 * The phone's microphone service, in one of two modes:
 *  - wake: listens on the phone for the wake word (nothing leaves the phone), paused on home Wi-Fi
 *  - talk: one conversation, streaming the mic to the home TARS and playing its replies
 * One foreground service does both, so a wake word can start a conversation with the screen off.
 */
class VoiceService : Service() {

    companion object {
        const val ACTION_TALK = "com.tars.assistant.TALK"
        const val ACTION_STOP = "com.tars.assistant.STOP"
        const val ACTION_INTERRUPT = "com.tars.assistant.INTERRUPT"
        const val ACTION_WAKE_ON = "com.tars.assistant.WAKE_ON"
        const val ACTION_WAKE_OFF = "com.tars.assistant.WAKE_OFF"
        private const val CHANNEL = "tars_session"
        private const val NOTIFICATION_ID = 1
        private const val MIC_RATE = 16000
        private const val SPEAKER_RATE = 24000
        private const val MIC_CHUNK_BYTES = 3200 // 100 ms of 16 kHz PCM16
        private const val PRECONNECT_CHUNKS = 40 // keep up to 4 s spoken before the link is up

        @Volatile
        var talking = false
            private set

        @Volatile
        private var alive = false

        fun talk(context: Context) {
            context.startForegroundService(Intent(context, VoiceService::class.java).setAction(ACTION_TALK))
        }

        fun setWake(context: Context, on: Boolean) {
            Prefs.setWakeEnabled(context, on)
            val intent = Intent(context, VoiceService::class.java)
            if (on) context.startForegroundService(intent.setAction(ACTION_WAKE_ON))
            else if (alive) context.startService(intent.setAction(ACTION_WAKE_OFF))
        }

        fun send(context: Context, action: String) {
            if (alive) context.startService(Intent(context, VoiceService::class.java).setAction(action))
        }
    }

    private val main = Handler(Looper.getMainLooper())
    private val client = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    private lateinit var audioManager: AudioManager
    private var wakeLock: PowerManager.WakeLock? = null
    private var networkCallback: ConnectivityManager.NetworkCallback? = null

    // talk mode
    private var socket: WebSocket? = null
    private var recorder: AudioRecord? = null
    private var track: AudioTrack? = null
    private var recordThread: Thread? = null
    private var playThread: Thread? = null
    private val playQueue = LinkedBlockingQueue<ByteArray>()
    private val preconnect = ArrayDeque<ByteArray>()
    @Volatile private var linked = false
    @Volatile private var lastSpeechAt = 0L

    // wake mode
    private var wakeModel: Model? = null
    private var wakeThread: Thread? = null
    @Volatile private var wakeRunning = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        alive = true
        audioManager = getSystemService(AUDIO_SERVICE) as AudioManager
        watchNetwork()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_TALK -> {
                goForeground()
                startTalk()
            }
            ACTION_WAKE_ON -> {
                goForeground()
                stopWake()
                if (!talking) startWake()
            }
            ACTION_WAKE_OFF -> {
                stopWake()
                TarsBus.update { it.copy(wake = TarsBus.Wake.OFF) }
                if (!talking) shutdown()
            }
            ACTION_STOP -> if (talking) finishTalk("") else if (!Prefs.wakeEnabled(this)) shutdown()
            ACTION_INTERRUPT -> clearPlayback()
        }
        return START_NOT_STICKY
    }

    private fun goForeground() {
        startForeground(NOTIFICATION_ID, buildNotification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
    }

    private fun refreshNotification() {
        if (alive) getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, buildNotification())
    }

    // ---------------------------------------------------------------- wake mode

    private fun startWake() {
        if (!Prefs.wakeEnabled(this) || talking || wakeRunning) return
        if (isAtHome(this)) {
            TarsBus.update { it.copy(wake = TarsBus.Wake.PAUSED_HOME) }
            refreshNotification()
            return
        }
        wakeRunning = true
        TarsBus.update { it.copy(wake = TarsBus.Wake.LOADING) }
        refreshNotification()
        val word = Prefs.wakeWord(this)
        wakeThread = thread(name = "TarsWake") {
            var heard = false
            try {
                val model = wakeModel ?: Model(WakeModel.ensure(this).absolutePath).also { wakeModel = it }
                Recognizer(model, MIC_RATE.toFloat(), JSONArray(wakeGrammar(word)).toString()).use { rec ->
                    heard = listenForWord(rec, word)
                }
            } catch (e: Exception) {
                TarsBus.update { it.copy(wake = TarsBus.Wake.ERROR, error = "호출어 대기 오류: ${e.message}") }
            } finally {
                wakeRunning = false
            }
            if (heard) main.post { onWakeWord() }
        }
    }

    /** Reads the mic until the word is heard (true) or wake mode is stopped (false). */
    private fun listenForWord(rec: Recognizer, word: String): Boolean {
        val minBuffer = AudioRecord.getMinBufferSize(MIC_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val mic = AudioRecord(
            MediaRecorder.AudioSource.VOICE_RECOGNITION, MIC_RATE,
            AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuffer, MIC_CHUNK_BYTES) * 4,
        )
        try {
            mic.startRecording()
            TarsBus.update { it.copy(wake = TarsBus.Wake.LISTENING) }
            refreshNotification()
            val buffer = ShortArray(MIC_CHUNK_BYTES / 2)
            val detector = WakeDetector(rec, word)
            while (wakeRunning) {
                val read = mic.read(buffer, 0, buffer.size)
                if (read > 0 && detector.feed(buffer.copyOf(read))) return true
            }
            return false
        } finally {
            runCatching { mic.stop() }
            mic.release()
        }
    }

    private fun stopWake() {
        wakeRunning = false
        wakeThread?.join(1500)
        wakeThread = null
    }

    private fun onWakeWord() {
        runCatching {
            val tone = ToneGenerator(AudioManager.STREAM_MUSIC, 80)
            tone.startTone(ToneGenerator.TONE_PROP_ACK, 160)
            main.postDelayed({ tone.release() }, 500)
        }
        startTalk()
        // Show the screen too when Android allows it; the talk works without it.
        runCatching {
            startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        }
    }

    /** Re-check home vs away whenever the network changes. */
    private fun watchNetwork() {
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) = recheck()
            override fun onLost(network: Network) = recheck()
            private fun recheck() {
                main.postDelayed({
                    if (!Prefs.wakeEnabled(this@VoiceService) || talking) return@postDelayed
                    val home = isAtHome(this@VoiceService)
                    if (home && wakeRunning) {
                        stopWake()
                        TarsBus.update { it.copy(wake = TarsBus.Wake.PAUSED_HOME) }
                        refreshNotification()
                    } else if (!home && !wakeRunning) {
                        startWake()
                    }
                }, 1500)
            }
        }
        runCatching {
            getSystemService(ConnectivityManager::class.java).registerDefaultNetworkCallback(callback)
            networkCallback = callback
        }
    }

    // ---------------------------------------------------------------- talk mode

    private fun startTalk() {
        if (talking) return
        stopWake()
        talking = true
        refreshNotification()
        TarsBus.update { TarsBus.Snapshot(state = TarsBus.State.CONNECTING, wake = it.wake) }
        wakeLock = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "tars:conversation")
            .apply { acquire(15 * 60 * 1000L) }
        try {
            startAudio()
        } catch (e: Exception) {
            finishTalk("마이크를 열 수 없어요: ${e.message}")
            return
        }
        connect()
    }

    private fun connect() {
        val request = Request.Builder().url(BuildConfig.TARS_URL).build()
        socket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                val hello = JSONObject()
                    .put("type", "hello")
                    .put("token", BuildConfig.TARS_TOKEN)
                    .put("input_rate", MIC_RATE)
                webSocket.send(hello.toString())
                synchronized(preconnect) {
                    while (preconnect.isNotEmpty()) webSocket.send(preconnect.removeFirst().toByteString())
                    linked = true
                }
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                playQueue.add(bytes.toByteArray())
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                handleEvent(JSONObject(text))
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                finishTalk("집 TARS에 연결하지 못했어요. Tailscale이 켜져 있는지 확인해 주세요.")
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                finishTalk(if (code == 4401) "TARS 토큰이 맞지 않아요." else "")
            }
        })
    }

    private fun handleEvent(event: JSONObject) {
        when (event.optString("type")) {
            "state" -> {
                val state = when (event.optString("state")) {
                    "listening" -> TarsBus.State.LISTENING
                    "reconnecting" -> TarsBus.State.RECONNECTING
                    else -> TarsBus.State.CONNECTING
                }
                TarsBus.update { it.copy(state = state, engine = event.optString("engine", it.engine)) }
            }
            "user" -> TarsBus.update { it.copy(userText = event.optString("text"), assistantText = "") }
            "assistant" -> TarsBus.update { it.copy(assistantText = event.optString("text")) }
            "clear" -> clearPlayback()
            "end" -> thread {
                // Let the last words finish playing before closing.
                val deadline = SystemClock.elapsedRealtime() + 8000
                while (talking && (playQueue.isNotEmpty() || isSpeaking()) &&
                    SystemClock.elapsedRealtime() < deadline
                ) Thread.sleep(100)
                val reason = event.optString("reason")
                finishTalk(if (reason.startsWith("error")) "TARS 오류: $reason" else "")
            }
        }
    }

    private fun startAudio() {
        audioManager.mode = AudioManager.MODE_IN_COMMUNICATION
        routeOutput()

        val minRecord = AudioRecord.getMinBufferSize(MIC_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val record = AudioRecord(
            MediaRecorder.AudioSource.VOICE_COMMUNICATION, MIC_RATE,
            AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, maxOf(minRecord, MIC_CHUNK_BYTES) * 4,
        )
        if (AcousticEchoCanceler.isAvailable()) AcousticEchoCanceler.create(record.audioSessionId)?.enabled = true
        if (NoiseSuppressor.isAvailable()) NoiseSuppressor.create(record.audioSessionId)?.enabled = true
        recorder = record

        val minTrack = AudioTrack.getMinBufferSize(SPEAKER_RATE, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val player = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
            )
            .setAudioFormat(
                AudioFormat.Builder()
                    .setSampleRate(SPEAKER_RATE)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .build()
            )
            .setBufferSizeInBytes(maxOf(minTrack * 2, 9600))
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
        track = player
        player.play()
        record.startRecording()

        recordThread = thread(name = "TarsMic") {
            val buffer = ByteArray(MIC_CHUNK_BYTES)
            while (talking) {
                val read = record.read(buffer, 0, buffer.size)
                if (read <= 0) continue
                val chunk = buffer.copyOf(read)
                synchronized(preconnect) {
                    if (linked) {
                        socket?.send(chunk.toByteString())
                    } else {
                        preconnect.addLast(chunk)
                        while (preconnect.size > PRECONNECT_CHUNKS) preconnect.removeFirst()
                    }
                }
            }
        }

        playThread = thread(name = "TarsSpeaker") {
            while (talking) {
                val chunk = playQueue.poll(150, TimeUnit.MILLISECONDS)
                if (chunk == null) {
                    if (TarsBus.snapshot.state == TarsBus.State.SPEAKING && !isSpeaking()) {
                        TarsBus.update { it.copy(state = TarsBus.State.LISTENING) }
                    }
                    continue
                }
                if (TarsBus.snapshot.state != TarsBus.State.SPEAKING) {
                    TarsBus.update { it.copy(state = TarsBus.State.SPEAKING) }
                }
                player.write(chunk, 0, chunk.size)
                // Remember when the written audio will have finished playing.
                lastSpeechAt = SystemClock.elapsedRealtime() + chunk.size * 1000L / (SPEAKER_RATE * 2)
            }
        }
    }

    private fun isSpeaking() = SystemClock.elapsedRealtime() < lastSpeechAt + 300

    /** Bluetooth or wired headset when connected, otherwise the loudspeaker (not the earpiece). */
    private fun routeOutput() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            val devices = audioManager.availableCommunicationDevices
            val preferred = listOf(
                AudioDeviceInfo.TYPE_BLUETOOTH_SCO,
                AudioDeviceInfo.TYPE_BLE_HEADSET,
                AudioDeviceInfo.TYPE_WIRED_HEADSET,
                AudioDeviceInfo.TYPE_USB_HEADSET,
                AudioDeviceInfo.TYPE_BUILTIN_SPEAKER,
            )
            for (type in preferred) {
                devices.firstOrNull { it.type == type }?.let {
                    audioManager.setCommunicationDevice(it)
                    return
                }
            }
        } else {
            @Suppress("DEPRECATION")
            audioManager.isSpeakerphoneOn = true
        }
    }

    private fun clearPlayback() {
        playQueue.clear()
        track?.let {
            it.pause()
            it.flush()
            it.play()
        }
        lastSpeechAt = 0L
        TarsBus.update { if (it.state == TarsBus.State.SPEAKING) it.copy(state = TarsBus.State.LISTENING) else it }
    }

    @Synchronized
    private fun finishTalk(error: String) {
        if (!talking) return
        talking = false
        linked = false
        socket?.let {
            it.send("""{"type":"stop"}""")
            it.close(1000, "done")
        }
        socket = null
        recordThread?.join(500)
        playThread?.join(500)
        recorder?.run { runCatching { stop() }; release() }
        track?.run { runCatching { stop() }; release() }
        recorder = null
        track = null
        playQueue.clear()
        synchronized(preconnect) { preconnect.clear() }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) audioManager.clearCommunicationDevice()
        audioManager.mode = AudioManager.MODE_NORMAL
        wakeLock?.takeIf { it.isHeld }?.release()
        TarsBus.update { it.copy(state = TarsBus.State.ENDED, error = error) }
        // Back to waiting for the wake word, or done.
        main.post {
            if (Prefs.wakeEnabled(this)) {
                refreshNotification()
                main.postDelayed({ if (!talking) startWake() }, 600)
            } else {
                shutdown()
            }
        }
    }

    private fun shutdown() {
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() {
        if (talking) finishTalk("")
        stopWake()
        networkCallback?.let { getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(it) }
        wakeModel?.close()
        alive = false
        super.onDestroy()
    }

    private fun buildNotification(): Notification {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel(CHANNEL, "TARS", NotificationManager.IMPORTANCE_LOW))
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE
        )
        val word = Prefs.wakeWord(this)
        val (title, text, actionLabel, action) = when {
            talking -> listOf("TARS와 대화 중", "말하면 들어요.", "끝내기", ACTION_STOP)
            TarsBus.snapshot.wake == TarsBus.Wake.PAUSED_HOME ->
                listOf("집이라 쉬는 중", "집에서는 데스크탑 TARS가 대신 들어요.", "대기 끄기", ACTION_WAKE_OFF)
            else -> listOf("'$word' 기다리는 중", "'$word' 하고 부르면 대답해요.", "대기 끄기", ACTION_WAKE_OFF)
        }
        val stop = PendingIntent.getService(
            this, 1, Intent(this, VoiceService::class.java).setAction(action), PendingIntent.FLAG_IMMUTABLE
        )
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_notify)
            .setContentTitle(title)
            .setContentText(text)
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(Notification.Action.Builder(null, actionLabel, stop).build())
            .build()
    }
}
