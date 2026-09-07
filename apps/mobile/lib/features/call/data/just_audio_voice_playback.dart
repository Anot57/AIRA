import 'package:audio_session/audio_session.dart';
import 'package:just_audio/just_audio.dart';

import '../application/voice_io.dart';

/// Plays the generated WAV directly from the trusted configured backend.
final class JustAudioVoicePlayback implements VoicePlayback {
  JustAudioVoicePlayback({AudioPlayer? player})
    : _player = player ?? AudioPlayer(useProxyForRequestHeaders: false);

  final AudioPlayer _player;

  @override
  Future<void> play(
    Uri audioUri, {
    required void Function() onPlaybackStarted,
  }) async {
    final session = await AudioSession.instance;
    await session.configure(const AudioSessionConfiguration.speech());
    await _player.stop();
    await _player.setUrl(audioUri.toString());
    onPlaybackStarted();
    await _player.play();
  }

  @override
  Future<void> stop() => _player.stop();

  @override
  Future<void> dispose() => _player.dispose();
}
