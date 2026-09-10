package com.femalevoiceai.female_voice_ai

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import java.util.concurrent.CopyOnWriteArraySet

/**
 * Foreground owner for an explicitly started Aanya AI call.
 *
 * Flutter may detach and reattach its UI bridge, but that never changes the
 * active native call generation. Only ACTION_END (including the notification
 * action) intentionally releases the runtime.
 */
class AanyaCallForegroundService : Service() {
    companion object {
        const val ACTION_START = "com.femalevoiceai.female_voice_ai.action.START_AANYA_CALL"
        const val ACTION_END = "com.femalevoiceai.female_voice_ai.action.END_AANYA_CALL"
        const val ACTION_END_FROM_NOTIFICATION =
            "com.femalevoiceai.female_voice_ai.action.END_AANYA_CALL_FROM_NOTIFICATION"
        const val EXTRA_GENERATION = "generation"

        private const val NOTIFICATION_CHANNEL_ID = "aira_active_ai_call"
        private const val NOTIFICATION_ID = 24117
        private const val TAG = "AiraCallService"
    }

    private val mainHandler = Handler(Looper.getMainLooper())
    private lateinit var runtime: AanyaCallRuntime
    private lateinit var audioManager: AudioManager
    private var audioFocusRequest: AudioFocusRequest? = null
    private var focusGeneration = 0
    private var focusRetryAttempt = 0
    private var wakeLock: PowerManager.WakeLock? = null
    private var previousAudioMode = AudioManager.MODE_NORMAL
    private val refreshNotification: (Map<String, Any?>) -> Unit = {
        if (runtime.isActive) {
            getSystemService(NotificationManager::class.java)
                .notify(NOTIFICATION_ID, buildNotification())
        }
    }

    override fun onCreate() {
        super.onCreate()
        runtime = AanyaCallRuntime.initialize(applicationContext)
        audioManager = getSystemService(Context.AUDIO_SERVICE) as AudioManager
        createNotificationChannel()
        runtime.addListener(refreshNotification)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val action = intent?.action
        val generation = intent?.getIntExtra(EXTRA_GENERATION, -1) ?: -1
        when (action) {
            ACTION_START -> startActiveCall(generation)
            ACTION_END, ACTION_END_FROM_NOTIFICATION -> endActiveCall(
                generation,
                if (action == ACTION_END_FROM_NOTIFICATION) {
                    "notification_end_call"
                } else {
                    "flutter_end_call"
                },
            )
            else -> {
                // A microphone service cannot reconstruct a killed Dart
                // realtime session. Treat a null/redelivered intent as an
                // unrecoverable process condition rather than inventing one.
                endActiveCall(runtime.generation, "service_restart_without_session")
            }
        }
        return START_NOT_STICKY
    }

