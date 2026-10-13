"""Production composition for existing realtime adapters and Pocket TTS."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
import os

from .approved_references import APPROVED_VOICE_REFERENCES_BY_ID
from .companion_personas import PERSONAS
from .conversation import (
    AANYA_SYSTEM_PERSONA,
    DEFAULT_LLAMA_CACHE_DIR,
    DEFAULT_LLM_MAX_TOKENS,
    DEFAULT_LLAMA_CLI_PATH,
    DEFAULT_RUNTIME_ROOT,
    configure_runtime_environment,
    generate_local_llm_response,
    get_companion_persona,
    resolve_cached_llm_model,
)
from .llama_server import (
    LlamaServerConfig,
    LlamaServerStreamingLanguageModel,
)
from .observability import debug_conversation_enabled
from .pocket_tts import PocketTtsWorkerConfig, PocketTtsWorkerSynthesizer
from .realtime_pipeline import RealtimeTurnProcessor, StreamingRealtimeTurnProcessor
from .realtime_protocol import PcmAudioFormat, ProtocolError
from .retrieval import (
    SEARCH_ROUTER_PROMPT,
    AsyncRetrievalLanguageModel,
    LanguageModelSearchRouter,
    RetrievalAugmentedLanguageModel,
    retrieval_provider_from_environment,
)
from .safety import CrisisEscalatingLanguageModel
from .story_details import story_details_from_environment
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
SEARCH_ROUTER_ENVIRONMENT = "AIRA_SEARCH_ROUTER"


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
) -> Callable[..., RealtimeTurnProcessor]:
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
    story_details = story_details_from_environment(environ)
    async_retrieval = _async_retrieval_enabled(environ)
    search_router_enabled = (
        source.get(SEARCH_ROUTER_ENVIRONMENT, "1").strip().casefold()
        not in {"0", "false", "no", "off"}
    )
    debug_conversation = debug_conversation_enabled(environ)

    def create_processor(companion_id: str = "aanya") -> RealtimeTurnProcessor:
        # A companion speaks only once a person has approved her voice.
        if companion_id not in APPROVED_VOICE_REFERENCES_BY_ID:
            name = PERSONAS[companion_id].name if companion_id in PERSONAS else "This companion"
            raise ProtocolError(
                "voice_not_ready",
                f"{name}'s voice isn't ready yet. Try another companion for now.",
                fatal=True,
            )
        persona = get_companion_persona(companion_id)
        companion_name = PERSONAS[companion_id].name
        if llama_server_config is not None:
            language_model = LlamaServerStreamingLanguageModel(
                llama_server_config,
                system_persona=persona,
                max_tokens=DEFAULT_LLM_MAX_TOKENS,
            )
        else:
            language_model = CompleteResponseStreamingLlmAdapter(
                partial(_generate_response, system_persona=persona)
            )

        # The model decides when a question needs the internet, so users do
        # not have to say "news" or "today" (llama-server only: it shares the
        # warm GPU model; llama-cli is too slow for an extra call per turn).
        router = (
            LanguageModelSearchRouter(
                LlamaServerStreamingLanguageModel(
                    llama_server_config,
                    system_persona=SEARCH_ROUTER_PROMPT,
                    max_tokens=24,
                )
            )
            if llama_server_config is not None and search_router_enabled
            else None
        )
        retrieval_language_model = (
            AsyncRetrievalLanguageModel(
                language_model,
                retrieval_provider,
                router=router,
                story_details=story_details,
                companion_name=companion_name,
            )
            if async_retrieval
            else RetrievalAugmentedLanguageModel(
                language_model, retrieval_provider
            )
        )

        return StreamingRealtimeTurnProcessor(
            BoundedBatchStreamingTranscriber(
                partial(_transcribe_pcm, hotwords=companion_name),
                admission=stt_admission,
            ),
            CrisisEscalatingLanguageModel(retrieval_language_model),
            PocketTtsWorkerSynthesizer(worker_config, voice=companion_id),
            debug_conversation=debug_conversation,
            companion_id=companion_id,
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
    audio: bytes, audio_format: PcmAudioFormat, *, hotwords: str = "Aanya"
) -> FinalTranscriptionResult:
    try:
        result = transcribe_pcm16_audio(
            audio,
            sample_rate_hz=audio_format.sample_rate_hz,
            channels=audio_format.channels,
            model_dir=DEFAULT_RUNTIME_ROOT / "models" / "faster-whisper",
            cpu_threads=DEFAULT_CPU_THREADS,
            include_diagnostics=True,
            hotwords=hotwords,
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


def _generate_response(
    transcript: str, *, system_persona: str = AANYA_SYSTEM_PERSONA
) -> str:
    runtime_environment = configure_runtime_environment(DEFAULT_RUNTIME_ROOT)
    model_path = resolve_cached_llm_model(
        DEFAULT_LLAMA_CACHE_DIR, runtime_root=DEFAULT_RUNTIME_ROOT
    )
    return generate_local_llm_response(
        transcript,
        system_persona=system_persona,
        llama_cli_path=DEFAULT_LLAMA_CLI_PATH,
        model_path=model_path,
        cache_dir=DEFAULT_LLAMA_CACHE_DIR,
        environ=runtime_environment,
    )
