import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;

import '../../core/config/aira_api_config.dart';
import '../../core/theme/app_tokens.dart';
import '../companions/domain/companion.dart';
import 'application/aanya_voice_call_controller.dart';
import 'data/http_conversation_api.dart';
import 'data/just_audio_voice_playback.dart';
import 'data/record_voice_recorder.dart';
import 'data/temporary_recording_store.dart';
import 'domain/conversation_session_turn.dart';

typedef AanyaVoiceCallBuilder = Widget Function({
  required Companion companion,
  required VoidCallback onEnd,
});

/// Real push-to-talk screen for Aanya's approved local voice only.
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
  final AanyaVoiceCallController controller;

  @override
  State<AanyaVoiceCallScreen> createState() => _AanyaVoiceCallScreenState();
}

class _AanyaVoiceCallScreenState extends State<AanyaVoiceCallScreen>
    with WidgetsBindingObserver {
  final _scrollController = ScrollController();
  int? _activePointer;
  var _lastTurnCount = 0;

  AanyaVoiceCallController get _controller => widget.controller;

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

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    switch (state) {
      case AppLifecycleState.resumed:
        _controller.handleAppResumed();
        return;
      case AppLifecycleState.inactive:
      case AppLifecycleState.hidden:
      case AppLifecycleState.paused:
      case AppLifecycleState.detached:
        _activePointer = null;
        unawaited(_controller.handleAppBackgrounded());
        return;
    }
  }

  void _handlePointerDown(PointerDownEvent event) {
    if (_activePointer != null || !_controller.state.canStartRecording) return;
    _activePointer = event.pointer;
    unawaited(_beginRecordingWithHaptic());
  }

  Future<void> _beginRecordingWithHaptic() async {
    final started = await _controller.beginRecording();
    if (started && mounted) await HapticFeedback.mediumImpact();
  }

  void _handlePointerUp(PointerUpEvent event) {
    if (_activePointer != event.pointer) return;
    _activePointer = null;
    unawaited(HapticFeedback.selectionClick());
    unawaited(_controller.finishRecording());
  }

  void _handlePointerCancel(PointerCancelEvent event) {
    if (_activePointer != event.pointer) return;
    _activePointer = null;
    unawaited(_controller.cancelRecording());
  }

  void _handleAccessibleTap() {
    final state = _controller.state;
    if (state.phase == VoiceCallPhase.recording) {
      _activePointer = null;
      unawaited(HapticFeedback.selectionClick());
      unawaited(_controller.finishRecording());
    } else if (state.canStartRecording) {
      unawaited(_beginRecordingWithHaptic());
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _controller.removeListener(_handleControllerChanged);
    _activePointer = null;
    _controller.dispose();
    _scrollController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
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
                  onEnd: widget.onEnd,
                ),
                Expanded(
                  child: _ConversationHistory(
                    controller: _scrollController,
                    turns: state.turns,
                  ),
                ),
                _VoiceControls(
                  state: state,
                  onPointerDown: _handlePointerDown,
                  onPointerUp: _handlePointerUp,
                  onPointerCancel: _handlePointerCancel,
                  onAccessibleTap: _handleAccessibleTap,
                  onRetryConnection: () {
                    unawaited(_controller.retryConnection());
                  },
                  onClearError: _controller.clearRecoverableError,
                ),
              ],
            );
          },
        ),
      ),
    );
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
                'Hold the microphone and speak naturally.',
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
                alignRight: true,
              ),
              const SizedBox(height: AppSpacing.sm),
              _MessageBubble(
                key: ValueKey('turn-aanya-${turn.turnId}'),
                speaker: 'Aanya',
                message: turn.assistantResponse,
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
    required this.alignRight,
  });

  final String speaker;
  final String message;
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
    required this.onPointerDown,
    required this.onPointerUp,
    required this.onPointerCancel,
    required this.onAccessibleTap,
    required this.onRetryConnection,
    required this.onClearError,
  });

  final VoiceCallState state;
  final PointerDownEventListener onPointerDown;
  final PointerUpEventListener onPointerUp;
  final PointerCancelEventListener onPointerCancel;
  final VoidCallback onAccessibleTap;
  final VoidCallback onRetryConnection;
  final VoidCallback onClearError;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colors = theme.colorScheme;
    final isRecording = state.phase == VoiceCallPhase.recording;
    final canInteract = state.canStartRecording || isRecording;
    final micLabel = isRecording
        ? 'Release to send'
        : state.canStartRecording
        ? 'Hold to talk'
        : 'Microphone unavailable';
    final micColor = isRecording
        ? colors.error
        : state.canStartRecording
        ? colors.primary
        : colors.surfaceContainerHighest;
    final micForeground = isRecording
        ? colors.onError
        : state.canStartRecording
        ? colors.onPrimary
        : colors.onSurfaceVariant;

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
                    if (state.phase == VoiceCallPhase.checkingConnection ||
                        state.phase == VoiceCallPhase.processing)
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
            Semantics(
              button: true,
              enabled: canInteract,
              label: isRecording
                  ? 'Stop recording and send to Aanya'
                  : 'Hold to record a message for Aanya. Double tap to start '
                        'or stop with TalkBack.',
              onTap: canInteract ? onAccessibleTap : null,
              child: ExcludeSemantics(
                child: Listener(
                  key: const Key('aanya_microphone_control'),
                  behavior: HitTestBehavior.opaque,
                  onPointerDown: canInteract ? onPointerDown : null,
                  onPointerUp: canInteract ? onPointerUp : null,
                  onPointerCancel: canInteract ? onPointerCancel : null,
                  child: AnimatedContainer(
                    duration: const Duration(milliseconds: 180),
                    width: 96,
                    height: 96,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      color: micColor,
                      boxShadow: canInteract
                          ? [
                              BoxShadow(
                                color: micColor.withValues(alpha: 0.3),
                                blurRadius: isRecording ? 28 : 18,
                                spreadRadius: isRecording ? 5 : 1,
                              ),
                            ]
                          : null,
                    ),
                    child: Icon(
                      isRecording ? Icons.stop_rounded : Icons.mic_rounded,
                      size: 42,
                      color: micForeground,
                    ),
                  ),
                ),
              ),
            ),
            const SizedBox(height: AppSpacing.xs),
            Text(
              micLabel,
              style: theme.textTheme.labelLarge?.copyWith(
                color: canInteract ? colors.onSurface : colors.onSurfaceVariant,
                fontWeight: FontWeight.w700,
              ),
            ),
          ],
        ),
      ),
    );
  }

  static IconData _statusIcon(VoiceCallPhase phase) => switch (phase) {
    VoiceCallPhase.recording => Icons.hearing_rounded,
    VoiceCallPhase.playing => Icons.volume_up_rounded,
    VoiceCallPhase.error => Icons.info_outline_rounded,
    VoiceCallPhase.disposed => Icons.close_rounded,
    VoiceCallPhase.idle => Icons.mic_none_rounded,
    VoiceCallPhase.checkingConnection ||
    VoiceCallPhase.processing => Icons.sync_rounded,
  };
}
