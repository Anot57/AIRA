import 'package:flutter/material.dart';

import '../theme/app_tokens.dart';

/// A shared padded surface that can optionally behave like a large tap target.
class AppSurfaceCard extends StatelessWidget {
  const AppSurfaceCard({
    super.key,
    required this.child,
    this.onTap,
    this.semanticLabel,
    this.padding = const EdgeInsets.all(AppSpacing.lg),
  });

  final Widget child;
  final VoidCallback? onTap;
  final String? semanticLabel;
  final EdgeInsetsGeometry padding;

  @override
  Widget build(BuildContext context) {
    Widget content = Padding(padding: padding, child: child);

    if (onTap != null) {
      content = InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(AppRadii.large),
        child: content,
      );
    }

    return Semantics(
      container: true,
      button: onTap != null,
      label: semanticLabel,
      child: Card(clipBehavior: Clip.antiAlias, child: content),
    );
  }
}
