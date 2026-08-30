import 'package:flutter/material.dart';

import 'app_colors.dart';
import 'app_tokens.dart';

/// Material 3 themes for the Android app.
abstract final class AppTheme {
  static ThemeData get light => _build(_lightColorScheme);

  static ThemeData get dark => _build(_darkColorScheme);

  static const ColorScheme _lightColorScheme = ColorScheme(
    brightness: Brightness.light,
    primary: AppColors.rosewood,
    onPrimary: Colors.white,
    primaryContainer: AppColors.blush,
    onPrimaryContainer: Color(0xFF2B1122),
    secondary: AppColors.sage,
    onSecondary: Colors.white,
    secondaryContainer: AppColors.mist,
    onSecondaryContainer: Color(0xFF122018),
    tertiary: AppColors.clay,
    onTertiary: Colors.white,
    tertiaryContainer: AppColors.sand,
    onTertiaryContainer: Color(0xFF2D160B),
    error: Color(0xFFBA1A1A),
    onError: Colors.white,
    errorContainer: Color(0xFFFFDAD6),
    onErrorContainer: Color(0xFF410002),
    surface: AppColors.warmSurface,
    onSurface: AppColors.warmInk,
    surfaceContainerHighest: Color(0xFFF0E5EA),
    onSurfaceVariant: Color(0xFF50464C),
    outline: Color(0xFF7F7379),
    outlineVariant: Color(0xFFD2C3CA),
    shadow: Color(0xFF000000),
    scrim: Color(0xFF000000),
    inverseSurface: Color(0xFF392F34),
    onInverseSurface: Color(0xFFFFECF3),
    inversePrimary: Color(0xFFE4B7CF),
    surfaceTint: AppColors.rosewood,
  );

  static const ColorScheme _darkColorScheme = ColorScheme(
    brightness: Brightness.dark,
    primary: Color(0xFFE4B7CF),
    onPrimary: Color(0xFF41263A),
    primaryContainer: Color(0xFF593D50),
    onPrimaryContainer: Color(0xFFFFD8EA),
    secondary: Color(0xFFB9CBBE),
    onSecondary: Color(0xFF24352B),
    secondaryContainer: Color(0xFF3A4C40),
    onSecondaryContainer: Color(0xFFD5E8D9),
    tertiary: Color(0xFFE7BDA7),
    onTertiary: Color(0xFF442A1C),
    tertiaryContainer: Color(0xFF5E4030),
    onTertiaryContainer: Color(0xFFFFDBCA),
    error: Color(0xFFFFB4AB),
    onError: Color(0xFF690005),
    errorContainer: Color(0xFF93000A),
    onErrorContainer: Color(0xFFFFDAD6),
    surface: AppColors.nightSurface,
    onSurface: AppColors.nightInk,
    surfaceContainerHighest: Color(0xFF3B3338),
    onSurfaceVariant: Color(0xFFD5C4CC),
    outline: Color(0xFF9D8D95),
    outlineVariant: Color(0xFF50464C),
    shadow: Color(0xFF000000),
    scrim: Color(0xFF000000),
    inverseSurface: Color(0xFFF1E8EC),
    onInverseSurface: Color(0xFF342B30),
    inversePrimary: AppColors.rosewood,
    surfaceTint: Color(0xFFE4B7CF),
  );

