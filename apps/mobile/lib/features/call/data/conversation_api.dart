import '../domain/conversation_turn_response.dart';

abstract interface class ConversationApi {
  Future<bool> checkHealth();

  Future<ConversationTurnResponse> createAanyaTurn(String audioPath);

  void dispose();
}

enum ConversationApiErrorKind {
  invalidRequest,
  serviceBusy,
  unavailable,
  timeout,
  processingFailure,
  unexpectedResponse,
}

/// A deliberately detail-free error safe to map directly to user-facing copy.
final class ConversationApiException implements Exception {
  const ConversationApiException(this.kind);

  final ConversationApiErrorKind kind;

  String get message => switch (kind) {
    ConversationApiErrorKind.invalidRequest => 'That recording could not be sent. Please record a new message and try again.',
    ConversationApiErrorKind.serviceBusy =>
      'Aanya is finishing another response. Try again in a moment.',
    ConversationApiErrorKind.unavailable =>
      "Aira can't reach the local AI service. Make sure your laptop and phone "
          'are on the same Wi-Fi and the local Aira server is running.',
    ConversationApiErrorKind.timeout =>
      'Aanya took too long to respond. Please try a new recording.',
    ConversationApiErrorKind.processingFailure =>
      'The local AI could not process that recording. Please try again.',
    ConversationApiErrorKind.unexpectedResponse =>
      'Aira received an unexpected response from the local AI service.',
  };

  @override
  String toString() => 'ConversationApiException($kind)';
}
