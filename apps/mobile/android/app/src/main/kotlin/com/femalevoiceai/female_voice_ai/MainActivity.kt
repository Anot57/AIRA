package com.femalevoiceai.female_voice_ai

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioManager
import android.os.Build
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel

/**
 * UI bridge only. Active call device resources live in [AanyaCallRuntime] and
 * [AanyaCallForegroundService], so losing an Activity/window does not end a
 * call.
 */
class MainActivity : FlutterActivity() {
    private var callBridge: AanyaCallFlutterBridge? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        volumeControlStream = AudioManager.STREAM_MUSIC
        callBridge = AanyaCallFlutterBridge(
            this,
            flutterEngine.dartExecutor.binaryMessenger,
        ).also { it.attach() }
    }

    override fun cleanUpFlutterEngine(flutterEngine: FlutterEngine) {
        callBridge?.detach()
        callBridge = null
        super.cleanUpFlutterEngine(flutterEngine)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        if (callBridge?.onRequestPermissionsResult(requestCode) == true) return
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
    }
}

/** Attaches one Flutter engine to the process-owned native call runtime. */
private class AanyaCallFlutterBridge(
    private val activity: MainActivity,
    private val messenger: BinaryMessenger,
) : EventChannel.StreamHandler {
    companion object {
        private const val METHOD_CHANNEL = "aira/active_call"
        private const val EVENT_CHANNEL = "aira/active_call/events"
        private const val NOTIFICATION_PERMISSION_REQUEST = 4917
    }

    private val runtime = AanyaCallRuntime.initialize(activity.applicationContext)
    private var methodChannel: MethodChannel? = null
    private var eventChannel: EventChannel? = null
    private var eventSink: EventChannel.EventSink? = null
    private var pendingStart: PendingStart? = null
    private val runtimeListener: (Map<String, Any?>) -> Unit = { event ->
        activity.runOnUiThread { eventSink?.success(event) }
    }

    fun attach() {
        runtime.capture.attach(messenger)
        runtime.playback.attach(messenger)
        methodChannel = MethodChannel(messenger, METHOD_CHANNEL).also {
            it.setMethodCallHandler(::handle)
        }
        eventChannel = EventChannel(messenger, EVENT_CHANNEL).also {
            it.setStreamHandler(this)
        }
        runtime.addListener(runtimeListener)
    }

    fun detach() {
        pendingStart?.result?.error(
            "activity_detached",
            "The call screen closed before notification permission completed.",
            null,
        )
        pendingStart = null
        runtime.removeListener(runtimeListener)
        eventSink = null
        eventChannel?.setStreamHandler(null)
        eventChannel = null
        methodChannel?.setMethodCallHandler(null)
        methodChannel = null
        runtime.capture.detach(messenger)
        runtime.playback.detach(messenger)
    }

    override fun onListen(arguments: Any?, events: EventChannel.EventSink) {
        eventSink = events
        events.success(runtime.snapshot("state"))
    }

    override fun onCancel(arguments: Any?) {
        // A Flutter/UI listener is not the owner of the active call.
        eventSink = null
    }

    private fun handle(call: MethodCall, result: MethodChannel.Result) {
        when (call.method) {
            "state" -> result.success(runtime.snapshot("state"))
            "startCall" -> startCall(call, result)
            "endCall" -> endCall(call, result)
            "updateState" -> updateState(call, result)
            else -> result.notImplemented()
        }
    }

    private fun startCall(call: MethodCall, result: MethodChannel.Result) {
        val generation = call.argument<Int>("generation")
        if (generation == null || generation <= 0) {
            result.error("invalid_generation", "Call generation must be positive.", null)
            return
        }
        if (activity.checkSelfPermission(Manifest.permission.RECORD_AUDIO) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            result.error(
                "microphone_permission_missing",
                "Microphone permission must be granted before starting the call service.",
                null,
            )
            return
        }
        if (pendingStart != null) {
            result.error("call_start_pending", "A call start is already pending.", null)
            return
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            activity.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            pendingStart = PendingStart(generation, result)
            activity.requestPermissions(
                arrayOf(Manifest.permission.POST_NOTIFICATIONS),
                NOTIFICATION_PERMISSION_REQUEST,
            )
            return
        }
        dispatchStart(generation, result)
    }

    fun onRequestPermissionsResult(requestCode: Int): Boolean {
        if (requestCode != NOTIFICATION_PERMISSION_REQUEST) return false
        val pending = pendingStart ?: return true
        pendingStart = null
        dispatchStart(pending.generation, pending.result)
        return true
    }

    private fun dispatchStart(generation: Int, result: MethodChannel.Result) {
        if (!runtime.reserve(generation)) {
            result.error(
                "call_already_active",
                "Another Aanya AI call generation is already active.",
                runtime.snapshot("state"),
            )
            return
        }
        try {
            val intent = Intent(activity, AanyaCallForegroundService::class.java).apply {
                action = AanyaCallForegroundService.ACTION_START
                putExtra(AanyaCallForegroundService.EXTRA_GENERATION, generation)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                activity.startForegroundService(intent)
            } else {
                activity.startService(intent)
            }
            result.success(runtime.snapshot("call_start_accepted"))
        } catch (error: Throwable) {
            runtime.abortReservation(generation, "foreground_service_start_failed")
            result.error(
                "foreground_service_start_failed",
                error.message ?: "Android could not start the call service.",
                null,
            )
        }
    }

    private fun endCall(call: MethodCall, result: MethodChannel.Result) {
        val generation = call.argument<Int>("generation")
        if (generation == null || generation <= 0) {
            result.error("invalid_generation", "Call generation must be positive.", null)
            return
        }
        val accepted = runtime.requestEnd(generation, "flutter_end_call")
        if (accepted) {
            activity.startService(
                Intent(activity, AanyaCallForegroundService::class.java).apply {
                    action = AanyaCallForegroundService.ACTION_END
                    putExtra(AanyaCallForegroundService.EXTRA_GENERATION, generation)
                },
            )
        }
        result.success(
            runtime.snapshot(if (accepted) "call_end_accepted" else "stale_end_ignored"),
        )
    }

    private fun updateState(call: MethodCall, result: MethodChannel.Result) {
        val generation = call.argument<Int>("generation")
        val state = call.argument<String>("state")
        val sessionId = call.argument<String>("sessionId")
        if (generation == null || generation <= 0 || state == null) {
            result.error("invalid_call_state", "Generation and state are required.", null)
            return
        }
        if (!runtime.updateCallState(generation, state, sessionId)) {
            result.error("stale_generation", "The native call generation has changed.", null)
            return
        }
        result.success(runtime.snapshot("state_updated"))
    }

    private data class PendingStart(
        val generation: Int,
        val result: MethodChannel.Result,
    )
}
