import 'package:flutter/material.dart';

import '../constants/app_copy.dart';
import '../theme/app_tokens.dart';

/// A consistent, accessible identity marker for AI-facing screens.
class AiIdentityBadge extends StatelessWidget {
  const AiIdentityBadge({super.key, this.compact = false});

  final bool compact;

  @override
  Widget build(BuildContext context) {
    final ColorScheme colors = Theme.of(context).colorScheme;

    return Semantics(
      container: true,
      label: '${AppCopy.productName}, ${AppCopy.aiCompanionLabel}',
      child: ExcludeSemantics(
        child: DecoratedBox(
          decoration: BoxDecoration(
            color: colors.primaryContainer,
            borderRadius: BorderRadius.circular(AppRadii.pill),
          ),
          child: Padding(
            padding: EdgeInsets.symmetric(
              horizontal: compact ? AppSpacing.sm : AppSpacing.md,
              vertical: compact ? AppSpacing.xs : AppSpacing.sm,
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(
                  Icons.auto_awesome_rounded,
                  size: compact ? 16 : 18,
                  color: colors.onPrimaryContainer,
                ),
                const SizedBox(width: AppSpacing.xs),
                Text(
                  AppCopy.aiCompanionLabel,
                  style: Theme.of(context).textTheme.labelLarge
                      ?.copyWith(color: colors.onPrimaryContainer),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
