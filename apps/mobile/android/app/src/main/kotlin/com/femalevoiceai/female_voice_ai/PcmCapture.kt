package com.femalevoiceai.female_voice_ai

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.AudioEffect
import android.media.audiofx.NoiseSuppressor
import android.os.Handler
import android.os.HandlerThread
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.util.concurrent.atomic.AtomicInteger
import kotlin.math.max

/** Native Android PCM16 capture with one generation-safe blocking read loop. */
internal class PcmCapture(
    private val context: Context,
) : EventChannel.StreamHandler {
    private enum class State(val wireName: String) {
        UNINITIALIZED("uninitialized"),
        INITIALIZING("initializing"),
        READY("ready"),
        STARTING("starting"),
        RECORDING("recording"),
        STOPPING("stopping"),
        STOPPED("stopped"),
        RELEASED("released"),
        ERROR("error"),
    }

    companion object {
        const val METHOD_CHANNEL = "aira/realtime_pcm_capture"
        const val EVENT_CHANNEL = "aira/realtime_pcm_capture/events"
        const val SAMPLE_RATE_HZ = 16_000
        const val FRAME_BYTES = 1_280
        const val ZERO_AUDIO_WATCHDOG_MS = 900L
        private const val TAG = "AiraCallTiming"
    }

    private val lock = Any()
    private val thread = HandlerThread("aira-pcm-capture").apply { start() }
    private val handler = Handler(thread.looper)
    private val mainHandler = Handler(Looper.getMainLooper())
    private val generation = AtomicInteger(0)
    private var messenger: BinaryMessenger? = null
    private var methodChannel: MethodChannel? = null
    private var eventChannel: EventChannel? = null
    private var eventSink: EventChannel.EventSink? = null
    private var state = State.UNINITIALIZED
    private var recorder: AudioRecord? = null
    private var stopReplies = mutableListOf<MethodChannel.Result>()
    private var stopReason: String? = null
    private var capturedBytes = 0L
    private var capturedFrames = 0L
    private var audioSource: String? = null
    private var disposed = false
    private var echoCancellationRequested = false
    private var echoCancellerActive = false
    private var captureEffects = listOf<AudioEffect>()

    fun attach(newMessenger: BinaryMessenger) {
        synchronized(lock) {
            if (messenger === newMessenger) return
            methodChannel?.setMethodCallHandler(null)
            eventChannel?.setStreamHandler(null)
            messenger = newMessenger
            methodChannel = MethodChannel(newMessenger, METHOD_CHANNEL).also {
                it.setMethodCallHandler(::handle)
            }
            eventChannel = EventChannel(newMessenger, EVENT_CHANNEL).also {
                it.setStreamHandler(this)
            }
            if (state == State.UNINITIALIZED) state = State.READY
        }
    }

    fun detach(oldMessenger: BinaryMessenger) {
        synchronized(lock) {
            if (messenger !== oldMessenger) return
            eventSink = null
            eventChannel?.setStreamHandler(null)
            eventChannel = null
            methodChannel?.setMethodCallHandler(null)
            methodChannel = null
            messenger = null
        }
    }

    override fun onListen(arguments: Any?, events: EventChannel.EventSink) {
        synchronized(lock) { eventSink = events }
        emitState()
    }

    override fun onCancel(arguments: Any?) {
        synchronized(lock) { eventSink = null }
        // Flutter/UI lifetime is not call lifetime. Native capture continues
        // while the foreground call generation remains active.
    }

    private fun handle(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "state" -> result.success(snapshot())
            "prepare" -> prepare(result)
            "start" -> start(call, result)
            "stop" -> stop(call, result, "client_stop")
            "cancel" -> stop(call, result, "client_cancel")
            "dispose" -> {
                stopInternal(result, "flutter_adapter_disposed")
            }
            else -> result.notImplemented()
        }
    }

    private fun start(call: MethodCall, result: MethodChannel.Result) {
        val callGeneration = call.argument<Int>("callGeneration")
        val echoCancellation = call.argument<Boolean>("echoCancellation") ?: false
        if (callGeneration == null || !AanyaCallRuntime.isCurrent(callGeneration)) {
            result.error("stale_call_generation", "The AI call generation is no longer active.", snapshot())
            return
        }
        val token: Int
        synchronized(lock) {
            if (disposed || state == State.RELEASED) {
                result.error("capture_released", "Microphone capture was released.", null)
                return
            }
            if (!AanyaCallRuntime.isCurrent(callGeneration)) {
                result.error(
                    "active_call_required",
                    "Microphone capture requires an active foreground AI call.",
                    snapshot(),
                )
                return
            }
            if (state !in setOf(State.READY, State.STOPPED, State.ERROR)) {
                result.error("capture_busy", "Microphone capture is ${state.wireName}.", snapshot())
                return
            }
            if (eventSink == null) {
                result.error("capture_listener_missing", "Microphone event listener is not attached.", null)
                return
            }
            state = State.INITIALIZING
            capturedBytes = 0
            capturedFrames = 0
            audioSource = null
            stopReason = null
            echoCancellationRequested = echoCancellation
            echoCancellerActive = false
            token = generation.incrementAndGet()
        }
        emitState()
        Log.i(
            TAG,
            "event=native_recorder_prepare_start generation=${AanyaCallRuntime.generation} " +
                "monotonic_ns=${SystemClock.elapsedRealtimeNanos()}",
        )
        handler.post { initializeAndRead(token, result) }
    }

    private fun stop(call: MethodCall, result: MethodChannel.Result, reason: String) {
        val callGeneration = call.argument<Int>("callGeneration")
        if (callGeneration == null || callGeneration != AanyaCallRuntime.generation) {
            result.success(snapshot() + mapOf("staleCommandIgnored" to true))
            return
        }
        stopInternal(result, reason)
    }

    private fun prepare(result: MethodChannel.Result) {
        Log.i(
            TAG,
            "event=native_recorder_prepare_start generation=${AanyaCallRuntime.generation} " +
                "monotonic_ns=${SystemClock.elapsedRealtimeNanos()}",
        )
        handler.post {
            val minimum = AudioRecord.getMinBufferSize(
                SAMPLE_RATE_HZ,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
            )
            if (minimum <= 0) {
                replyError(
                    result,
                    "capture_prepare_failed",
                    IllegalStateException("AudioRecord rejected the capture format."),
                )
            } else {
                synchronized(lock) {
                    if (!disposed && state == State.UNINITIALIZED) state = State.READY
                }
                Log.i(
                    TAG,
                    "event=native_recorder_prepare_end generation=${AanyaCallRuntime.generation} " +
                        "monotonic_ns=${SystemClock.elapsedRealtimeNanos()} min_buffer_bytes=$minimum",
                )
                replySuccess(result, snapshot())
            }
        }
    }

    @SuppressLint("MissingPermission")
    private fun initializeAndRead(token: Int, result: MethodChannel.Result) {
        var active: AudioRecord? = null
        try {
            synchronized(lock) { state = State.STARTING }
            emitState()
            val wantsEchoCancellation = synchronized(lock) { echoCancellationRequested }
            if (wantsEchoCancellation) {
                // The platform's call-audio path, which applies echo
                // cancellation against what the device is playing.
                active = createAndStartRecorder(MediaRecorder.AudioSource.VOICE_COMMUNICATION)
                if (active != null) {
                    val effects = attachCaptureEffects(active.audioSessionId)
                    synchronized(lock) {
                        audioSource = "voice_communication"
                        captureEffects = effects
                        echoCancellerActive = effects.any { it is AcousticEchoCanceler }
                    }
                }
            }
            if (active == null) {
                active = createAndStartRecorder(MediaRecorder.AudioSource.VOICE_RECOGNITION)
                if (active != null) synchronized(lock) { audioSource = "voice_recognition" }
            }
            if (active == null) {
                active = createAndStartRecorder(MediaRecorder.AudioSource.MIC)
                if (active != null) synchronized(lock) { audioSource = "mic" }
            }
            active ?: error("VOICE_RECOGNITION and MIC both failed to initialize.")
            synchronized(lock) {
                if (disposed || token != generation.get()) {
                    active.stop()
                    releaseCaptureEffectsLocked()
                    active.release()
                    state = if (disposed) State.RELEASED else State.STOPPED
                    replySuccess(result, snapshot())
                    completeStopReplies()
                    return
                }
                recorder = active
                state = State.RECORDING
            }
            emit(mapOf("type" to "started", "generation" to token) + snapshot())
            Log.i(
                TAG,
                "event=native_recorder_prepare_end generation=${AanyaCallRuntime.generation} " +
                    "capture_generation=$token monotonic_ns=${SystemClock.elapsedRealtimeNanos()}",
            )
            replySuccess(result, mapOf("generation" to token) + snapshot())
            mainHandler.postDelayed({ zeroAudioWatchdog(token) }, ZERO_AUDIO_WATCHDOG_MS)
            readLoop(active, token)
        } catch (error: Throwable) {
            try { active?.release() } catch (_: Throwable) {}
            synchronized(lock) {
                releaseCaptureEffectsLocked()
                recorder = null
                state = if (disposed) State.RELEASED else State.ERROR
                stopReason = "capture_start_failed"
            }
            emitError("capture_start_failed", error.message ?: "Android microphone failed to start.")
            replyError(result, "capture_start_failed", error)
            completeStopReplies()
        }
    }

    private fun attachCaptureEffects(audioSessionId: Int): List<AudioEffect> {
        val effects = mutableListOf<AudioEffect>()
        if (AcousticEchoCanceler.isAvailable()) {
            try {
                AcousticEchoCanceler.create(audioSessionId)?.let {
                    it.enabled = true
                    effects += it
                }
            } catch (_: Throwable) {}
        }
        if (NoiseSuppressor.isAvailable()) {
            try {
                NoiseSuppressor.create(audioSessionId)?.let {
                    it.enabled = true
                    effects += it
                }
            } catch (_: Throwable) {}
        }
        Log.i(
            TAG,
            "event=capture_effects aec=${effects.any { it is AcousticEchoCanceler }} " +
                "ns=${effects.any { it is NoiseSuppressor }}",
        )
        return effects
    }

    // Caller holds [lock].
    private fun releaseCaptureEffectsLocked() {
        for (effect in captureEffects) {
            try { effect.release() } catch (_: Throwable) {}
        }
        captureEffects = listOf()
        echoCancellerActive = false
    }

    @SuppressLint("MissingPermission")
    private fun createAndStartRecorder(source: Int): AudioRecord? {
        var candidate: AudioRecord? = null
        return try {
            val minimum = AudioRecord.getMinBufferSize(
                SAMPLE_RATE_HZ,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
            )
            if (minimum <= 0) return null
            candidate = AudioRecord.Builder()
                .setAudioSource(source)
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(SAMPLE_RATE_HZ)
                        .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                        .build(),
                )
                .setBufferSizeInBytes(max(minimum * 2, FRAME_BYTES * 2))
                .build()
            if (candidate.state != AudioRecord.STATE_INITIALIZED) {
                candidate.release()
                return null
            }
            candidate.startRecording()
            if (candidate.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                candidate.release()
                null
            } else {
                candidate
            }
        } catch (_: Throwable) {
            try { candidate?.release() } catch (_: Throwable) {}
            null
        }
    }

    private fun readLoop(active: AudioRecord, token: Int) {
        val buffer = ByteArray(FRAME_BYTES)
        var terminalCode: String? = null
        var terminalMessage: String? = null
        var firstPcm = true
        try {
            while (isRecording(token)) {
                val count = active.read(buffer, 0, buffer.size, AudioRecord.READ_BLOCKING)
                if (count > 0) {
                    val completeBytes = count - (count % 2)
                    if (completeBytes > 0) {
                        synchronized(lock) {
                            capturedBytes += completeBytes
                            capturedFrames += completeBytes / 2
                        }
                        if (firstPcm) {
                            firstPcm = false
                            Log.i(
                                TAG,
                                "event=first_mic_pcm generation=${AanyaCallRuntime.generation} " +
                                    "capture_generation=$token " +
                                    "monotonic_ns=${SystemClock.elapsedRealtimeNanos()} bytes=$completeBytes",
                            )
                        }
                        emit(
                            mapOf(
                                "type" to "pcm",
                                "generation" to token,
                                "bytes" to buffer.copyOf(completeBytes),
                                "capturedBytes" to capturedBytes,
                                "capturedFrames" to capturedFrames,
                            ),
                        )
                    }
                } else if (isRecording(token)) {
                    terminalCode = "capture_read_failed"
                    terminalMessage = "Android AudioRecord read failed with code $count."
                    break
                }
            }
        } catch (error: Throwable) {
            if (isRecording(token)) {
                terminalCode = "capture_read_failed"
                terminalMessage = error.message ?: "Android microphone read failed."
            }
        } finally {
            try { active.stop() } catch (_: Throwable) {}
            synchronized(lock) { releaseCaptureEffectsLocked() }
            active.release()
            synchronized(lock) {
                if (recorder === active) recorder = null
                if (terminalCode != null) {
                    state = State.ERROR
                    stopReason = terminalCode
                } else if (disposed) {
                    state = State.RELEASED
                } else {
                    state = State.STOPPED
                }
            }
            if (terminalCode != null) emitError(terminalCode, terminalMessage!!)
            emit(mapOf("type" to "stopped", "generation" to token) + snapshot())
            Log.i(
                TAG,
                "event=mic_stop generation=${AanyaCallRuntime.generation} " +
                    "capture_generation=$token monotonic_ns=${SystemClock.elapsedRealtimeNanos()} " +
                    "captured_bytes=$capturedBytes reason=${stopReason ?: "none"}",
            )
            completeStopReplies()
            if (disposed) thread.quitSafely()
        }
    }

    private fun zeroAudioWatchdog(token: Int) {
        val shouldFail = synchronized(lock) {
            token == generation.get() && state == State.RECORDING && capturedBytes == 0L
        }
        if (!shouldFail) return
        synchronized(lock) {
            state = State.ERROR
            stopReason = "zero_audio_timeout"
        }
        emitError(
            "zero_audio_timeout",
            "The microphone produced no PCM audio within ${ZERO_AUDIO_WATCHDOG_MS} ms.",
        )
        stopRecorderOnly()
    }

    private fun stopInternal(result: MethodChannel.Result?, reason: String) {
        var replyImmediately = false
        synchronized(lock) {
            if (result != null) stopReplies.add(result)
            if (state in setOf(State.RECORDING, State.STARTING, State.INITIALIZING, State.STOPPING)) {
                if (state != State.ERROR) state = State.STOPPING
                stopReason = stopReason ?: reason
                generation.incrementAndGet()
            } else {
                replyImmediately = true
            }
        }
        emitState()
        stopRecorderOnly()
        if (replyImmediately) completeStopReplies()
    }

    private fun stopRecorderOnly() {
        val active = synchronized(lock) { recorder }
        try { active?.stop() } catch (_: Throwable) {}
    }

    private fun isRecording(token: Int): Boolean = synchronized(lock) {
        !disposed && token == generation.get() && state == State.RECORDING
    }

    fun disposeProcessResources() {
        synchronized(lock) {
            if (disposed) return
            disposed = true
            generation.incrementAndGet()
            state = State.RELEASED
            stopReason = "disposed"
        }
        stopRecorderOnly()
        if (synchronized(lock) { recorder == null }) thread.quitSafely()
    }

    fun stopForCallEnd(callGeneration: Int) {
        if (callGeneration != AanyaCallRuntime.generation) return
        stopInternal(null, "call_ended")
    }

    private fun snapshot(): Map<String, Any?> = synchronized(lock) {
        mapOf(
            "state" to state.wireName,
            "sampleRateHz" to SAMPLE_RATE_HZ,
            "channels" to 1,
            "encoding" to "pcm_s16le",
            "frameBytes" to FRAME_BYTES,
            "capturedBytes" to capturedBytes,
            "capturedFrames" to capturedFrames,
            "audioSource" to audioSource,
            "echoCancellationRequested" to echoCancellationRequested,
            "echoCancellerActive" to echoCancellerActive,
            "stopReason" to stopReason,
            "callGeneration" to AanyaCallRuntime.generation,
            "monotonicNanos" to SystemClock.elapsedRealtimeNanos(),
        )
    }

    private fun emitState() = emit(mapOf("type" to "state") + snapshot())

    private fun emitError(code: String, message: String) = emit(
        mapOf("type" to "error", "code" to code, "message" to message) + snapshot(),
    )

    private fun emit(event: Map<String, Any?>) {
        val sink = synchronized(lock) { eventSink } ?: return
        mainHandler.post { sink.success(event) }
    }

    private fun completeStopReplies() {
        val replies = synchronized(lock) {
            val pending = stopReplies
            stopReplies = mutableListOf()
            pending
        }
        val value = snapshot()
        replies.forEach { replySuccess(it, value) }
    }

    private fun replySuccess(result: MethodChannel.Result, value: Any?) {
        mainHandler.post { result.success(value) }
    }

    private fun replyError(result: MethodChannel.Result, code: String, error: Throwable) {
        mainHandler.post {
            result.error(code, error.message ?: "Android microphone capture failed.", snapshot())
        }
    }
}
