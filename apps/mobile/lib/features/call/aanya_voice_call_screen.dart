import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../core/time/ist_timestamp_formatter.dart';
import 'package:http/http.dart' as http;

import '../../core/config/aira_api_config.dart';
import '../../core/theme/app_tokens.dart';
import '../companions/domain/companion.dart';
import 'application/aanya_voice_call_controller.dart';
import 'application/aanya_realtime_client.dart';
import 'application/aanya_realtime_voice_call_controller.dart';
import 'data/android_active_call_service.dart';
import 'data/android_pcm_voice_playback.dart';
import 'data/android_pcm_voice_recorder.dart';
import 'data/http_conversation_api.dart';
import 'data/just_audio_voice_playback.dart';
import 'data/record_voice_recorder.dart';
import 'data/temporary_recording_store.dart';
import 'domain/conversation_session_turn.dart';
import 'domain/realtime_voice_protocol.dart';

typedef AanyaVoiceCallBuilder = Widget Function({
  required Companion companion,
  required VoidCallback onEnd,
});

/// Continuous Android voice-call screen for Aanya's approved local AI voice.
class AanyaVoiceCallScreen extends StatefulWidget {
  const AanyaVoiceCallScreen({
    super.key,
    required this.companion,
    required this.onEnd,
    required this.controller,
  });

  static AanyaVoiceCallScreen production({
    Key? key,
    required Companion companion,
    required VoidCallback onEnd,
    required AiraApiConfig config,
  }) {
    if (!AanyaVoiceCallController.supportsCompanion(companion.id)) {
      throw ArgumentError.value(
        companion.id,
        'companion',
        'The real local voice screen supports only Aanya.',
      );
    }
    return AanyaVoiceCallScreen(
      key: key,
      companion: companion,
      onEnd: onEnd,
      controller: AanyaRealtimeVoiceCallController(
        companionId: companion.id,
        client: AanyaRealtimeClient(companionId: companion.id, config: config),
        recorder: AndroidPcmVoiceRecorder(),
        playback: AndroidPcmVoicePlayback(),
        activeCallPlatform: AndroidActiveCallService(),
      ),
    );
  }

  /// Explicit whole-WAV HTTP fallback for local diagnosis.
  static AanyaVoiceCallScreen httpFallback({
    Key? key,
    required Companion companion,
    required VoidCallback onEnd,
    required AiraApiConfig config,
  }) {
    if (!AanyaVoiceCallController.supportsCompanion(companion.id)) {
      throw ArgumentError.value(companion.id, 'companion');
    }
    return AanyaVoiceCallScreen(
      key: key,
      companion: companion,
      onEnd: onEnd,
      controller: AanyaVoiceCallController(
        companionId: companion.id,
        api: HttpConversationApi(config: config, client: http.Client()),
        recorder: RecordVoiceRecorder(),
        playback: JustAudioVoicePlayback(),
        recordingStore: AppTemporaryRecordingStore(),
      ),
    );
  }

  final Companion companion;
  final VoidCallback onEnd;
  final AanyaVoiceCallCoordinator controller;

  @override
  State<AanyaVoiceCallScreen> createState() => _AanyaVoiceCallScreenState();
}

