import 'package:flutter/widgets.dart';

/// Shared layout values that keep phone screens visually consistent.
abstract final class AppSpacing {
  static const double xxs = 4;
  static const double xs = 8;
  static const double sm = 12;
  static const double md = 16;
  static const double lg = 20;
  static const double xl = 24;
  static const double xxl = 32;
  static const double xxxl = 48;

  static const EdgeInsets pagePadding = EdgeInsets.symmetric(
    horizontal: lg,
    vertical: md,
  );
}

abstract final class AppRadii {
  static const double small = 12;
  static const double medium = 18;
  static const double large = 28;
  static const double pill = 999;
}

abstract final class AppMotion {
  static const Duration quick = Duration(milliseconds: 180);
  static const Duration standard = Duration(milliseconds: 320);
  static const Duration orbPulse = Duration(milliseconds: 2400);
}
