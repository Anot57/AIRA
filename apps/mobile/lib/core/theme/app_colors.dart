import 'package:flutter/material.dart';

/// Warm, muted colors used by the app theme and custom visual elements.
///
/// Text and control colors are paired through [AppTheme]'s color schemes. The
/// accent constants are exposed for visuals such as the abstract voice orb.
abstract final class AppColors {
  static const Color rosewood = Color(0xFF725267);
  static const Color rose = Color(0xFFB77B9D);
  static const Color blush = Color(0xFFF4D7E5);
  static const Color sage = Color(0xFF53665A);
  static const Color mist = Color(0xFFDDE7DE);
  static const Color clay = Color(0xFF7B563F);
  static const Color sand = Color(0xFFF4DDCF);

  static const Color warmWhite = Color(0xFFFFF9F7);
  static const Color warmSurface = Color(0xFFFFFBFA);
  static const Color warmInk = Color(0xFF241C21);

  static const Color night = Color(0xFF171316);
  static const Color nightSurface = Color(0xFF1D191C);
  static const Color nightRaised = Color(0xFF272126);
  static const Color nightInk = Color(0xFFF1E8EC);

  static const List<Color> lightOrbGradient = <Color>[
    Color(0xFFE5AFCB),
    Color(0xFFB89BCB),
    Color(0xFF8FBEB0),
  ];

  static const List<Color> darkOrbGradient = <Color>[
    Color(0xFFD89ABC),
    Color(0xFF9C7DB5),
    Color(0xFF6A9F90),
  ];
}
