import 'conversation_turn_response.dart';

/// One visible exchange kept only for the lifetime of the call screen.
final class ConversationSessionTurn {
  const ConversationSessionTurn({
    required this.turnId,
    required this.userTranscript,
    required this.assistantResponse,
    required this.aiDisclosure,
    required this.audioUri,
  });

  factory ConversationSessionTurn.fromResponse(
    ConversationTurnResponse response,
  ) {
    return ConversationSessionTurn(
      turnId: response.turnId,
      userTranscript: response.normalizedTranscript,
      assistantResponse: response.response,
      aiDisclosure: response.aiDisclosure,
      audioUri: response.audioUri,
    );
  }

  final String turnId;
  final String userTranscript;
  final String assistantResponse;
  final String aiDisclosure;
  final Uri audioUri;
}
