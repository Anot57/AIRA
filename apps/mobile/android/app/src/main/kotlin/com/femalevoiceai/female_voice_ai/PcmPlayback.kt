package com.femalevoiceai.female_voice_ai

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.os.Handler
import android.os.HandlerThread
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.util.concurrent.atomic.AtomicInteger
import kotlin.math.max

/** Serial, generation-safe AudioTrack owner retained by the call service. */
internal class PcmPlayback(private val context: Context) {
    companion object {
        const val METHOD_CHANNEL = "aira/realtime_pcm_playback"
        private const val TAG = "AiraCallTiming"
    }

    private val lock = Any()
    private val thread = HandlerThread("aira-pcm-playback").apply { start() }
    private val handler = Handler(thread.looper)
    private val mainHandler = Handler(Looper.getMainLooper())
    private val generation = AtomicInteger(0)
    private var messenger: BinaryMessenger? = null
    private var methodChannel: MethodChannel? = null
    private var track: AudioTrack? = null
    private var writtenFrames = 0L
    private var pausedForFocus = false
    private var firstWriteLogged = false
    private var disposed = false

    fun attach(newMessenger: BinaryMessenger) {
        synchronized(lock) {
            if (messenger === newMessenger) return
            methodChannel?.setMethodCallHandler(null)
            messenger = newMessenger
            methodChannel = MethodChannel(newMessenger, METHOD_CHANNEL).also {
                it.setMethodCallHandler(::handle)
            }
        }
    }

    fun detach(oldMessenger: BinaryMessenger) {
        synchronized(lock) {
            if (messenger !== oldMessenger) return
            methodChannel?.setMethodCallHandler(null)
            methodChannel = null
            messenger = null
        }
    }

