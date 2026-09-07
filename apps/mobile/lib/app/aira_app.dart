import 'package:flutter/material.dart';

import '../core/constants/app_copy.dart';
import '../core/theme/app_theme.dart';
import '../features/call/aanya_voice_call_screen.dart';
import '../features/companions/data/companion_catalog.dart';
import '../features/onboarding/onboarding_screen.dart';
import 'app_shell.dart';

class AiraApp extends StatefulWidget {
  const AiraApp({
    super.key,
    this.initiallyOnboarded = false,
    this.companionCatalog,
    this.aanyaVoiceCallBuilder,
  });

  /// Allows widget tests to exercise the post-onboarding shell directly.
  /// Production callers keep the default value so onboarding is always shown.
  final bool initiallyOnboarded;

  /// Overrides the production asset catalog in deterministic widget tests.
  final CompanionCatalog? companionCatalog;

  /// Injects the real Aanya route without constructing plugins in widget tests.
  final AanyaVoiceCallBuilder? aanyaVoiceCallBuilder;

  @override
  State<AiraApp> createState() => _AiraAppState();
}

class _AiraAppState extends State<AiraApp> {
  late bool _hasCompletedOnboarding;
  var _themeMode = ThemeMode.dark;

  @override
  void initState() {
    super.initState();
    _hasCompletedOnboarding = widget.initiallyOnboarded;
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: AppCopy.productName,
      debugShowCheckedModeBanner: false,
      theme: AppTheme.light,
      darkTheme: AppTheme.dark,
      themeMode: _themeMode,
      home: AnimatedSwitcher(
        duration: const Duration(milliseconds: 300),
        child: _hasCompletedOnboarding
            ? AppShell(
                key: const ValueKey('app-shell'),
                themeMode: _themeMode,
                companionCatalog: widget.companionCatalog,
                aanyaVoiceCallBuilder: widget.aanyaVoiceCallBuilder,
                onThemeModeChanged: (themeMode) {
                  setState(() => _themeMode = themeMode);
                },
              )
            : OnboardingScreen(
                key: const ValueKey('onboarding'),
                onComplete: () {
                  setState(() => _hasCompletedOnboarding = true);
                },
              ),
      ),
    );
  }
}
