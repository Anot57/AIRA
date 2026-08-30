import 'package:flutter/material.dart';

import '../../domain/companion.dart';

/// A compact catalog card for one fictional AI companion.
class CompanionCard extends StatelessWidget {
  const CompanionCard({
    super.key,
    required this.companion,
    required this.onTap,
  });

  final Companion companion;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colors = theme.colorScheme;
    final accent = companion.accent;
    final cacheWidth = (112 * MediaQuery.devicePixelRatioOf(context)).ceil();
    final borderRadius = BorderRadius.circular(24);

    return Semantics(
      container: true,
      button: true,
      label:
          '${companion.name}, fictional AI companion. '
          '${companion.personality}. ${companion.tagline}',
      hint: 'Open companion details',
      onTap: onTap,
      child: Material(
        color: Color.alphaBlend(
          accent.withValues(alpha: 0.08),
          colors.surfaceContainerLow,
        ),
        shape: RoundedRectangleBorder(
          borderRadius: borderRadius,
          side: BorderSide(color: accent.withValues(alpha: 0.30)),
        ),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          excludeFromSemantics: true,
          onTap: onTap,
          borderRadius: borderRadius,
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                SizedBox(
                  height: 112,
                  child: Stack(
                    clipBehavior: Clip.none,
                    children: [
                      Align(
                        alignment: Alignment.topCenter,
                        child: DecoratedBox(
                          decoration: BoxDecoration(
                            shape: BoxShape.circle,
                            gradient: LinearGradient(
                              begin: Alignment.topLeft,
                              end: Alignment.bottomRight,
                              colors: [
                                accent,
                                colors.primary.withValues(alpha: 0.78),
                              ],
                            ),
                            boxShadow: [
                              BoxShadow(
                                color: accent.withValues(alpha: 0.22),
                                blurRadius: 18,
                                spreadRadius: 1,
                              ),
                            ],
                          ),
                          child: Padding(
                            padding: const EdgeInsets.all(3),
                            child: Hero(
                              tag: 'companion-portrait-${companion.id}',
                              child: ClipOval(
                                child: SizedBox.square(
                                  dimension: 106,
                                  child: Image.asset(
                                    companion.assetPath,
                                    fit: BoxFit.cover,
                                    alignment: Alignment.topCenter,
                                    cacheWidth: cacheWidth,
                                    filterQuality: FilterQuality.medium,
                                    semanticLabel:
                                        'Portrait of ${companion.name}, a '
                                        'fictional AI companion',
                                    errorBuilder: (context, error, stackTrace) {
                                      return ColoredBox(
                                        color: colors.surfaceContainerHighest,
                                        child: Icon(
                                          Icons.auto_awesome_rounded,
                                          color: colors.onSurfaceVariant,
                                          size: 36,
                                        ),
                                      );
                                    },
                                  ),
                                ),
                              ),
                            ),
                          ),
                        ),
                      ),
                      Positioned(
                        top: 4,
                        right: 0,
                        child: Semantics(
                          label: 'AI companion',
                          child: ExcludeSemantics(
                            child: Container(
                              padding: const EdgeInsets.symmetric(
                                horizontal: 9,
                                vertical: 5,
                              ),
                              decoration: BoxDecoration(
                                color: colors.inverseSurface,
                                borderRadius: BorderRadius.circular(999),
                                border: Border.all(
                                  color: colors.onInverseSurface.withValues(
                                    alpha: 0.20,
                                  ),
                                ),
                              ),
                              child: Text(
                                'AI',
                                style: theme.textTheme.labelSmall?.copyWith(
                                  color: colors.onInverseSurface,
                                  fontWeight: FontWeight.w900,
                                  letterSpacing: 0.8,
                                ),
                              ),
                            ),
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 12),
                Text(
                  companion.name,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: theme.textTheme.titleMedium?.copyWith(
                    fontWeight: FontWeight.w800,
                    letterSpacing: -0.2,
                  ),
                ),
                const SizedBox(height: 3),
                Text(
                  companion.personality,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: theme.textTheme.labelMedium?.copyWith(
                    color: colors.primary,
                    fontWeight: FontWeight.w700,
                    height: 1.25,
                  ),
                ),
                const SizedBox(height: 7),
                Expanded(
                  child: Text(
                    companion.tagline,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: theme.textTheme.bodySmall?.copyWith(
                      color: colors.onSurfaceVariant,
                      height: 1.35,
                    ),
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