class _AanyaVoiceCallScreenState extends State<AanyaVoiceCallScreen>
    with WidgetsBindingObserver {
  final _scrollController = ScrollController();
  var _lastTurnCount = 0;
  Timer? _durationTicker;
  var _allowPop = false;
  var _endingFromUi = false;

  AanyaVoiceCallCoordinator get _controller => widget.controller;

  @override
  void initState() {
    super.initState();
    if (!AanyaVoiceCallController.supportsCompanion(widget.companion.id)) {
      throw ArgumentError.value(
        widget.companion.id,
        'companion',
        'The real local voice screen supports only Aanya.',
      );
    }
    WidgetsBinding.instance.addObserver(this);
    _controller.addListener(_handleControllerChanged);
    unawaited(_controller.initialize());
  }

  @override
  void didUpdateWidget(covariant AanyaVoiceCallScreen oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.controller, widget.controller)) {
      throw StateError('The voice controller cannot change during a call.');
    }
  }

  void _handleControllerChanged() {
    if (!mounted) return;
    _syncDurationTicker();
    final turnCount = _controller.state.turns.length;
    if (turnCount == _lastTurnCount) return;
    _lastTurnCount = turnCount;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !_scrollController.hasClients) return;
      unawaited(
        _scrollController.animateTo(
          _scrollController.position.maxScrollExtent,
          duration: const Duration(milliseconds: 280),
          curve: Curves.easeOut,
        ),
      );
    });
  }

  void _syncDurationTicker() {
    if (_controller.state.callActive) {
      _durationTicker ??= Timer.periodic(const Duration(seconds: 1), (_) {
        if (mounted) setState(() {});
      });
    } else {
      _durationTicker?.cancel();
      _durationTicker = null;
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    switch (state) {
      case AppLifecycleState.resumed:
        _controller.handleAppResumed();
        return;
      case AppLifecycleState.inactive:
        // Notification shade, volume UI, system dialogs, and focus changes are
        // transient. They are never route/call disposal.
        return;
      case AppLifecycleState.hidden:
      case AppLifecycleState.paused:
        unawaited(_controller.handleAppBackgrounded());
        return;
      case AppLifecycleState.detached:
        // Native foreground-service ownership is independent from Activity
        // attachment. Widget disposal handles a genuinely destroyed engine.
        return;
    }
  }

  Future<void> _startCall() async {
    final started = await _controller.startCall();
    if (started && mounted) await HapticFeedback.mediumImpact();
  }

  Future<void> _endCallAndExit() async {
    if (_endingFromUi) return;
    _endingFromUi = true;
    await HapticFeedback.selectionClick();
    await _controller.endCall();
    if (!mounted) return;
    setState(() => _allowPop = true);
    await WidgetsBinding.instance.endOfFrame;
    if (!mounted) return;
    widget.onEnd();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _durationTicker?.cancel();
    _controller.removeListener(_handleControllerChanged);
    _controller.dispose();
    _scrollController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: _allowPop,
      onPopInvokedWithResult: (didPop, result) {
        if (!didPop) unawaited(_endCallAndExit());
      },
      child: Scaffold(
        key: const Key('aanya_voice_call_screen'),
        body: SafeArea(
          child: AnimatedBuilder(
            animation: _controller,
            builder: (context, _) {
              final state = _controller.state;
              return Column(
                children: [
                  _CallHeader(
                    companion: widget.companion,
                    connection: state.connection,
                    onEnd: () => unawaited(_endCallAndExit()),
                  ),
                  if (state.crisisResources.isNotEmpty)
                    _CrisisResourcesBanner(
                      resources: state.crisisResources,
                      onDismiss: _controller.dismissCrisisResources,
                    ),
                  Expanded(
                    child: _ConversationHistory(
                      controller: _scrollController,
                      turns: state.turns,
                    ),
                  ),
                  _VoiceControls(
                    state: state,
                    duration: _callDuration(state),
                    onStartCall: () => unawaited(_startCall()),
                    onEndCall: () => unawaited(_endCallAndExit()),
                    onRetryConnection: () {
                      unawaited(_controller.retryConnection());
                    },
                    onClearError: _controller.clearRecoverableError,
                    onInterrupt: () {
                      unawaited(_controller.cancelActiveTurn());
                    },
                  ),
                ],
              );
            },
          ),
        ),
      ),
    );
  }

  Duration _callDuration(VoiceCallState state) {
    final startedAt = state.callStartedAt;
    if (startedAt == null) return Duration.zero;
    final elapsed = DateTime.now().difference(startedAt);
    return elapsed.isNegative ? Duration.zero : elapsed;
  }
}

class _CallHeader extends StatelessWidget {
  const _CallHeader({
    required this.companion,
    required this.connection,
    required this.onEnd,
  });

  final Companion companion;
  final LocalAiConnection connection;
  final VoidCallback onEnd;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colors = theme.colorScheme;
    return Padding(
      padding: const EdgeInsets.fromLTRB(
        AppSpacing.sm,
        AppSpacing.sm,
        AppSpacing.lg,
        AppSpacing.sm,
      ),
      child: Row(
        children: [
          IconButton(
            key: const Key('aanya_end_call'),
            tooltip: 'End voice conversation',
            onPressed: onEnd,
            icon: const Icon(Icons.arrow_back_rounded),
          ),
          const SizedBox(width: AppSpacing.xs),
          Semantics(
            image: true,
            label: 'Portrait of Aanya, a fictional AI companion',
            child: ExcludeSemantics(
              child: CircleAvatar(
                radius: 30,
                backgroundColor: companion.accent.withValues(alpha: 0.2),
                backgroundImage: AssetImage(companion.assetPath),
              ),
            ),
          ),
          const SizedBox(width: AppSpacing.sm),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  companion.name,
                  style: theme.textTheme.titleLarge?.copyWith(
                    fontWeight: FontWeight.w700,
                  ),
                ),
                Text(
                  'AI companion',
                  key: const Key('aanya_ai_disclosure'),
                  style: theme.textTheme.bodySmall?.copyWith(
                    color: colors.onSurfaceVariant,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ],
            ),
          ),
          _ConnectionBadge(connection: connection),
        ],
      ),
    );
  }
}

