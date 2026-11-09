package com.tars.assistant

import android.Manifest
import android.animation.ObjectAnimator
import android.animation.ValueAnimator
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.view.WindowManager
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.Spinner
import android.widget.Switch
import android.widget.TextView

/** The one screen: opened by the side key, the assist gesture or the app icon. */
class MainActivity : Activity() {

    private lateinit var orb: View
    private lateinit var status: TextView
    private lateinit var engine: TextView
    private lateinit var userText: TextView
    private lateinit var assistantText: TextView
    private lateinit var wakeStatus: TextView
    private lateinit var wakeSwitch: Switch
    private var pulse: ObjectAnimator? = null
    private val handler = Handler(Looper.getMainLooper())
    private var sessionStarted = false

    private val listener: (TarsBus.Snapshot) -> Unit = { render(it) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        orb = findViewById(R.id.orb)
        status = findViewById(R.id.status)
        engine = findViewById(R.id.engine)
        userText = findViewById(R.id.userText)
        assistantText = findViewById(R.id.assistantText)

        wakeStatus = findViewById(R.id.wakeStatus)
        wakeSwitch = findViewById(R.id.wakeSwitch)
        setUpWakeControls()

        orb.setOnClickListener { VoiceService.send(this, VoiceService.ACTION_INTERRUPT) }
        findViewById<View>(R.id.endButton).setOnClickListener {
            VoiceService.send(this, VoiceService.ACTION_STOP)
            finish()
        }
        ensureSession()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        ensureSession()
    }

    override fun onStart() {
        super.onStart()
        TarsBus.listen(listener)
    }

    override fun onStop() {
        TarsBus.unlisten(listener)
        super.onStop()
    }

    private fun ensureSession() {
        val needed = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            needed += Manifest.permission.POST_NOTIFICATIONS
        }
        val missing = needed.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (Manifest.permission.RECORD_AUDIO in missing) {
            requestPermissions(missing.toTypedArray(), 1)
            return
        }
        if (!VoiceService.talking) {
            sessionStarted = true
            TarsBus.update { TarsBus.Snapshot(state = TarsBus.State.CONNECTING, wake = it.wake) }
            VoiceService.talk(this)
        }
    }

    override fun onRequestPermissionsResult(code: Int, permissions: Array<out String>, results: IntArray) {
        super.onRequestPermissionsResult(code, permissions, results)
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            ensureSession()
        } else {
            status.text = "마이크 권한이 필요해요"
        }
    }

    private fun render(s: TarsBus.Snapshot) {
        engine.text = when (s.engine) {
            "openai" -> "TARS · GPT"
            "gemini" -> "TARS · Gemini"
            else -> "TARS"
        }
        userText.text = s.userText
        assistantText.text = s.assistantText
        val (label, color, pulsing) = when (s.state) {
            TarsBus.State.CONNECTING -> Triple("연결 중…", R.color.idle, false)
            TarsBus.State.RECONNECTING -> Triple("다시 연결 중…", R.color.idle, false)
            TarsBus.State.LISTENING -> Triple("듣고 있어요", R.color.accent, false)
            TarsBus.State.SPEAKING -> Triple("말하는 중 · 눌러서 멈추기", R.color.accent, true)
            TarsBus.State.ENDED -> Triple(s.error.ifEmpty { "끝났어요" }, R.color.idle, false)
        }
        status.text = label
        wakeStatus.text = when (s.wake) {
            TarsBus.Wake.OFF -> "꺼짐"
            TarsBus.Wake.LOADING -> "준비 중…"
            TarsBus.Wake.LISTENING -> "듣는 중 · 폰 안에서만 들어요"
            TarsBus.Wake.PAUSED_HOME -> "집 와이파이라 쉬는 중"
            TarsBus.Wake.ERROR -> "오류"
        }
        orb.background.mutate().setTint(getColor(color))
        orb.alpha = if (s.state == TarsBus.State.LISTENING) 0.85f else 1f
        setPulse(pulsing)

        // Close by itself after a normal ending; keep errors on screen.
        if (s.state == TarsBus.State.ENDED && sessionStarted && s.error.isEmpty()) {
            handler.postDelayed({ if (!VoiceService.talking) finish() }, 900)
        }
    }

    private fun setUpWakeControls() {
        val words = Prefs.WAKE_WORDS
        val picker = findViewById<Spinner>(R.id.wakeWord)
        picker.adapter = ArrayAdapter(
            this, android.R.layout.simple_spinner_dropdown_item, words
        )
        picker.setSelection(words.indexOf(Prefs.wakeWord(this)))
        picker.onItemSelectedListener = object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: AdapterView<*>?, view: View?, position: Int, id: Long) {
                if (words[position] == Prefs.wakeWord(this@MainActivity)) return
                Prefs.setWakeWord(this@MainActivity, words[position])
                if (Prefs.wakeEnabled(this@MainActivity)) VoiceService.setWake(this@MainActivity, true)
            }

            override fun onNothingSelected(parent: AdapterView<*>?) {}
        }
        wakeSwitch.isChecked = Prefs.wakeEnabled(this)
        wakeSwitch.setOnCheckedChangeListener { _, on -> VoiceService.setWake(this, on) }
    }

    private fun setPulse(on: Boolean) {
        if (on && pulse == null) {
            pulse = ObjectAnimator.ofFloat(orb, View.SCALE_X, 1f, 1.08f).apply {
                duration = 520
                repeatMode = ValueAnimator.REVERSE
                repeatCount = ValueAnimator.INFINITE
                addUpdateListener { orb.scaleY = orb.scaleX }
                start()
            }
        } else if (!on && pulse != null) {
            pulse?.cancel()
            pulse = null
            orb.scaleX = 1f
            orb.scaleY = 1f
        }
    }
}
