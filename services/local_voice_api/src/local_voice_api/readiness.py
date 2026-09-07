"""Observable, single-flight warmup and readiness state for local runtimes."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

_LOGGER = logging.getLogger(__name__)


def _safe_readiness_message(message: object) -> str:
    if not isinstance(message, str):
        return "The local model runtime is degraded."
    rendered = " ".join(message.split()).strip()
    if not rendered or "/" in rendered or "\\" in rendered:
        return "The local model runtime is degraded."
    return rendered[:160]


class ReadinessStatus(StrEnum):
    STARTING = "starting"
    WARMING = "warming"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


class ComponentStatus(StrEnum):
    NOT_LOADED = "not_loaded"
    LOADING = "loading"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class WarmupStep:
    """One blocking model/runtime preparation call executed off the event loop."""

    component: str
    callback: Callable[[], Any]


class ReadinessRegistry:
    """Thread-safe readiness snapshot containing only client-safe state."""

    def __init__(self, components: Iterable[str]) -> None:
        names = tuple(components)
        if not names or any(not isinstance(name, str) or not name for name in names):
            raise ValueError("Readiness requires non-empty component names.")
        if len(set(names)) != len(names):
            raise ValueError("Readiness component names must be unique.")
        self._lock = threading.RLock()
        self._status = ReadinessStatus.STARTING
        self._components = {
            name: ComponentStatus.NOT_LOADED for name in names
        }
        self._message = "Optional model warmup has not started."

    @property
    def status(self) -> ReadinessStatus:
        with self._lock:
            return self._status

    def begin_warmup(self) -> None:
        with self._lock:
            self._status = ReadinessStatus.WARMING
            self._message = "Local model runtimes are warming."

    def set_component(self, name: str, status: ComponentStatus) -> None:
        with self._lock:
            if name not in self._components:
                raise KeyError(f"Unknown readiness component: {name}")
            self._components[name] = status

    def finish_ready(self) -> None:
        with self._lock:
            if any(
                status is not ComponentStatus.READY
                for status in self._components.values()
            ):
                raise RuntimeError("All components must be ready before completion.")
            self._status = ReadinessStatus.READY
            self._message = "The warmed local conversation runtime is ready."

    def finish_failed(self, component: str) -> None:
        with self._lock:
            if component not in self._components:
                raise KeyError(f"Unknown readiness component: {component}")
            self._components[component] = ComponentStatus.FAILED
            self._status = ReadinessStatus.FAILED
            self._message = f"Warmup failed for component: {component}."

    def mark_degraded(self, message: str) -> None:
        with self._lock:
            self._status = ReadinessStatus.DEGRADED
            self._message = _safe_readiness_message(message)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "status": self._status.value,
                "components": {
                    name: status.value
                    for name, status in self._components.items()
                },
                "message": self._message,
            }


class WarmupCoordinator:
    """Run expensive warmup once without blocking liveness or the event loop."""

    def __init__(
        self,
        registry: ReadinessRegistry,
        steps: Iterable[WarmupStep],
    ) -> None:
        self.registry = registry
        self._steps = tuple(steps)
        if not self._steps:
            raise ValueError("Warmup requires at least one step.")
        self._start_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    async def start(self) -> asyncio.Task[None]:
        """Schedule the one warmup task and return it immediately."""

        async with self._start_lock:
            if self._closed:
                raise RuntimeError("Warmup coordinator is closed.")
            if self._task is None:
                self._task = asyncio.create_task(
                    self._run(), name="aira-local-runtime-warmup"
                )
            return self._task

    async def wait(self) -> None:
        task = await self.start()
        await task

    async def _run(self) -> None:
        self.registry.begin_warmup()
        for step in self._steps:
            self.registry.set_component(step.component, ComponentStatus.LOADING)
            _LOGGER.info("Starting local warmup component=%s", step.component)
            try:
                await asyncio.to_thread(step.callback)
            except asyncio.CancelledError:
                self.registry.mark_degraded("Model warmup was cancelled during shutdown.")
                raise
            except Exception:
                self.registry.finish_failed(step.component)
                _LOGGER.exception(
                    "Local model warmup failed component=%s", step.component
                )
                return
            self.registry.set_component(step.component, ComponentStatus.READY)
            _LOGGER.info("Completed local warmup component=%s", step.component)
        self.registry.finish_ready()

    async def close(self) -> None:
        async with self._start_lock:
            self._closed = True
            task = self._task
        if task is not None and not task.done():
            # asyncio.to_thread cannot stop a model load already executing.
            # Await it so shutdown never races a still-mutating CUDA/model cache.
            await asyncio.shield(task)


def component_is_ready(snapshot: Mapping[str, object], component: str) -> bool:
    components = snapshot.get("components")
    return isinstance(components, Mapping) and components.get(component) == "ready"
