"""Production composition for existing realtime adapters and Pocket TTS."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .conversation import (
    AANYA_SYSTEM_PERSONA,
    DEFAULT_LLAMA_CACHE_DIR,
    DEFAULT_LLM_MAX_TOKENS,
    DEFAULT_LLAMA_CLI_PATH,
    DEFAULT_RUNTIME_ROOT,
    configure_runtime_environment,
    generate_local_llm_response,
    resolve_cached_llm_model,
)
from .llama_server import (
    LlamaServerConfig,
    LlamaServerStreamingLanguageModel,
)
from .pocket_tts import PocketTtsWorkerConfig, PocketTtsWorkerSynthesizer
from .realtime_pipeline import RealtimeTurnProcessor, StreamingRealtimeTurnProcessor
from .realtime_protocol import PcmAudioFormat
from .streaming import (
    BoundedBatchStreamingTranscriber,
    CompleteResponseStreamingLlmAdapter,
    NoSpeechDetectedError,
)
from .transcription import (
    DEFAULT_CPU_THREADS,
    NoSpeechError,
    transcribe_pcm16_audio,
)



REALTIME_LLM_PROVIDER_ENVIRONMENT = "AIRA_REALTIME_LLM_PROVIDER"


def _realtime_llm_provider(
    environ: Mapping[str, str] | None = None,
) -> str:
    import os

    source = os.environ if environ is None else environ
    provider = source.get(
        REALTIME_LLM_PROVIDER_ENVIRONMENT,
        "llama_cli",
    ).strip().casefold()

    if provider not in {"llama_cli", "llama_server"}:
        raise ValueError(
            "AIRA_REALTIME_LLM_PROVIDER must be "
            "'llama_cli' or 'llama_server'."
        )

    return provider

def build_pocket_realtime_processor_factory(
    environ: Mapping[str, str] | None = None,
) -> Callable[[], RealtimeTurnProcessor]:
    """Return a cheap per-session factory; the persistent worker owns the model."""

    worker_config = PocketTtsWorkerConfig.from_environment(environ)
    llm_provider = _realtime_llm_provider(environ)
    llama_server_config = (
        LlamaServerConfig.from_environment(environ)
        if llm_provider == "llama_server"
        else None
    )

    def create_processor() -> RealtimeTurnProcessor:
        if llama_server_config is not None:
            language_model = LlamaServerStreamingLanguageModel(
                llama_server_config,
                system_persona=AANYA_SYSTEM_PERSONA,
                max_tokens=DEFAULT_LLM_MAX_TOKENS,
            )
        else:
            language_model = CompleteResponseStreamingLlmAdapter(
                _generate_response
            )

        return StreamingRealtimeTurnProcessor(
            BoundedBatchStreamingTranscriber(_transcribe_pcm),
            language_model,
            PocketTtsWorkerSynthesizer(worker_config),
        )

    return create_processor


def build_pocket_tts_readiness_check(
    environ: Mapping[str, str] | None = None,
) -> Callable[[], None]:
    config = PocketTtsWorkerConfig.from_environment(environ)
    llm_provider = _realtime_llm_provider(environ)
    llama_server = (
        LlamaServerStreamingLanguageModel(
            LlamaServerConfig.from_environment(environ),
            system_persona=AANYA_SYSTEM_PERSONA,
            max_tokens=DEFAULT_LLM_MAX_TOKENS,
        )
        if llm_provider == "llama_server"
        else None
    )

    def check() -> None:
        PocketTtsWorkerSynthesizer(config).check_ready()
        if llama_server is not None:
            llama_server.check_ready()

    return check


def _transcribe_pcm(audio: bytes, audio_format: PcmAudioFormat) -> str:
    try:
        return transcribe_pcm16_audio(
            audio,
            sample_rate_hz=audio_format.sample_rate_hz,
            channels=audio_format.channels,
            model_dir=DEFAULT_RUNTIME_ROOT / "models" / "faster-whisper",
            cpu_threads=DEFAULT_CPU_THREADS,
        )
    except NoSpeechError as error:
        raise NoSpeechDetectedError(str(error)) from error


def _generate_response(transcript: str) -> str:
    runtime_environment = configure_runtime_environment(DEFAULT_RUNTIME_ROOT)
    model_path = resolve_cached_llm_model(
        DEFAULT_LLAMA_CACHE_DIR, runtime_root=DEFAULT_RUNTIME_ROOT
    )
    return generate_local_llm_response(
        transcript,
        system_persona=AANYA_SYSTEM_PERSONA,
        llama_cli_path=DEFAULT_LLAMA_CLI_PATH,
        model_path=model_path,
        cache_dir=DEFAULT_LLAMA_CACHE_DIR,
        environ=runtime_environment,
    )
