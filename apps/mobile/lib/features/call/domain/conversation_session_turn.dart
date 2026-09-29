import 'conversation_turn_response.dart';

/// One visible exchange kept only for the lifetime of the call screen.
final class ConversationSessionTurn {
  const ConversationSessionTurn({
    required this.turnId,
    required this.userTranscript,
    required this.assistantResponse,
    required this.aiDisclosure,
    required this.audioUri,
    required this.userCreatedAt,
    required this.assistantCreatedAt,
  });

  factory ConversationSessionTurn.fromResponse(
    ConversationTurnResponse response, {
    required DateTime userCreatedAt,
    required DateTime assistantCreatedAt,
  }) {
    return ConversationSessionTurn(
      turnId: response.turnId,
      userTranscript: response.normalizedTranscript,
      assistantResponse: response.response,
      aiDisclosure: response.aiDisclosure,
      audioUri: response.audioUri,
      userCreatedAt: userCreatedAt,
      assistantCreatedAt: assistantCreatedAt,
    );
  }

  final String turnId;
  final String userTranscript;
  final String assistantResponse;
  final String aiDisclosure;
  final Uri audioUri;
  final DateTime userCreatedAt;
  final DateTime assistantCreatedAt;
}
