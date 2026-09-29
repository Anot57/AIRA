import 'dart:math' as math;
import 'dart:typed_data';

const int defaultUserSilenceEndpointMs = int.fromEnvironment(
  'AIRA_USER_SILENCE_ENDPOINT_MS',
  defaultValue: 3000,
);

enum PcmVoiceActivityState {
  waitingForSpeech,
  speechCandidate,
  speechActive,
  possibleEndpoint,
  finalized,
}

final class PcmVoiceActivityConfig {
  const PcmVoiceActivityConfig({
    this.silenceEndpoint = const Duration(
      milliseconds: defaultUserSilenceEndpointMs,
    ),
    this.speechOnRms = 650,
    this.speechOffRms = 400,
    this.minimumSpeechFrames = 3,
    this.minimumSpeechDuration = const Duration(milliseconds: 80),
    this.maximumStationaryCandidateDuration = const Duration(
      milliseconds: 280,
    ),
    this.minimumOnsetModulation = 0.10,
    this.noiseMultiplier = 2.2,
    this.continuationNoiseMultiplier = 1.35,
    this.maximumAdaptiveSpeechOnRms = 5000,
  });

  final Duration silenceEndpoint;

  /// PCM16 RMS needed to begin a speech candidate.
  final double speechOnRms;

  /// Lower threshold used after onset, providing energy hysteresis.
  final double speechOffRms;
  final int minimumSpeechFrames;
  final Duration minimumSpeechDuration;
  final Duration maximumStationaryCandidateDuration;
  final double minimumOnsetModulation;
  final double noiseMultiplier;
  final double continuationNoiseMultiplier;
  final double maximumAdaptiveSpeechOnRms;

  void validate() {
    if (silenceEndpoint <= Duration.zero ||
        speechOnRms <= 0 ||
        speechOffRms <= 0 ||
        speechOffRms >= speechOnRms ||
        minimumSpeechFrames <= 0 ||
        minimumSpeechDuration <= Duration.zero ||
        maximumStationaryCandidateDuration < minimumSpeechDuration ||
        minimumOnsetModulation <= 0 ||
        noiseMultiplier <= 1 ||
        continuationNoiseMultiplier <= 1 ||
        continuationNoiseMultiplier >= noiseMultiplier ||
        maximumAdaptiveSpeechOnRms < speechOnRms) {
      throw ArgumentError('Voice endpoint thresholds are invalid.');
    }
  }
}

final class PcmVoiceActivityObservation {
  const PcmVoiceActivityObservation({
    required this.state,
    required this.rms,
    required this.speechStarted,
    required this.endpointReached,
    required this.speechOnThreshold,
    this.speechOnset,
    this.lastSpeech,
  });

  final PcmVoiceActivityState state;
  final double rms;
  final bool speechStarted;
  final bool endpointReached;
  final double speechOnThreshold;
  final Duration? speechOnset;
  final Duration? lastSpeech;
}

/// Deterministic PCM16 energy endpoint detector.
///
/// It never starts a silence endpoint before confirmed speech onset. A lower
/// speech-off threshold, consecutive onset frames, and an adaptive noise floor
/// provide hysteresis against room noise. Timestamps must come from a monotonic
/// clock; the controller uses [Stopwatch].
final class PcmVoiceActivityEndpointDetector {
  PcmVoiceActivityEndpointDetector({
    this.config = const PcmVoiceActivityConfig(),
  }) {
    config.validate();
  }

  final PcmVoiceActivityConfig config;

  PcmVoiceActivityState _state = PcmVoiceActivityState.waitingForSpeech;
  PcmVoiceActivityState get state => _state;

  Duration? _lastTimestamp;
  Duration? _speechOnset;
  Duration? _lastSpeech;
  var _candidateFrames = 0;
  Duration? _candidateStartedAt;
  final List<double> _candidateRms = <double>[];
  var _noiseFloorRms = 120.0;
  var _endpointEmitted = false;

  bool get hasSpeech => _speechOnset != null;
  Duration? get speechOnset => _speechOnset;
  Duration? get lastSpeech => _lastSpeech;

