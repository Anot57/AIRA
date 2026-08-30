import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../../core/theme/app_tokens.dart';
import '../domain/companion.dart';

/// A detailed, safety-forward introduction to one fictional AI companion.
class CompanionDetailsScreen extends StatelessWidget {
  const CompanionDetailsScreen({
    super.key,
    required this.companion,
    required this.onStartVoiceCall,
  });

  final Companion companion;
  final VoidCallback onStartVoiceCall;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colors = theme.colorScheme;
    final accent = companion.accent;

    return Scaffold(
      appBar: AppBar(
        title: const Text('Companion profile'),
        backgroundColor: Colors.transparent,
      ),
      body: DecoratedBox(
        decoration: BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topCenter,
            end: const Alignment(0, -0.15),
            colors: <Color>[
              Color.alphaBlend(
                accent.withValues(alpha: 0.13),
                theme.scaffoldBackgroundColor,
              ),
              theme.scaffoldBackgroundColor,
            ],
          ),
        ),
        child: SafeArea(
          top: false,
          child: LayoutBuilder(
            builder: (context, constraints) {
              final double portraitSize = math.min(
                236.0,
                math.max(164.0, constraints.maxWidth * 0.61),
              );

              return SingleChildScrollView(
                padding: const EdgeInsets.fromLTRB(
                  AppSpacing.lg,
                  AppSpacing.sm,
                  AppSpacing.lg,
                  AppSpacing.xxl,
                ),
                child: Center(
                  child: ConstrainedBox(
                    constraints: const BoxConstraints(maxWidth: 620),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        _ProfileHeader(
                          companion: companion,
                          portraitSize: portraitSize,
                        ),
                        const SizedBox(height: AppSpacing.xxl),
                        _DetailCard(
                          icon: Icons.auto_awesome_rounded,
                          title: 'Personality',
                          accent: accent,
                          child: Text(
                            companion.description,
                            style: theme.textTheme.bodyLarge?.copyWith(
                              color: colors.onSurfaceVariant,
                            ),
                          ),
                        ),
                        const SizedBox(height: AppSpacing.md),
                        _DetailCard(
                          icon: Icons.psychology_alt_outlined,
                          title: 'Memory',
                          accent: colors.secondary,
                          child: Text(
                            'Long-term memory is optional and off by default. '
                            'This local preview stores no conversation or '
                            'memory data.',
                            style: theme.textTheme.bodyMedium?.copyWith(
                              color: colors.onSurfaceVariant,
                            ),
                          ),
                        ),
                        const SizedBox(height: AppSpacing.md),
                        _DisclosureCard(companionName: companion.name),
                      ],
                    ),
                  ),
                ),
              );
            },
          ),
        ),
      ),
      bottomNavigationBar: Material(
        color: colors.surface,
        elevation: 12,
        shadowColor: colors.shadow.withValues(alpha: 0.3),
        child: SafeArea(
          minimum: const EdgeInsets.fromLTRB(
            AppSpacing.lg,
            AppSpacing.sm,
            AppSpacing.lg,
            AppSpacing.md,
          ),
          child: Center(
            heightFactor: 1,
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 620),
              child: SizedBox(
                width: double.infinity,
                child: Semantics(
                  button: true,
                  label: 'Start a local mock voice call with ${companion.name}',
                  onTap: onStartVoiceCall,
                  child: ExcludeSemantics(
                    child: FilledButton.icon(
                      key: const Key('start-voice-call'),
                      onPressed: onStartVoiceCall,
                      icon: const Icon(Icons.graphic_eq_rounded),
                      label: const Text('Start voice call'),
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _ProfileHeader extends StatelessWidget {
  const _ProfileHeader({required this.companion, required this.portraitSize});

  final Companion companion;
  final double portraitSize;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colors = theme.colorScheme;

    return Column(
      children: [
        Semantics(
          image: true,
          label: 'Portrait of ${companion.name}, a fictional AI companion',
          child: ExcludeSemantics(
            child: SizedBox(
              width: portraitSize,
              height: portraitSize + 18,
              child: Stack(
                alignment: Alignment.topCenter,
                children: [
                  Container(
                    width: portraitSize,
                    height: portraitSize,
                    padding: const EdgeInsets.all(4),
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      gradient: LinearGradient(
                        begin: Alignment.topLeft,
                        end: Alignment.bottomRight,
                        colors: [
                          companion.accent.withValues(alpha: 0.95),
                          colors.primary,
                        ],
                      ),
                      boxShadow: [
                        BoxShadow(
                          color: companion.accent.withValues(alpha: 0.22),
                          blurRadius: 30,
                          spreadRadius: 2,
                        ),
                      ],
                    ),
                    child: Hero(
                      tag: 'companion-portrait-${companion.id}',
                      child: ClipOval(
                        child: Image.asset(
                          companion.assetPath,
                          cacheWidth: 720,
                          fit: BoxFit.cover,
                          filterQuality: FilterQuality.high,
                          errorBuilder: (context, error, stackTrace) {
                            return ColoredBox(
                              color: colors.surfaceContainerHighest,
                              child: Icon(
                                Icons.auto_awesome_rounded,
                                size: portraitSize * 0.3,
                                color: colors.onSurfaceVariant,
                              ),
                            );
                          },
                        ),
                      ),
                    ),
                  ),
                  const Positioned(bottom: 0, child: _FictionalAiBadge()),
                ],
              ),
            ),
          ),
        ),
        const SizedBox(height: AppSpacing.lg),
        Semantics(
          header: true,
          child: Text(
            companion.name,
            textAlign: TextAlign.center,
            style: theme.textTheme.headlineLarge?.copyWith(
              fontWeight: FontWeight.w700,
              letterSpacing: -0.5,
            ),
          ),
        ),
        const SizedBox(height: AppSpacing.xs),
        Text(
          companion.personality,
          textAlign: TextAlign.center,
          style: theme.textTheme.titleMedium?.copyWith(
            color: colors.primary,
            fontWeight: FontWeight.w700,
          ),
        ),
        const SizedBox(height: AppSpacing.sm),
        Text(
          companion.tagline,
          textAlign: TextAlign.center,
          style: theme.textTheme.bodyLarge?.copyWith(
            color: colors.onSurfaceVariant,
            fontStyle: FontStyle.italic,
          ),
        ),
      ],
    );
  }
}

class _FictionalAiBadge extends StatelessWidget {
  const _FictionalAiBadge();

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colors = theme.colorScheme;

    return DecoratedBox(
      decoration: BoxDecoration(
        color: colors.primaryContainer,
        borderRadius: BorderRadius.circular(AppRadii.pill),
        border: Border.all(color: colors.primary.withValues(alpha: 0.45)),
        boxShadow: [
          BoxShadow(
            color: colors.shadow.withValues(alpha: 0.2),
            blurRadius: 10,
            offset: const Offset(0, 3),
          ),
        ],
      ),
      child: Padding(
        padding: const EdgeInsets.symmetric(
          horizontal: AppSpacing.md,
          vertical: AppSpacing.xs,
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              Icons.auto_awesome_rounded,
              size: 16,
              color: colors.onPrimaryContainer,
            ),
            const SizedBox(width: AppSpacing.xs),
            Flexible(
              child: Text(
                'FICTIONAL AI',
                style: theme.textTheme.labelLarge?.copyWith(
                  color: colors.onPrimaryContainer,
                  fontWeight: FontWeight.w800,
                  letterSpacing: 0.6,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _DetailCard extends StatelessWidget {
  const _DetailCard({
    required this.icon,
    required this.title,
    required this.accent,
    required this.child,
  });

  final IconData icon;
  final String title;
  final Color accent;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colors = theme.colorScheme;

    return Semantics(
      container: true,
      child: Container(
        padding: const EdgeInsets.all(AppSpacing.lg),
        decoration: BoxDecoration(
          color: colors.surfaceContainerLow,
          borderRadius: BorderRadius.circular(AppRadii.large),
          border: Border.all(color: colors.outlineVariant),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Container(
                  width: 40,
                  height: 40,
                  decoration: BoxDecoration(
                    color: Color.alphaBlend(
                      accent.withValues(alpha: 0.16),
                      colors.surfaceContainerHighest,
                    ),
                    borderRadius: BorderRadius.circular(AppRadii.small),
                  ),
                  child: Icon(icon, size: 21, color: colors.onSurface),
                ),
                const SizedBox(width: AppSpacing.sm),
                Expanded(
                  child: Semantics(
                    header: true,
                    child: Text(title, style: theme.textTheme.titleMedium),
                  ),
                ),
              ],
            ),
            const SizedBox(height: AppSpacing.md),
            child,
          ],
        ),
      ),
    );
  }
}

class _DisclosureCard extends StatelessWidget {
  const _DisclosureCard({required this.companionName});

  final String companionName;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colors = theme.colorScheme;

    return Semantics(
      container: true,
      label:
          'AI disclosure. $companionName is a fictional AI companion. '
          'It is not a person, therapist, or emergency service.',
      child: ExcludeSemantics(
        child: Container(
          padding: const EdgeInsets.all(AppSpacing.lg),
          decoration: BoxDecoration(
            color: colors.tertiaryContainer.withValues(alpha: 0.5),
            borderRadius: BorderRadius.circular(AppRadii.large),
            border: Border.all(color: colors.tertiary.withValues(alpha: 0.45)),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Wrap(
                children: [
                  Icon(
                    Icons.info_outline_rounded,
                    color: colors.onTertiaryContainer,
                  ),
                  const SizedBox(width: AppSpacing.sm),
                  Text(
                    'AI disclosure',
                    style: theme.textTheme.titleMedium?.copyWith(
                      color: colors.onTertiaryContainer,
                    ),
                  ),
                ],
              ),
              const SizedBox(height: AppSpacing.md),
              Text(
                '$companionName is a fictional AI companion. It is not a '
                'person, therapist, or emergency service.',
                style: theme.textTheme.bodyMedium?.copyWith(
                  color: colors.onTertiaryContainer,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
