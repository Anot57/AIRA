import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../core/constants/app_copy.dart';

/// Introduces the product's AI identity and gates access to adults.
class OnboardingScreen extends StatefulWidget {
  const OnboardingScreen({super.key, required this.onComplete});

  final VoidCallback onComplete;

  @override
  State<OnboardingScreen> createState() => _OnboardingScreenState();
}

class _OnboardingScreenState extends State<OnboardingScreen> {
  bool _understandsAiIdentity = false;
  bool _confirmsAdult = false;

  bool get _canContinue => _understandsAiIdentity && _confirmsAdult;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colors = theme.colorScheme;

    return Scaffold(
      body: SafeArea(
        child: LayoutBuilder(
          builder: (context, constraints) {
            return SingleChildScrollView(
              padding: const EdgeInsets.fromLTRB(24, 24, 24, 32),
              child: Center(
                child: ConstrainedBox(
                  constraints: BoxConstraints(
                    maxWidth: 560,
                    minHeight: math.max(0, constraints.maxHeight - 56),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Row(
                        children: [
                          _BrandMark(colorScheme: colors),
                          const SizedBox(width: 12),
                          Text(
                            AppCopy.productName,
                            style: theme.textTheme.titleLarge?.copyWith(
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                        ],
                      ),
                      const SizedBox(height: 36),
                      Align(
                        alignment: Alignment.centerLeft,
                        child: _DisclosureBadge(colorScheme: colors),
                      ),
                      const SizedBox(height: 16),
                      Text(
                        'Meet your AI companions',
                        style: theme.textTheme.displaySmall?.copyWith(
                          fontWeight: FontWeight.w700,
                          height: 1.08,
                          letterSpacing: -0.8,
                        ),
                      ),
                      const SizedBox(height: 12),
                      Text(
                        AppCopy.onboardingBody,
                        style: theme.textTheme.bodyLarge?.copyWith(
                          color: colors.onSurfaceVariant,
                          height: 1.5,
                        ),
                      ),
                      const SizedBox(height: 12),
                      Text(
                        'These companions are not real people, therapists, or '
                        'emergency services. In an urgent situation, contact '
                        'local emergency services or a trusted person.',
                        style: theme.textTheme.bodyMedium?.copyWith(
                          color: colors.onSurfaceVariant,
                          height: 1.45,
                        ),
                      ),
                      const SizedBox(height: 24),
                      _ConfirmationCard(
                        understandsAiIdentity: _understandsAiIdentity,
                        confirmsAdult: _confirmsAdult,
                        onAiIdentityChanged: (value) {
                          setState(() => _understandsAiIdentity = value);
                        },
                        onAdultChanged: (value) {
                          setState(() => _confirmsAdult = value);
                        },
                      ),
                      const SizedBox(height: 24),
                      Semantics(
                        button: true,
                        enabled: _canContinue,
                        onTap: _canContinue ? widget.onComplete : null,
                        label: _canContinue
                            ? 'Continue to ${AppCopy.productName}'
                            : 'Continue unavailable. Confirm both statements '
                                  'first.',
                        child: ExcludeSemantics(
                          child: FilledButton.icon(
                            key: const Key('onboarding_continue_button'),
                            onPressed: _canContinue ? widget.onComplete : null,
                            icon: const Icon(Icons.arrow_forward_rounded),
                            label: const Text(AppCopy.continueLabel),
                            style: FilledButton.styleFrom(
                              minimumSize: const Size.fromHeight(56),
                              textStyle: theme.textTheme.titleMedium?.copyWith(
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                          ),
                        ),
                      ),
                      const SizedBox(height: 16),
                      Text(
                        'This preview runs locally and does not record audio.',
                        textAlign: TextAlign.center,
                        style: theme.textTheme.bodySmall?.copyWith(
                          color: colors.onSurfaceVariant,
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

class _BrandMark extends StatelessWidget {
  const _BrandMark({required this.colorScheme});

  final ColorScheme colorScheme;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      label: '${AppCopy.productName} abstract orb logo',
      image: true,
      child: ExcludeSemantics(
        child: Container(
          width: 40,
          height: 40,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            gradient: LinearGradient(
              begin: Alignment.topLeft,
              end: Alignment.bottomRight,
              colors: [colorScheme.primary, colorScheme.tertiary],
            ),
            boxShadow: [
              BoxShadow(
                color: colorScheme.primary.withValues(alpha: 0.24),
                blurRadius: 16,
                spreadRadius: 1,
              ),
            ],
          ),
          child: Icon(
            Icons.graphic_eq_rounded,
            color: colorScheme.onPrimary,
            size: 22,
          ),
        ),
      ),
    );
  }
}

class _DisclosureBadge extends StatelessWidget {
  const _DisclosureBadge({required this.colorScheme});

  final ColorScheme colorScheme;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      label: 'Disclosure: fictional AI companions',
      child: ExcludeSemantics(
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 7),
          decoration: BoxDecoration(
            color: colorScheme.primaryContainer,
            borderRadius: BorderRadius.circular(999),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(
                Icons.auto_awesome_rounded,
                size: 16,
                color: colorScheme.onPrimaryContainer,
              ),
              const SizedBox(width: 7),
              Flexible(
                child: Text(
                  'Fictional AI companions',
                  style: Theme.of(context).textTheme.labelLarge?.copyWith(
                    color: colorScheme.onPrimaryContainer,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _ConfirmationCard extends StatelessWidget {
  const _ConfirmationCard({
    required this.understandsAiIdentity,
    required this.confirmsAdult,
    required this.onAiIdentityChanged,
    required this.onAdultChanged,
  });

  final bool understandsAiIdentity;
  final bool confirmsAdult;
  final ValueChanged<bool> onAiIdentityChanged;
  final ValueChanged<bool> onAdultChanged;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colors = theme.colorScheme;

    return Material(
      color: colors.surfaceContainerLow,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(24),
        side: BorderSide(color: colors.outlineVariant),
      ),
      clipBehavior: Clip.antiAlias,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 16, 20, 4),
            child: Text(
              'Before you enter',
              style: theme.textTheme.titleMedium?.copyWith(
                fontWeight: FontWeight.w700,
              ),
            ),
          ),
          CheckboxListTile(
            key: const Key('onboarding_ai_confirmation'),
            value: understandsAiIdentity,
            onChanged: (value) => onAiIdentityChanged(value ?? false),
            controlAffinity: ListTileControlAffinity.leading,
            contentPadding: const EdgeInsets.symmetric(horizontal: 12),
            title: Text(
              'I understand every companion is fictional AI, not a real person.',
            ),
          ),
          Divider(
            height: 1,
            indent: 20,
            endIndent: 20,
            color: colors.outlineVariant,
          ),
          CheckboxListTile(
            key: const Key('onboarding_age_confirmation'),
            value: confirmsAdult,
            onChanged: (value) => onAdultChanged(value ?? false),
            controlAffinity: ListTileControlAffinity.leading,
            contentPadding: const EdgeInsets.symmetric(horizontal: 12),
            title: const Text(AppCopy.ageConfirmationLabel),
          ),
          const SizedBox(height: 8),
        ],
      ),
    );
  }
}