    private fun handle(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "prepare" -> prepare(call, result)
            "start" -> start(call, result)
            "append" -> append(call, result)
            "finish" -> finish(result)
            "stop" -> stop(result)
            "dispose" -> {
                // Detaching a Flutter owner must not dispose process-owned I/O.
                stop(result)
            }
            else -> result.notImplemented()
        }
    }

    private fun prepare(call: MethodCall, result: MethodChannel.Result) {
        val sampleRate = call.argument<Int>("sampleRateHz") ?: 24_000
        if (sampleRate !in 8_000..48_000) {
            result.error("invalid_audio_format", "Unsupported PCM playback format.", null)
            return
        }
        val started = SystemClock.elapsedRealtimeNanos()
        Log.i(
            TAG,
            "event=audio_track_prepare_start generation=${AanyaCallRuntime.generation} " +
                "monotonic_ns=$started",
        )
        handler.post {
            val minimum = AudioTrack.getMinBufferSize(
                sampleRate,
                AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
            )
            if (minimum <= 0) {
                replyError(result, "playback_prepare_failed", null)
            } else {
                Log.i(
                    TAG,
                    "event=audio_track_prepare_end generation=${AanyaCallRuntime.generation} " +
                        "monotonic_ns=${SystemClock.elapsedRealtimeNanos()} min_buffer_bytes=$minimum",
                )
                replySuccess(result)
            }
        }
    }

    private fun start(call: MethodCall, result: MethodChannel.Result) {
        val sampleRate = call.argument<Int>("sampleRateHz")
        val channels = call.argument<Int>("channels")
        val encoding = call.argument<String>("encoding")
        if (disposed || !AanyaCallRuntime.isActive || sampleRate == null ||
            sampleRate !in 8_000..48_000 || channels != 1 || encoding != "pcm_s16le"
        ) {
            result.error("invalid_audio_format", "Unsupported or inactive PCM playback.", null)
            return
        }
        val token = generation.incrementAndGet()
        val started = SystemClock.elapsedRealtimeNanos()
        Log.i(
            TAG,
            "event=audio_track_prepare_start generation=${AanyaCallRuntime.generation} " +
                "monotonic_ns=$started",
        )
        handler.post {
            releaseTrack()
            if (disposed || token != generation.get() || !AanyaCallRuntime.isActive) {
                replySuccess(result)
                return@post
            }
            try {
                val minimum = AudioTrack.getMinBufferSize(
                    sampleRate,
                    AudioFormat.CHANNEL_OUT_MONO,
                    AudioFormat.ENCODING_PCM_16BIT,
                )
                if (minimum <= 0) error("AudioTrack rejected the sample rate.")
                track = AudioTrack.Builder()
                    .setAudioAttributes(
                        AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_MEDIA)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                            .build(),
                    )
                    .setAudioFormat(
                        AudioFormat.Builder()
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .setSampleRate(sampleRate)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                            .build(),
                    )
                    .setBufferSizeInBytes(max(minimum * 2, sampleRate / 5 * 2))
                    .setTransferMode(AudioTrack.MODE_STREAM)
                    .build()
                    .also { it.play() }
                writtenFrames = 0
                pausedForFocus = false
                firstWriteLogged = false
                Log.i(
                    TAG,
                    "event=audio_track_prepare_end generation=${AanyaCallRuntime.generation} " +
                        "monotonic_ns=${SystemClock.elapsedRealtimeNanos()}",
                )
                replySuccess(result)
            } catch (error: Throwable) {
                releaseTrack()
                replyError(result, "playback_start_failed", error)
            }
        }
    }

    private fun append(call: MethodCall, result: MethodChannel.Result) {
        val bytes = call.arguments as? ByteArray
        val token = generation.get()
        if (disposed || bytes == null || bytes.isEmpty() || bytes.size % 2 != 0) {
            result.error("invalid_audio_frame", "PCM16 bytes must be non-empty and even.", null)
            return
        }
        handler.post {
            val active = track
            if (disposed || token != generation.get() || active == null) {
                replyError(result, "stale_audio_frame", null)
                return@post
            }
            try {
                var offset = 0
                while (offset < bytes.size && token == generation.get()) {
                    val count = active.write(
                        bytes,
                        offset,
                        bytes.size - offset,
                        AudioTrack.WRITE_BLOCKING,
                    )
                    if (count <= 0) error("AudioTrack write failed: $count")
                    offset += count
                    writtenFrames += count / 2
                    if (!firstWriteLogged) {
                        firstWriteLogged = true
                        Log.i(
                            TAG,
                            "event=first_audio_track_write generation=${AanyaCallRuntime.generation} " +
                                "monotonic_ns=${SystemClock.elapsedRealtimeNanos()} bytes=$count",
                        )
                    }
                }
                replySuccess(result)
            } catch (error: Throwable) {
                replyError(result, "playback_write_failed", error)
            }
        }
    }

    private fun finish(result: MethodChannel.Result) {
        val token = generation.get()
        handler.post(object : Runnable {
            override fun run() {
                val active = track
                if (disposed || token != generation.get() || active == null) {
                    replySuccess(result)
                    return
                }
                // AudioTrack completion, not text completion, owns the
                // speaking -> listening boundary. While focus is transiently
                // lost, keep the same generation paused and wait for gain.
                if (pausedForFocus || active.playbackHeadPosition.toLong() < writtenFrames) {
                    handler.postDelayed(this, 10)
                    return
                }
                releaseTrack()
                replySuccess(result)
            }
        })
    }

    private fun stop(result: MethodChannel.Result) {
        generation.incrementAndGet()
        handler.post {
            releaseTrack()
            replySuccess(result)
        }
    }

    fun stopForCallEnd(callGeneration: Int) {
        if (callGeneration != AanyaCallRuntime.generation) return
        generation.incrementAndGet()
        handler.post { releaseTrack() }
    }

    fun pauseForFocusLoss(callGeneration: Int) {
        if (!AanyaCallRuntime.isCurrent(callGeneration)) return
        val token = generation.get()
        handler.post {
            val active = track ?: return@post
            if (token != generation.get() || !AanyaCallRuntime.isCurrent(callGeneration)) return@post
            try {
                active.pause()
                pausedForFocus = true
            } catch (_: Throwable) {
                // The call/session stays active; playback failure is reported
                // by the normal append/finish path if the track is unusable.
            }
        }
    }

    fun resumeAfterFocusGain(callGeneration: Int) {
        if (!AanyaCallRuntime.isCurrent(callGeneration)) return
        val token = generation.get()
        handler.post {
            val active = track ?: return@post
            if (token != generation.get() || !AanyaCallRuntime.isCurrent(callGeneration)) return@post
            if (pausedForFocus) {
                try {
                    active.play()
                    pausedForFocus = false
                } catch (_: Throwable) {}
            }
        }
    }

    fun setDucked(ducked: Boolean) {
        val token = generation.get()
        handler.post {
            val active = track ?: return@post
            if (token != generation.get()) return@post
            try { active.setVolume(if (ducked) 0.2f else 1.0f) } catch (_: Throwable) {}
        }
    }

    fun disposeProcessResources() {
        if (disposed) return
        disposed = true
        generation.incrementAndGet()
        handler.post {
            releaseTrack()
            thread.quitSafely()
        }
    }

    private fun releaseTrack() {
        val active = track ?: return
        track = null
        pausedForFocus = false
        try { active.pause() } catch (_: Throwable) {}
        try { active.flush() } catch (_: Throwable) {}
        try { active.stop() } catch (_: Throwable) {}
        active.release()
        writtenFrames = 0
        firstWriteLogged = false
    }

    private fun replySuccess(result: MethodChannel.Result) =
        mainHandler.post { result.success(null) }

    private fun replyError(result: MethodChannel.Result, code: String, error: Throwable?) =
        mainHandler.post {
            result.error(code, error?.message ?: "PCM playback is not active.", null)
        }
}
