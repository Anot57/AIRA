/// Validated response from one non-streaming local conversation turn.
final class ConversationTurnResponse {
  const ConversationTurnResponse({
    required this.aiDisclosure,
    required this.turnId,
    required this.companion,
    required this.rawTranscript,
    required this.normalizedTranscript,
    required this.response,
    required this.audioUrl,
    required this.audioUri,
  });

  factory ConversationTurnResponse.fromJson(
    Map<String, Object?> json, {
    required Uri Function(String audioUrl) resolveAudioUri,
  }) {
    final audioUrl = _requiredString(json, 'audio_url');
    final parsed = ConversationTurnResponse(
      aiDisclosure: _requiredString(json, 'ai_disclosure'),
      turnId: _requiredString(json, 'turn_id'),
      companion: _requiredString(json, 'companion'),
      rawTranscript: _requiredString(json, 'raw_transcript'),
      normalizedTranscript: _requiredString(json, 'normalized_transcript'),
      response: _requiredString(json, 'response'),
      audioUrl: audioUrl,
      audioUri: resolveAudioUri(audioUrl),
    );

    if (parsed.companion != 'aanya' ||
        !_safeTurnId.hasMatch(parsed.turnId) ||
        parsed.audioUri.path !=
            '/v1/conversation/turns/${parsed.turnId}/audio' ||
        !RegExp(
          r'\bai\b',
          caseSensitive: false,
        ).hasMatch(parsed.aiDisclosure)) {
      throw const FormatException('Malformed conversation response.');
    }
    return parsed;
  }

  final String aiDisclosure;
  final String turnId;
  final String companion;
  final String rawTranscript;
  final String normalizedTranscript;
  final String response;
  final String audioUrl;
  final Uri audioUri;

  static final RegExp _safeTurnId = RegExp(r'^[a-z0-9][a-z0-9_-]{0,119}$');

  static String _requiredString(Map<String, Object?> json, String key) {
    final value = json[key];
    if (value is! String || value.trim().isEmpty) {
      throw const FormatException('Malformed conversation response.');
    }
    return value.trim();
  }
}