class _ConnectionBadge extends StatelessWidget {
  const _ConnectionBadge({required this.connection});

  final LocalAiConnection connection;

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    final (label, icon, foreground, background) = switch (connection) {
      LocalAiConnection.connected => (
        'Connected',
        Icons.check_circle_rounded,
        colors.primary,
        colors.primaryContainer,
      ),
      LocalAiConnection.offline => (
        'Offline',
        Icons.cloud_off_rounded,
        colors.error,
        colors.errorContainer,
      ),
      LocalAiConnection.checking => (
        'Checking',
        Icons.sync_rounded,
        colors.secondary,
        colors.secondaryContainer,
      ),
      LocalAiConnection.unknown => (
        'Waiting',
        Icons.cloud_outlined,
        colors.onSurfaceVariant,
        colors.surfaceContainerHighest,
      ),
    };

    return Semantics(
      label: 'Local AI connection: $label',
      child: ExcludeSemantics(
        child: Container(
          key: const Key('local_ai_connection'),
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 7),
          decoration: BoxDecoration(
            color: background,
            borderRadius: BorderRadius.circular(AppRadii.pill),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(icon, size: 15, color: foreground),
              const SizedBox(width: 5),
              Text(
                'Local AI • $label',
                style: Theme.of(context).textTheme.labelSmall
                    ?.copyWith(color: foreground, fontWeight: FontWeight.w700),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _CrisisResourcesBanner extends StatelessWidget {
  const _CrisisResourcesBanner({
    required this.resources,
    required this.onDismiss,
  });

  final List<RealtimeCrisisResource> resources;
  final VoidCallback onDismiss;

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    final textTheme = Theme.of(context).textTheme;
    return Semantics(
      container: true,
      liveRegion: true,
      label: 'Crisis support resources',
      child: Container(
        key: const Key('crisis_resources_banner'),
        width: double.infinity,
        margin: const EdgeInsets.fromLTRB(
          AppSpacing.md,
          0,
          AppSpacing.md,
          AppSpacing.xs,
        ),
        padding: const EdgeInsets.all(AppSpacing.md),
        decoration: BoxDecoration(
          color: colors.errorContainer,
          borderRadius: BorderRadius.circular(AppRadii.medium),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Icon(Icons.support_rounded, color: colors.onErrorContainer),
            const SizedBox(width: AppSpacing.sm),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    'You deserve real support right now',
                    style: textTheme.titleSmall?.copyWith(
                      color: colors.onErrorContainer,
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                  const SizedBox(height: AppSpacing.xxs),
                  Text(
                    'Aanya is an AI and can\'t help in an emergency. '
                    'Please reach out now:',
                    style: textTheme.bodyMedium?.copyWith(
                      color: colors.onErrorContainer,
                    ),
                  ),
                  for (final resource in resources)
                    Padding(
                      padding: const EdgeInsets.only(top: AppSpacing.xxs),
                      child: SelectableText(
                        '${resource.label}: ${resource.phone}',
                        style: textTheme.bodyLarge?.copyWith(
                          color: colors.onErrorContainer,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                ],
              ),
            ),
            IconButton(
              key: const Key('crisis_resources_dismiss'),
              tooltip: 'Dismiss crisis resources',
              onPressed: onDismiss,
              color: colors.onErrorContainer,
              icon: const Icon(Icons.close_rounded),
            ),
          ],
        ),
      ),
    );
  }
}

class _ConversationHistory extends StatelessWidget {
  const _ConversationHistory({required this.controller, required this.turns});

  final ScrollController controller;
  final List<ConversationSessionTurn> turns;

  @override
  Widget build(BuildContext context) {
    if (turns.isEmpty) {
      return Center(
        child: SingleChildScrollView(
          padding: const EdgeInsets.symmetric(horizontal: AppSpacing.xxl),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              Icon(
                Icons.graphic_eq_rounded,
                size: 42,
                color: Theme.of(context).colorScheme.primary,
              ),
              const SizedBox(height: AppSpacing.md),
              Text(
                'Start the call, then speak naturally when Aanya is listening.',
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.titleMedium
                    ?.copyWith(fontWeight: FontWeight.w700),
              ),
              const SizedBox(height: AppSpacing.xs),
              Text(
                'Turns stay visible only in this screen session. Aanya does '
                'not have persistent conversation memory yet.',
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.bodySmall?.copyWith(
                  color: Theme.of(context).colorScheme.onSurfaceVariant,
                ),
              ),
            ],
          ),
        ),
      );
    }

    return ListView.builder(
      key: const Key('voice_session_history'),
      controller: controller,
      padding: const EdgeInsets.fromLTRB(
        AppSpacing.lg,
        AppSpacing.sm,
        AppSpacing.lg,
        AppSpacing.xl,
      ),
      itemCount: turns.length,
      itemBuilder: (context, index) {
        final turn = turns[index];
        return Padding(
          padding: const EdgeInsets.only(bottom: AppSpacing.lg),
          child: Column(
            children: [
              _MessageBubble(
                key: ValueKey('turn-user-${turn.turnId}'),
                speaker: 'You',
                message: turn.userTranscript,
                createdAt: turn.userCreatedAt,
                alignRight: true,
              ),
              const SizedBox(height: AppSpacing.sm),
              _MessageBubble(
                key: ValueKey('turn-aanya-${turn.turnId}'),
                speaker: 'Aanya',
                message: turn.assistantResponse,
                createdAt: turn.assistantCreatedAt,
                alignRight: false,
              ),
            ],
          ),
        );
      },
    );
  }
}

class _MessageBubble extends StatelessWidget {
  const _MessageBubble({
    super.key,
    required this.speaker,
    required this.message,
    required this.createdAt,
    required this.alignRight,
  });

  final String speaker;
  final String message;
  final DateTime createdAt;
  final bool alignRight;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colors = theme.colorScheme;
    return Align(
      alignment: alignRight ? Alignment.centerRight : Alignment.centerLeft,
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 440),
        child: DecoratedBox(
          decoration: BoxDecoration(
            color: alignRight
                ? colors.primaryContainer
                : colors.surfaceContainerHigh,
            borderRadius: BorderRadius.circular(AppRadii.medium),
          ),
          child: Padding(
            padding: const EdgeInsets.all(AppSpacing.md),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  speaker,
                  style: theme.textTheme.labelMedium?.copyWith(
                    color: alignRight
                        ? colors.onPrimaryContainer
                        : colors.primary,
                    fontWeight: FontWeight.w800,
                  ),
                ),
                const SizedBox(height: AppSpacing.xxs),
                Text(
                  IstTimestampFormatter.format(createdAt),
                  key: ValueKey('message-timestamp-${key.toString()}'),
                  style: theme.textTheme.labelSmall?.copyWith(
                    color: alignRight
                        ? colors.onPrimaryContainer
                        : colors.onSurfaceVariant,
                    fontFeatures: const [FontFeature.tabularFigures()],
                  ),
                ),
                const SizedBox(height: AppSpacing.xxs),
                Text(
                  message,
                  style: theme.textTheme.bodyLarge?.copyWith(
                    color: alignRight
                        ? colors.onPrimaryContainer
                        : colors.onSurface,
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _VoiceControls extends StatelessWidget {
  const _VoiceControls({
    required this.state,
    required this.duration,
    required this.onStartCall,
    required this.onEndCall,
    required this.onRetryConnection,
    required this.onClearError,
    required this.onInterrupt,
  });

  final VoiceCallState state;
  final Duration duration;
  final VoidCallback onStartCall;
  final VoidCallback onEndCall;
  final VoidCallback onRetryConnection;
  final VoidCallback onClearError;
  final VoidCallback onInterrupt;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colors = theme.colorScheme;
    final active = state.callActive;
    final busy =
        state.phase == VoiceCallPhase.connecting ||
        state.phase == VoiceCallPhase.reconnecting ||
        state.phase == VoiceCallPhase.ending;

    return Material(
      color: colors.surface,
      elevation: 10,
      shadowColor: colors.shadow.withValues(alpha: 0.24),
      child: SafeArea(
        top: false,
        minimum: const EdgeInsets.fromLTRB(
          AppSpacing.lg,
          AppSpacing.sm,
          AppSpacing.lg,
          AppSpacing.md,
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Semantics(
              liveRegion: true,
              label: 'Voice status: ${state.statusLabel}',
              child: ExcludeSemantics(
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    if (busy)
                      const SizedBox.square(
                        dimension: 16,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    else
                      Icon(
                        _statusIcon(state.phase),
                        size: 18,
                        color: colors.primary,
                      ),
                    const SizedBox(width: AppSpacing.xs),
                    Flexible(
                      child: Text(
                        state.statusLabel,
                        key: const Key('voice_status'),
                        style: theme.textTheme.titleMedium?.copyWith(
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
            if (state.userMessage case final message?) ...[
              const SizedBox(height: AppSpacing.sm),
              Container(
                key: const Key('voice_error_message'),
                width: double.infinity,
                padding: const EdgeInsets.all(AppSpacing.sm),
                decoration: BoxDecoration(
                  color: colors.errorContainer,
                  borderRadius: BorderRadius.circular(AppRadii.small),
                ),
                child: Row(
                  children: [
                    Icon(
                      Icons.info_outline_rounded,
                      color: colors.onErrorContainer,
                    ),
                    const SizedBox(width: AppSpacing.sm),
                    Expanded(
                      child: Text(
                        message,
                        style: theme.textTheme.bodySmall?.copyWith(
                          color: colors.onErrorContainer,
                        ),
                      ),
                    ),
                    TextButton(
                      key: state.canRetryConnection
                          ? const Key('retry_local_ai')
                          : const Key('clear_voice_error'),
                      onPressed: state.canRetryConnection
                          ? onRetryConnection
                          : onClearError,
                      child: Text(
                        state.canRetryConnection ? 'Retry' : 'Try again',
                      ),
                    ),
                  ],
                ),
              ),
            ],
            const SizedBox(height: AppSpacing.sm),
            if (active) ...[
              Text(
                _formatDuration(duration),
                key: const Key('aanya_call_duration'),
                style: theme.textTheme.titleLarge?.copyWith(
                  fontFeatures: const [FontFeature.tabularFigures()],
                  fontWeight: FontWeight.w700,
                ),
              ),
              const SizedBox(height: AppSpacing.sm),
              if (state.phase == VoiceCallPhase.thinking ||
                  state.phase == VoiceCallPhase.speaking) ...[
                OutlinedButton.icon(
                  key: const Key('aanya_interrupt_button'),
                  onPressed: onInterrupt,
                  style: OutlinedButton.styleFrom(
                    minimumSize: const Size(190, 48),
                  ),
                  icon: const Icon(Icons.pan_tool_rounded),
                  label: const Text('INTERRUPT'),
                ),
                const SizedBox(height: AppSpacing.sm),
              ],
              FilledButton.icon(
                key: const Key('aanya_end_call_button'),
                onPressed: state.phase == VoiceCallPhase.ending
                    ? null
                    : onEndCall,
                style: FilledButton.styleFrom(
                  backgroundColor: colors.error,
                  foregroundColor: colors.onError,
                  minimumSize: const Size(190, 54),
                ),
                icon: const Icon(Icons.call_end_rounded),
                label: const Text('END CALL'),
              ),
            ] else ...[
              FilledButton.icon(
                key: const Key('aanya_start_call'),
                onPressed: state.canStartCall ? onStartCall : null,
                style: FilledButton.styleFrom(minimumSize: const Size(190, 54)),
                icon: const Icon(Icons.call_rounded),
                label: const Text('START CALL'),
              ),
            ],
            const SizedBox(height: AppSpacing.xs),
            Text(
              active ? 'The call stays active until you end it.' : 'Aanya is an AI companion, not a human or emergency service.',
              textAlign: TextAlign.center,
              style: theme.textTheme.labelMedium?.copyWith(
                color: colors.onSurfaceVariant,
              ),
            ),
          ],
        ),
      ),
    );
  }

  static IconData _statusIcon(VoiceCallPhase phase) => switch (phase) {
    VoiceCallPhase.listening ||
    VoiceCallPhase.recording => Icons.hearing_rounded,
    VoiceCallPhase.speaking ||
    VoiceCallPhase.playing => Icons.graphic_eq_rounded,
    VoiceCallPhase.error => Icons.info_outline_rounded,
    VoiceCallPhase.disposed => Icons.close_rounded,
    VoiceCallPhase.idle ||
    VoiceCallPhase.ready ||
    VoiceCallPhase.ended => Icons.call_rounded,
    VoiceCallPhase.ending => Icons.call_end_rounded,
    VoiceCallPhase.thinking ||
    VoiceCallPhase.finalizingUserTurn => Icons.psychology_alt_rounded,
    VoiceCallPhase.connecting ||
    VoiceCallPhase.reconnecting => Icons.sync_rounded,
    VoiceCallPhase.checkingConnection ||
    VoiceCallPhase.processing => Icons.sync_rounded,
  };

  static String _formatDuration(Duration duration) {
    final hours = duration.inHours;
    final minutes = duration.inMinutes.remainder(60).toString().padLeft(2, '0');
    final seconds = duration.inSeconds.remainder(60).toString().padLeft(2, '0');
    return hours > 0 ? '$hours:$minutes:$seconds' : '$minutes:$seconds';
  }
}
