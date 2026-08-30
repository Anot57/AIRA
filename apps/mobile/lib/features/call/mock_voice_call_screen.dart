import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../core/constants/app_copy.dart';
import '../companions/domain/companion.dart';

enum MockVoiceActivity { listening, speaking }

/// A completely local, deterministic preview of the voice-call experience.
class MockVoiceCallScreen extends StatefulWidget {
  const MockVoiceCallScreen({
    super.key,
    required this.companion,
    required this.onEnd,
  });

  final Companion companion;
  final VoidCallback onEnd;

  @override
  State<MockVoiceCallScreen> createState() => _MockVoiceCallScreenState();
}

class _MockVoiceCallScreenState extends State<MockVoiceCallScreen>
    with SingleTickerProviderStateMixin {
  static const _activityInterval = Duration(seconds: 6);

  late final AnimationController _orbController;
  Timer? _callTimer;
  Duration _elapsed = Duration.zero;
  MockVoiceActivity _activity = MockVoiceActivity.listening;
  bool _isMuted = false;
  bool _isSpeakerOn = true;
  bool _reduceMotion = false;
  bool _hasConfiguredMotion = false;

  @override
  void initState() {
    super.initState();
    _orbController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 3200),
    );
    _callTimer = Timer.periodic(const Duration(seconds: 1), _onSecondElapsed);
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final reduceMotion = MediaQuery.disableAnimationsOf(context);
    if (_hasConfiguredMotion && reduceMotion == _reduceMotion) return;

    _hasConfiguredMotion = true;
    _reduceMotion = reduceMotion;
    if (_reduceMotion) {
      _orbController
        ..stop()
        ..value = 0.25;
    } else {
      _orbController.repeat();
    }
  }

  void _onSecondElapsed(Timer _) {
    if (!mounted) return;

    setState(() {
      _elapsed += const Duration(seconds: 1);
      if (_elapsed.inSeconds % _activityInterval.inSeconds == 0) {
        _activity = _activity == MockVoiceActivity.listening
            ? MockVoiceActivity.speaking
            : MockVoiceActivity.listening;
      }
    });
  }

  void _toggleMute() {
    setState(() => _isMuted = !_isMuted);
  }

  void _toggleSpeaker() {
    setState(() => _isSpeakerOn = !_isSpeakerOn);
  }

  @override
  void dispose() {
    _callTimer?.cancel();
    _orbController.dispose();
    super.dispose();
  }

  String get _activityLabel {
    if (_isMuted) return 'Muted';
    return switch (_activity) {
      MockVoiceActivity.listening => AppCopy.listeningState,
      MockVoiceActivity.speaking => AppCopy.speakingState,
    };
  }

  String get _activityDescription {
    if (_isMuted) return 'Your mock microphone is muted';
    return switch (_activity) {
      MockVoiceActivity.listening => 'Ready for your voice',
      MockVoiceActivity.speaking => '${widget.companion.name} is responding',
    };
  }

  String get _formattedDuration {
    final minutes = _elapsed.inMinutes.toString().padLeft(2, '0');
    final seconds = (_elapsed.inSeconds % 60).toString().padLeft(2, '0');
    return '$minutes:$seconds';
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colors = theme.colorScheme;

    return Scaffold(
      body: SafeArea(
        child: LayoutBuilder(
          builder: (context, constraints) {
            final orbSize = math.max(
              0.0,
              math.min(constraints.maxWidth - 80, 248.0),
            );

            return SingleChildScrollView(
              padding: const EdgeInsets.fromLTRB(24, 20, 24, 28),
              child: Center(
                child: ConstrainedBox(
                  constraints: BoxConstraints(
                    maxWidth: 520,
                    minHeight: math.max(0, constraints.maxHeight - 48),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.center,
                    children: [
                      _CallHeader(
                        colorScheme: colors,
                        companionName: widget.companion.name,
                      ),
                      const SizedBox(height: 30),
                      Semantics(
                        label:
                            '${AppCopy.callDurationLabel} $_formattedDuration',
                        child: ExcludeSemantics(
                          child: Text(
                            _formattedDuration,
                            key: const Key('call_duration'),
                            style: theme.textTheme.titleMedium?.copyWith(
                              color: colors.onSurfaceVariant,
                              fontFeatures: const [
                                FontFeature.tabularFigures(),
                              ],
                            ),
                          ),
                        ),
                      ),
                      const SizedBox(height: 30),
                      Semantics(
                        image: true,
                        label:
                            'Animated voice frame around a fictional AI '
                            'portrait of ${widget.companion.name}',
                        child: ExcludeSemantics(
                          child: SizedBox.square(
                            dimension: orbSize,
                            child: AnimatedBuilder(
                              animation: _orbController,
                              builder: (context, child) {
                                return Stack(
                                  fit: StackFit.expand,
                                  children: [
                                    CustomPaint(
                                      painter: _VoiceOrbPainter(
                                        progress: _orbController.value,
                                        colorScheme: colors,
                                        accent: widget.companion.accent,
                                        activity: _activity,
                                        isMuted: _isMuted,
                                      ),
                                    ),
                                    Padding(
                                      padding: EdgeInsets.all(orbSize * 0.19),
                                      child: DecoratedBox(
                                        decoration: BoxDecoration(
                                          shape: BoxShape.circle,
                                          border: Border.all(
                                            color: colors.onSurface.withValues(
                                              alpha: 0.2,
                                            ),
                                            width: 2,
                                          ),
                                        ),
                                        child: Padding(
                                          padding: const EdgeInsets.all(4),
                                          child: ClipOval(
                                            child: Image.asset(
                                              widget.companion.assetPath,
                                              fit: BoxFit.cover,
                                              cacheWidth: 480,
                                              errorBuilder:
                                                  (context, error, stackTrace) {
                                                    return ColoredBox(
                                                      color: colors
                                                          .surfaceContainerHigh,
                                                      child: Icon(
                                                        Icons.person_outline,
                                                        color: colors
                                                            .onSurfaceVariant,
                                                        size: 64,
                                                      ),
                                                    );
                                                  },
                                            ),
                                          ),
                                        ),
                                      ),
                                    ),
                                  ],
                                );
                              },
                            ),
                          ),
                        ),
                      ),
                      const SizedBox(height: 32),
                      Semantics(
                        liveRegion: true,
                        label:
                            'Voice status: $_activityLabel. '
                            '$_activityDescription.',
                        child: ExcludeSemantics(
                          child: Column(
                            children: [
                              AnimatedSwitcher(
                                duration: const Duration(milliseconds: 300),
                                child: Text(
                                  _activityLabel,
                                  key: ValueKey(_activityLabel),
                                  style: theme.textTheme.headlineSmall
                                      ?.copyWith(fontWeight: FontWeight.w700),
                                ),
                              ),
                              const SizedBox(height: 6),
                              Text(
                                _activityDescription,
                                style: theme.textTheme.bodyMedium?.copyWith(
                                  color: colors.onSurfaceVariant,
                                ),
                              ),
                            ],
                          ),
                        ),
                      ),
                      const SizedBox(height: 52),
                      Wrap(
                        alignment: WrapAlignment.center,
                        crossAxisAlignment: WrapCrossAlignment.start,
                        spacing: 18,
                        runSpacing: 20,
                        children: [
                          _CallControl(
                            key: const Key('call_mute_button'),
                            label: _isMuted
                                ? AppCopy.unmuteLabel
                                : AppCopy.muteLabel,
                            semanticLabel: _isMuted
                                ? 'Unmute mock microphone'
                                : 'Mute mock microphone',
                            icon: _isMuted
                                ? Icons.mic_off_rounded
                                : Icons.mic_rounded,
                            onPressed: _toggleMute,
                            backgroundColor: colors.surfaceContainerHighest,
                            foregroundColor: colors.onSurface,
                          ),
                          _CallControl(
                            key: const Key('call_speaker_button'),
                            label: _isSpeakerOn ? 'Speaker' : 'Speaker off',
                            semanticLabel: _isSpeakerOn
                                ? 'Turn mock speaker off'
                                : 'Turn mock speaker on',
                            icon: _isSpeakerOn
                                ? Icons.volume_up_rounded
                                : Icons.volume_off_rounded,
                            onPressed: _toggleSpeaker,
                            backgroundColor: _isSpeakerOn
                                ? colors.primaryContainer
                                : colors.surfaceContainerHighest,
                            foregroundColor: _isSpeakerOn
                                ? colors.onPrimaryContainer
                                : colors.onSurface,
                          ),
                          _CallControl(
                            key: const Key('call_end_button'),
                            label: AppCopy.endCallLabel,
                            semanticLabel: 'End mock AI conversation',
                            icon: Icons.call_end_rounded,
                            onPressed: widget.onEnd,
                            backgroundColor: colors.error,
                            foregroundColor: colors.onError,
                          ),
                        ],
                      ),
                      const SizedBox(height: 36),
                      Container(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 16,
                          vertical: 12,
                        ),
                        decoration: BoxDecoration(
                          color: colors.surfaceContainerLow,
                          borderRadius: BorderRadius.circular(16),
                        ),
                        child: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Icon(
                              Icons.lock_outline_rounded,
                              size: 18,
                              color: colors.onSurfaceVariant,
                            ),
                            const SizedBox(width: 8),
                            Flexible(
                              child: Text(
                                AppCopy.mockCallNotice,
                                textAlign: TextAlign.center,
                                style: theme.textTheme.bodySmall?.copyWith(
                                  color: colors.onSurfaceVariant,
                                ),
                              ),
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            );
          },
        ),
      ),
    );
  }
}

class _CallHeader extends StatelessWidget {
  const _CallHeader({required this.colorScheme, required this.companionName});

  final ColorScheme colorScheme;
  final String companionName;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Column(
      children: [
        Semantics(
          label: 'Disclosure: ${AppCopy.fictionalAiLabel}',
          child: ExcludeSemantics(
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 7),
              decoration: BoxDecoration(
                color: colorScheme.primaryContainer,
                borderRadius: BorderRadius.circular(999),
              ),
              child: Text(
                AppCopy.fictionalAiLabel,
                style: theme.textTheme.labelLarge?.copyWith(
                  color: colorScheme.onPrimaryContainer,
                  fontWeight: FontWeight.w700,
                ),
              ),
            ),
          ),
        ),
        const SizedBox(height: 14),
        Text(
          'Mock voice call with $companionName',
          textAlign: TextAlign.center,
          style: theme.textTheme.titleLarge?.copyWith(
            fontWeight: FontWeight.w700,
          ),
        ),
      ],
    );
  }
}

class _CallControl extends StatelessWidget {
  const _CallControl({
    super.key,
    required this.label,
    required this.semanticLabel,
    required this.icon,
    required this.onPressed,
    required this.backgroundColor,
    required this.foregroundColor,
  });

  final String label;
  final String semanticLabel;
  final IconData icon;
  final VoidCallback onPressed;
  final Color backgroundColor;
  final Color foregroundColor;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      button: true,
      label: semanticLabel,
      onTap: onPressed,
      child: ExcludeSemantics(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            IconButton.filled(
              onPressed: onPressed,
              icon: Icon(icon),
              style: IconButton.styleFrom(
                fixedSize: const Size.square(64),
                backgroundColor: backgroundColor,
                foregroundColor: foregroundColor,
              ),
            ),
            const SizedBox(height: 8),
            Text(
              label,
              style: Theme.of(context).textTheme.labelLarge
                  ?.copyWith(fontWeight: FontWeight.w600),
            ),
          ],
        ),
      ),
    );
  }
}

