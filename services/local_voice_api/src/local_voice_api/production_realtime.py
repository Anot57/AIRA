"""Production composition for existing realtime adapters and Pocket TTS."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import os

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
from .observability import debug_conversation_enabled
from .pocket_tts import PocketTtsWorkerConfig, PocketTtsWorkerSynthesizer
from .realtime_pipeline import RealtimeTurnProcessor, StreamingRealtimeTurnProcessor
from .realtime_protocol import PcmAudioFormat
from .retrieval import (
    AsyncRetrievalLanguageModel,
    RetrievalAugmentedLanguageModel,
    retrieval_provider_from_environment,
)
from .safety import CrisisEscalatingLanguageModel
from .streaming import (
    BoundedBatchStreamingTranscriber,
    CompleteResponseStreamingLlmAdapter,
    FinalTranscriptionResult,
    NoSpeechDetectedError,
    SttAdmissionController,
)
from .transcription import (
    DEFAULT_CPU_THREADS,
    NoSpeechError,
    Pcm16TranscriptionResult,
    transcribe_pcm16_audio,
)



REALTIME_LLM_PROVIDER_ENVIRONMENT = "AIRA_REALTIME_LLM_PROVIDER"
REALTIME_STT_CONCURRENCY_ENVIRONMENT = "AIRA_REALTIME_STT_CONCURRENCY"
ASYNC_RETRIEVAL_ENVIRONMENT = "AIRA_ASYNC_RETRIEVAL"


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


def _async_retrieval_enabled(
    environ: Mapping[str, str] | None = None,
) -> bool:
    source = os.environ if environ is None else environ
    return source.get(ASYNC_RETRIEVAL_ENVIRONMENT, "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def build_pocket_realtime_processor_factory(
    environ: Mapping[str, str] | None = None,
) -> Callable[[], RealtimeTurnProcessor]:
    """Return a cheap per-session factory; the persistent worker owns the model."""

    worker_config = PocketTtsWorkerConfig.from_environment(environ)
    source = os.environ if environ is None else environ
    raw_stt_concurrency = source.get(REALTIME_STT_CONCURRENCY_ENVIRONMENT, "1")
    try:
        stt_concurrency = int(raw_stt_concurrency)
    except ValueError as error:
        raise ValueError("AIRA_REALTIME_STT_CONCURRENCY must be an integer.") from error
    stt_admission = SttAdmissionController(stt_concurrency)
    llm_provider = _realtime_llm_provider(environ)
    llama_server_config = (
        LlamaServerConfig.from_environment(environ)
        if llm_provider == "llama_server"
        else None
    )
    retrieval_provider = retrieval_provider_from_environment(environ)
    async_retrieval = _async_retrieval_enabled(environ)
    debug_conversation = debug_conversation_enabled(environ)

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

        retrieval_language_model = (
            AsyncRetrievalLanguageModel(language_model, retrieval_provider)
            if async_retrieval
            else RetrievalAugmentedLanguageModel(
                language_model, retrieval_provider
            )
        )

        return StreamingRealtimeTurnProcessor(
            BoundedBatchStreamingTranscriber(
                _transcribe_pcm,
                admission=stt_admission,
            ),
            CrisisEscalatingLanguageModel(retrieval_language_model),
            PocketTtsWorkerSynthesizer(worker_config),
            debug_conversation=debug_conversation,
        )

    create_processor.capacity_snapshot = lambda: {  # type: ignore[attr-defined]
        "stt": stt_admission.snapshot()
    }
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

    pocket = PocketTtsWorkerSynthesizer(config)

    def check() -> None:
        pocket.check_ready()
        if llama_server is not None:
            llama_server.check_ready()

    def warmup_tts() -> None:
        pocket.check_ready()
        pocket.warmup_inference()

    check.tts_warmup = warmup_tts  # type: ignore[attr-defined]
    if llama_server is not None:
        check.llm_warmup = llama_server.warmup_inference  # type: ignore[attr-defined]

    check.capacity_snapshot = lambda: {  # type: ignore[attr-defined]
        "pocket_tts": {
            key: value
            for key, value in pocket.worker_snapshot().items()
            if key
            in {
                "busy",
                "queue_depth",
                "queue_capacity",
                "oldest_queue_wait_ms",
            }
        }
    }
    return check


def _transcribe_pcm(
    audio: bytes, audio_format: PcmAudioFormat
) -> FinalTranscriptionResult:
    try:
        result = transcribe_pcm16_audio(
            audio,
            sample_rate_hz=audio_format.sample_rate_hz,
            channels=audio_format.channels,
            model_dir=DEFAULT_RUNTIME_ROOT / "models" / "faster-whisper",
            cpu_threads=DEFAULT_CPU_THREADS,
            include_diagnostics=True,
        )
        if not isinstance(result, Pcm16TranscriptionResult):
            raise RuntimeError("Realtime STT diagnostics were unavailable.")
        return FinalTranscriptionResult(
            text=result.text,
            segment_count=result.segment_count,
            stt_no_speech_probability=result.stt_no_speech_probability,
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