    private fun startActiveCall(generation: Int) {
        if (generation <= 0 || !runtime.matchesReservation(generation)) {
            stopSelf()
            return
        }
        val notification = buildNotification(generation)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE,
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
        if (!runtime.activate(generation)) {
            removeForegroundNotification()
            stopSelf()
            return
        }
        Log.i(
            TAG,
            "event=foreground_service_started generation=$generation " +
                "monotonic_ns=${SystemClock.elapsedRealtimeNanos()}",
        )
        previousAudioMode = audioManager.mode
        audioManager.mode = AudioManager.MODE_IN_COMMUNICATION
        val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = powerManager.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK,
            "$packageName:AanyaAiCall",
        ).also { it.acquire() }
        requestAudioFocus(generation)
    }

    private fun endActiveCall(generation: Int, reason: String) {
        if (generation > 0) runtime.requestEnd(generation, reason)
        abandonAudioFocus()
        releaseWakeLock()
        audioManager.mode = previousAudioMode
        runtime.completeEnd(generation, reason)
        removeForegroundNotification()
        stopSelf()
    }

    private fun requestAudioFocus(generation: Int) {
        if (!runtime.isCurrent(generation)) return
        focusGeneration = generation
        val listener = AudioManager.OnAudioFocusChangeListener { change ->
            if (!runtime.isCurrent(generation) || focusGeneration != generation) return@OnAudioFocusChangeListener
            when (change) {
                AudioManager.AUDIOFOCUS_GAIN -> {
                    focusRetryAttempt = 0
                    runtime.playback.setDucked(false)
                    runtime.playback.resumeAfterFocusGain(generation)
                    runtime.reportAudioFocus(generation, "gain")
                }
                AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK -> {
                    runtime.playback.setDucked(true)
                    runtime.reportAudioFocus(generation, "loss_transient_can_duck")
                }
                AudioManager.AUDIOFOCUS_LOSS_TRANSIENT -> {
                    runtime.playback.pauseForFocusLoss(generation)
                    runtime.reportAudioFocus(generation, "loss_transient")
                }
                AudioManager.AUDIOFOCUS_LOSS -> {
                    runtime.playback.pauseForFocusLoss(generation)
                    runtime.reportAudioFocus(generation, "loss")
                    scheduleFocusRecovery(generation)
                }
            }
        }

        val granted = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build(),
                )
                .setOnAudioFocusChangeListener(listener, mainHandler)
                .setWillPauseWhenDucked(false)
                .build()
            audioFocusRequest = request
            audioManager.requestAudioFocus(request)
        } else {
            @Suppress("DEPRECATION")
            audioManager.requestAudioFocus(
                listener,
                AudioManager.STREAM_MUSIC,
                AudioManager.AUDIOFOCUS_GAIN,
            )
        }
        runtime.reportAudioFocus(
            generation,
            if (granted == AudioManager.AUDIOFOCUS_REQUEST_GRANTED) "gain" else "request_delayed",
        )
        if (granted != AudioManager.AUDIOFOCUS_REQUEST_GRANTED) {
            scheduleFocusRecovery(generation)
        }
    }

    private fun scheduleFocusRecovery(generation: Int) {
        if (!runtime.isCurrent(generation)) return
        val delays = longArrayOf(500L, 1_000L, 2_000L, 4_000L, 8_000L)
        val delay = delays[focusRetryAttempt.coerceAtMost(delays.lastIndex)]
        focusRetryAttempt += 1
        mainHandler.postDelayed(
            {
                if (!runtime.isCurrent(generation)) return@postDelayed
                abandonAudioFocus(clearGeneration = false)
                requestAudioFocus(generation)
            },
            delay,
        )
    }

    private fun abandonAudioFocus(clearGeneration: Boolean = true) {
        if (clearGeneration) focusGeneration += 1
        val request = audioFocusRequest
        audioFocusRequest = null
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && request != null) {
            audioManager.abandonAudioFocusRequest(request)
        } else {
            @Suppress("DEPRECATION")
            audioManager.abandonAudioFocus(null)
        }
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(
                NOTIFICATION_CHANNEL_ID,
                "Active Aanya AI call",
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = "Shows while an Aanya AI voice conversation is active."
                setSound(null, null)
                enableVibration(false)
            },
        )
    }

    private fun buildNotification(generation: Int = runtime.generation): Notification {
        val returnIntent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val returnPendingIntent = PendingIntent.getActivity(
            this,
            1,
            returnIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val endPendingIntent = PendingIntent.getService(
            this,
            2,
            Intent(this, AanyaCallForegroundService::class.java).apply {
                action = ACTION_END_FROM_NOTIFICATION
                putExtra(EXTRA_GENERATION, generation)
            },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val stateText = runtime.notificationStateLabel
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, NOTIFICATION_CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        return builder
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle("Aanya AI call in progress")
            .setContentText("AI companion • $stateText")
            .setContentIntent(returnPendingIntent)
            .setCategory(Notification.CATEGORY_CALL)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .setOnlyAlertOnce(true)
            .setOngoing(true)
            .setShowWhen(true)
            .setWhen(runtime.startedAtWallClockMs)
            .addAction(
                Notification.Action.Builder(0, "Return to call", returnPendingIntent).build(),
            )
            .addAction(Notification.Action.Builder(0, "End call", endPendingIntent).build())
            .build()
    }

    private fun removeForegroundNotification() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            stopForeground(STOP_FOREGROUND_REMOVE)
        } else {
            @Suppress("DEPRECATION")
            stopForeground(true)
        }
    }

    private fun releaseWakeLock() {
        val held = wakeLock
        wakeLock = null
        if (held?.isHeld == true) held.release()
    }

    override fun onDestroy() {
        runtime.removeListener(refreshNotification)
        abandonAudioFocus()
        releaseWakeLock()
        if (::audioManager.isInitialized) audioManager.mode = previousAudioMode
        if (runtime.isActive) {
            runtime.completeEnd(runtime.generation, "foreground_service_destroyed")
        }
        super.onDestroy()
    }

    override fun onBind(intent: Intent?) = null
}

/** Process-owned native state and device I/O shared by service and UI bridges. */
internal object AanyaCallRuntime {
    private const val TAG = "AiraCallTiming"
    private val lock = Any()
    private val listeners = CopyOnWriteArraySet<(Map<String, Any?>) -> Unit>()
    private lateinit var appContext: Context
    private var initialized = false
    private var reserved = false
    private var active = false
    private var ending = false
    private var currentGeneration = 0
    private var callState = "idle"
    private var sessionId: String? = null
    private var audioFocus = "none"
    private var startedAtElapsedNanos = 0L
    private var startedAtWallMs = 0L

    lateinit var capture: PcmCapture
        private set
    lateinit var playback: PcmPlayback
        private set

    fun initialize(context: Context): AanyaCallRuntime {
        synchronized(lock) {
            if (!initialized) {
                appContext = context.applicationContext
                capture = PcmCapture(appContext)
                playback = PcmPlayback(appContext)
                initialized = true
            }
        }
        return this
    }