class _VoiceOrbPainter extends CustomPainter {
  const _VoiceOrbPainter({
    required this.progress,
    required this.colorScheme,
    required this.accent,
    required this.activity,
    required this.isMuted,
  });

  final double progress;
  final ColorScheme colorScheme;
  final Color accent;
  final MockVoiceActivity activity;
  final bool isMuted;

  @override
  void paint(Canvas canvas, Size size) {
    final center = size.center(Offset.zero);
    final baseRadius = size.shortestSide * 0.31;
    final phase = progress * math.pi * 2;
    final activityStrength = activity == MockVoiceActivity.speaking
        ? 1.0
        : 0.55;
    final motionStrength = isMuted ? 0.12 : activityStrength;
    final pulse = (math.sin(phase) + 1) / 2;

    final outerPaint = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.5
      ..color = accent.withValues(alpha: isMuted ? 0.12 : 0.16 + pulse * 0.12);
    canvas.drawCircle(
      center,
      baseRadius * (1.32 + pulse * 0.1 * motionStrength),
      outerPaint,
    );

    final glowRect = Rect.fromCircle(center: center, radius: baseRadius * 1.35);
    final glowPaint = Paint()
      ..shader = RadialGradient(
        colors: [
          accent.withValues(alpha: isMuted ? 0.08 : 0.2 * motionStrength),
          accent.withValues(alpha: 0),
        ],
      ).createShader(glowRect);
    canvas.drawCircle(center, baseRadius * 1.35, glowPaint);

    final orbRadius = baseRadius * (1 + pulse * 0.045 * motionStrength);
    final orbRect = Rect.fromCircle(center: center, radius: orbRadius);
    final primary = isMuted
        ? colorScheme.outline
        : activity == MockVoiceActivity.speaking
        ? colorScheme.tertiary
        : accent;
    final secondary = isMuted
        ? colorScheme.surfaceContainerHighest
        : colorScheme.secondary;
    final orbPaint = Paint()
      ..shader = RadialGradient(
        center: Alignment(math.cos(phase) * 0.18, math.sin(phase) * 0.18),
        radius: 0.95,
        colors: [
          Color.lerp(primary, colorScheme.surface, 0.18)!,
          primary,
          secondary,
        ],
        stops: const [0, 0.58, 1],
      ).createShader(orbRect);
    canvas.drawCircle(center, orbRadius, orbPaint);

    final highlightPaint = Paint()
      ..color = colorScheme.onPrimary.withValues(alpha: isMuted ? 0.08 : 0.16);
    canvas.drawCircle(
      center.translate(-orbRadius * 0.28, -orbRadius * 0.3),
      orbRadius * 0.14,
      highlightPaint,
    );
  }

  @override
  bool shouldRepaint(_VoiceOrbPainter oldDelegate) {
    return oldDelegate.progress != progress ||
        oldDelegate.colorScheme != colorScheme ||
        oldDelegate.accent != accent ||
        oldDelegate.activity != activity ||
        oldDelegate.isMuted != isMuted;
  }
}
