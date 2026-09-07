import 'package:flutter/material.dart';

import 'app/aira_app.dart';
import 'core/config/aira_api_config.dart';
import 'features/call/aanya_voice_call_screen.dart';

void main() {
  final apiConfig = AiraApiConfig();
  runApp(
    AiraApp(
      aanyaVoiceCallBuilder: ({required companion, required onEnd}) {
        return AanyaVoiceCallScreen.production(
          companion: companion,
          onEnd: onEnd,
          config: apiConfig,
        );
      },
    ),
  );
}