  static ThemeData _build(ColorScheme colorScheme) {
    final bool isDark = colorScheme.brightness == Brightness.dark;
    final ThemeData base = ThemeData(
      useMaterial3: true,
      brightness: colorScheme.brightness,
      colorScheme: colorScheme,
      scaffoldBackgroundColor: isDark ? AppColors.night : AppColors.warmWhite,
      visualDensity: VisualDensity.standard,
    );

    final TextTheme textTheme = base.textTheme.copyWith(
      displayLarge: base.textTheme.displayLarge?.copyWith(
        fontWeight: FontWeight.w600,
        height: 1.08,
        letterSpacing: -1.2,
      ),
      displayMedium: base.textTheme.displayMedium?.copyWith(
        fontWeight: FontWeight.w600,
        height: 1.1,
        letterSpacing: -0.8,
      ),
      headlineLarge: base.textTheme.headlineLarge?.copyWith(
        fontWeight: FontWeight.w600,
        height: 1.15,
        letterSpacing: -0.4,
      ),
      headlineMedium: base.textTheme.headlineMedium?.copyWith(
        fontWeight: FontWeight.w600,
        height: 1.18,
      ),
      titleLarge: base.textTheme.titleLarge?.copyWith(
        fontWeight: FontWeight.w600,
        height: 1.25,
      ),
      titleMedium: base.textTheme.titleMedium?.copyWith(
        fontWeight: FontWeight.w600,
        height: 1.3,
      ),
      bodyLarge: base.textTheme.bodyLarge?.copyWith(height: 1.5),
      bodyMedium: base.textTheme.bodyMedium?.copyWith(height: 1.45),
      labelLarge: base.textTheme.labelLarge?.copyWith(
        fontWeight: FontWeight.w600,
        letterSpacing: 0.1,
      ),
    );

    return base.copyWith(
      textTheme: textTheme,
      appBarTheme: AppBarTheme(
        centerTitle: false,
        elevation: 0,
        scrolledUnderElevation: 0,
        backgroundColor: Colors.transparent,
        foregroundColor: colorScheme.onSurface,
        titleTextStyle: textTheme.titleLarge?.copyWith(
          color: colorScheme.onSurface,
        ),
      ),
      cardTheme: CardThemeData(
        elevation: 0,
        margin: EdgeInsets.zero,
        color: isDark ? AppColors.nightRaised : colorScheme.surface,
        surfaceTintColor: Colors.transparent,
        shape: RoundedRectangleBorder(
          side: BorderSide(color: colorScheme.outlineVariant),
          borderRadius: BorderRadius.circular(AppRadii.large),
        ),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          minimumSize: const Size(64, 52),
          padding: const EdgeInsets.symmetric(
            horizontal: AppSpacing.xl,
            vertical: AppSpacing.md,
          ),
          shape: const StadiumBorder(),
          textStyle: textTheme.labelLarge,
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          minimumSize: const Size(64, 52),
          padding: const EdgeInsets.symmetric(
            horizontal: AppSpacing.xl,
            vertical: AppSpacing.md,
          ),
          side: BorderSide(color: colorScheme.outline),
          shape: const StadiumBorder(),
          textStyle: textTheme.labelLarge,
        ),
      ),
      textButtonTheme: TextButtonThemeData(
        style: TextButton.styleFrom(
          minimumSize: const Size(48, 48),
          padding: const EdgeInsets.symmetric(horizontal: AppSpacing.md),
          shape: const StadiumBorder(),
          textStyle: textTheme.labelLarge,
        ),
      ),
      iconButtonTheme: IconButtonThemeData(
        style: IconButton.styleFrom(minimumSize: const Size(48, 48)),
      ),
      navigationBarTheme: NavigationBarThemeData(
        height: 72,
        elevation: 0,
        backgroundColor: isDark
            ? AppColors.nightSurface
            : AppColors.warmSurface,
        indicatorColor: colorScheme.primaryContainer,
        labelTextStyle: WidgetStatePropertyAll<TextStyle?>(
          textTheme.labelMedium,
        ),
      ),
      switchTheme: SwitchThemeData(
        thumbIcon: WidgetStateProperty.resolveWith<Icon?>((states) {
          if (states.contains(WidgetState.selected)) {
            return const Icon(Icons.check, size: 16);
          }
          return null;
        }),
      ),
      dividerTheme: DividerThemeData(
        color: colorScheme.outlineVariant,
        space: 1,
      ),
      listTileTheme: ListTileThemeData(
        contentPadding: const EdgeInsets.symmetric(
          horizontal: AppSpacing.lg,
          vertical: AppSpacing.xs,
        ),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(AppRadii.medium),
        ),
        iconColor: colorScheme.primary,
      ),
      snackBarTheme: SnackBarThemeData(
        behavior: SnackBarBehavior.floating,
        backgroundColor: colorScheme.inverseSurface,
        contentTextStyle: textTheme.bodyMedium?.copyWith(
          color: colorScheme.onInverseSurface,
        ),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(AppRadii.medium),
        ),
      ),
    );
  }
}