    val generation: Int get() = synchronized(lock) { currentGeneration }
    val isActive: Boolean get() = synchronized(lock) { active && !ending }
    val startedAtWallClockMs: Long get() = synchronized(lock) { startedAtWallMs }
    val notificationStateLabel: String
        get() = synchronized(lock) {
            when (callState) {
                "connecting" -> "Connecting"
                "listening" -> "Listening"
                "finalizing_user_turn", "thinking" -> "Thinking"
                "speaking" -> "Speaking"
                "reconnecting" -> "Reconnecting"
                "ending" -> "Ending"
                else -> "Active"
            }
        }

    fun reserve(generation: Int): Boolean = synchronized(lock) {
        if ((active || reserved) && currentGeneration != generation) return@synchronized false
        currentGeneration = generation
        reserved = true
        ending = false
        callState = "connecting"
        startedAtElapsedNanos = SystemClock.elapsedRealtimeNanos()
        startedAtWallMs = System.currentTimeMillis()
        true
    }.also { accepted ->
        if (accepted) emit("call_start_accepted")
    }

    fun matchesReservation(generation: Int): Boolean = synchronized(lock) {
        reserved && !ending && generation == currentGeneration
    }

    fun abortReservation(generation: Int, reason: String) {
        synchronized(lock) {
            if (generation != currentGeneration || active) return
            reserved = false
            ending = false
            callState = "idle"
            sessionId = null
        }
        emit(reason)
    }

    fun activate(generation: Int): Boolean {
        val accepted = synchronized(lock) {
            if (generation != currentGeneration || ending || (!reserved && !active)) {
                return@synchronized false
            }
            reserved = false
            active = true
            true
        }
        if (accepted) emit("foreground_service_started")
        return accepted
    }

    fun requestEnd(generation: Int, reason: String): Boolean {
        val accepted = synchronized(lock) {
            if (generation != currentGeneration || (!active && !reserved)) return@synchronized false
            ending = true
            callState = "ending"
            true
        }
        if (accepted) {
            capture.stopForCallEnd(generation)
            playback.stopForCallEnd(generation)
            emit("end_requested", reason)
        }
        return accepted
    }

    fun completeEnd(generation: Int, reason: String) {
        val ended = synchronized(lock) {
            if (generation > 0 && generation != currentGeneration) return@synchronized false
            if (!active && !reserved && !ending) return@synchronized false
            reserved = false
            active = false
            ending = false
            callState = "ended"
            sessionId = null
            audioFocus = "none"
            true
        }
        if (ended) emit("ended", reason)
    }

    fun isCurrent(generation: Int): Boolean = synchronized(lock) {
        active && !ending && currentGeneration == generation
    }

    fun updateCallState(generation: Int, state: String, newSessionId: String?): Boolean {
        val allowed = setOf(
            "connecting",
            "listening",
            "finalizing_user_turn",
            "thinking",
            "speaking",
            "reconnecting",
            "ending",
        )
        if (state !in allowed) return false
        if (newSessionId != null && !Regex("^[A-Za-z0-9_-]{1,128}$").matches(newSessionId)) {
            return false
        }
        val updated = synchronized(lock) {
            if ((!active && !reserved) || ending || generation != currentGeneration) {
                return@synchronized false
            }
            callState = state
            sessionId = newSessionId ?: sessionId
            true
        }
        if (updated) emit("call_state")
        return updated
    }

    fun reportAudioFocus(generation: Int, focus: String) {
        synchronized(lock) {
            if (generation != currentGeneration || !active || ending) return
            audioFocus = focus
        }
        emit("audio_focus", focus)
    }

    fun addListener(listener: (Map<String, Any?>) -> Unit) {
        listeners.add(listener)
    }

    fun removeListener(listener: (Map<String, Any?>) -> Unit) {
        listeners.remove(listener)
    }

    fun snapshot(event: String, reason: String? = null): Map<String, Any?> = synchronized(lock) {
        mapOf(
            "type" to event,
            "active" to active,
            "reserved" to reserved,
            "ending" to ending,
            "generation" to currentGeneration,
            "state" to callState,
            "sessionId" to sessionId,
            "audioFocus" to audioFocus,
            "startedAtElapsedNanos" to startedAtElapsedNanos,
            "monotonicNanos" to SystemClock.elapsedRealtimeNanos(),
            "reason" to reason,
        )
    }

    private fun emit(event: String, reason: String? = null) {
        val value = snapshot(event, reason)
        Log.i(
            TAG,
            "event=$event generation=${value["generation"]} " +
                "session_id=${value["sessionId"] ?: "none"} " +
                "monotonic_ns=${value["monotonicNanos"]} reason=${reason ?: "none"}",
        )
        listeners.forEach { listener -> listener(value) }
    }
}