  PcmVoiceActivityObservation observe(
    Uint8List pcm16,
    Duration monotonicTimestamp,
  ) {
    if (pcm16.isEmpty || pcm16.length.isOdd) {
      throw const FormatException('VAD requires non-empty, even PCM16 bytes.');
    }
    final previousTimestamp = _lastTimestamp;
    if (previousTimestamp != null && monotonicTimestamp < previousTimestamp) {
      throw ArgumentError.value(
        monotonicTimestamp,
        'monotonicTimestamp',
        'Voice timestamps cannot move backwards.',
      );
    }
    _lastTimestamp = monotonicTimestamp;

    final rms = pcm16Rms(pcm16);
    final adaptiveOn = math.min(
      config.maximumAdaptiveSpeechOnRms,
      math.max(config.speechOnRms, _noiseFloorRms * config.noiseMultiplier),
    );
    final adaptiveOff = math.max(
      config.speechOffRms,
      math.min(adaptiveOn, _noiseFloorRms * config.continuationNoiseMultiplier),
    );
    var speechStarted = false;
    var endpointReached = false;

    switch (_state) {
      case PcmVoiceActivityState.waitingForSpeech:
      case PcmVoiceActivityState.speechCandidate:
        if (rms >= adaptiveOn) {
          _candidateStartedAt ??= monotonicTimestamp;
          _candidateFrames += 1;
          _candidateRms.add(rms);
          _state = PcmVoiceActivityState.speechCandidate;
          final candidateDuration = monotonicTimestamp - _candidateStartedAt!;
          if (_candidateFrames >= config.minimumSpeechFrames &&
              candidateDuration >= config.minimumSpeechDuration &&
              _candidateHasSpeechModulation()) {
            _speechOnset = _candidateStartedAt;
            _lastSpeech = monotonicTimestamp;
            _state = PcmVoiceActivityState.speechActive;
            speechStarted = true;
          } else if (candidateDuration >=
              config.maximumStationaryCandidateDuration) {
            // A steady fan or electrical hum can sit above the initial floor.
            // Fold a stationary candidate into the ambient estimate instead
            // of allowing it to become a conversation turn.
            _learnNoise(
              _candidateRms.reduce((a, b) => a + b) / _candidateRms.length,
              weight: 0.35,
            );
            _clearCandidate();
            _state = PcmVoiceActivityState.waitingForSpeech;
          }
        } else {
          _clearCandidate();
          _state = PcmVoiceActivityState.waitingForSpeech;
          _learnNoise(rms);
        }
      case PcmVoiceActivityState.speechActive:
        if (rms >= adaptiveOff) {
          _lastSpeech = monotonicTimestamp;
        } else {
          _state = PcmVoiceActivityState.possibleEndpoint;
          final lastSpeech = _lastSpeech;
          if (lastSpeech != null &&
              monotonicTimestamp - lastSpeech >= config.silenceEndpoint) {
            _state = PcmVoiceActivityState.finalized;
            if (!_endpointEmitted) {
              _endpointEmitted = true;
              endpointReached = true;
            }
          }
        }
      case PcmVoiceActivityState.possibleEndpoint:
        // Once speech has already started, use the lower continuation
        // threshold to detect resumed/continued speech. Requiring adaptiveOn
        // here can lose quiet words after a natural pause.
        if (rms >= adaptiveOff) {
          _lastSpeech = monotonicTimestamp;
          _state = PcmVoiceActivityState.speechActive;
        } else {
          final lastSpeech = _lastSpeech;
          if (lastSpeech != null &&
              monotonicTimestamp - lastSpeech >= config.silenceEndpoint) {
            _state = PcmVoiceActivityState.finalized;
            if (!_endpointEmitted) {
              _endpointEmitted = true;
              endpointReached = true;
            }
          }
        }
      case PcmVoiceActivityState.finalized:
        break;
    }

    return PcmVoiceActivityObservation(
      state: _state,
      rms: rms,
      speechStarted: speechStarted,
      endpointReached: endpointReached,
      speechOnThreshold: adaptiveOn,
      speechOnset: _speechOnset,
      lastSpeech: _lastSpeech,
    );
  }

  bool _candidateHasSpeechModulation() {
    if (_candidateRms.isEmpty) return false;
    final lowest = _candidateRms.reduce(math.min);
    final highest = _candidateRms.reduce(math.max);
    final mean = _candidateRms.reduce((a, b) => a + b) / _candidateRms.length;
    return mean > 0 && (highest - lowest) / mean >= config.minimumOnsetModulation;
  }

  void _clearCandidate() {
    _candidateFrames = 0;
    _candidateStartedAt = null;
    _candidateRms.clear();
  }

  void _learnNoise(double rms, {double weight = 0.06}) {
    // Slow enough not to chase a sudden voice transient, fast enough to adapt
    // to a stable fan/room floor before speech begins.
    _noiseFloorRms = (_noiseFloorRms * (1 - weight)) + (rms * weight);
  }

  void reset() {
    _state = PcmVoiceActivityState.waitingForSpeech;
    _lastTimestamp = null;
    _speechOnset = null;
    _lastSpeech = null;
    _clearCandidate();
    _endpointEmitted = false;
    _noiseFloorRms = 120;
  }
}

double pcm16Rms(Uint8List bytes) {
  if (bytes.isEmpty || bytes.length.isOdd) {
    throw const FormatException('RMS requires non-empty, even PCM16 bytes.');
  }
  final data = ByteData.sublistView(bytes);
  var sumSquares = 0.0;
  final sampleCount = bytes.length ~/ 2;
  for (var offset = 0; offset < bytes.length; offset += 2) {
    final sample = data.getInt16(offset, Endian.little).toDouble();
    sumSquares += sample * sample;
  }
  return math.sqrt(sumSquares / sampleCount);
}
