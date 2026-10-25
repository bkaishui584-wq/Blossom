"""Provider decorator that records call status without touching prompt text."""

import time

from model.base import ModelProvider, Usage


class LoggedProvider(ModelProvider):
    def __init__(self, inner, call_log=None, role="companion"):
        self.inner = inner
        self.call_log = call_log
        self.role = str(role or "companion")
        self.name = getattr(inner, "name", "model") if inner is not None else "model"
        self.label = getattr(inner, "label", self.name) if inner is not None else self.name

    @classmethod
    def wrap(cls, provider, call_log=None, role="companion"):
        if provider is None or call_log is None:
            return provider
        if isinstance(provider, cls):
            return provider
        return cls(provider, call_log=call_log, role=role)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def is_available(self):
        return bool(self.inner is not None and self.inner.is_available())

    def chat(self, messages, tools=None, temperature=0.7):
        started = time.monotonic()
        result = None
        try:
            result = self.inner.chat(messages, tools=tools, temperature=temperature)
            return result
        except Exception as exc:
            self._record(
                started,
                False,
                error_kind="exception",
                error=str(exc),
                model=getattr(self.inner, "model", ""),
            )
            raise
        finally:
            if result is not None:
                usage = getattr(result, "usage", None) or Usage()
                self._record(
                    started,
                    bool(getattr(result, "ok", False)),
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    error_kind=getattr(result, "error_kind", ""),
                    error=getattr(result, "error", ""),
                    model=getattr(result, "model", "") or getattr(self.inner, "model", ""),
                )

    def stream(self, messages, tools=None, temperature=0.7):
        started = time.monotonic()
        usage = Usage()
        finished = False
        cancelled = False
        error = ""
        try:
            for chunk in self.inner.stream(messages, tools=tools, temperature=temperature):
                measured = getattr(chunk, "usage", None)
                if measured is not None and (
                    getattr(measured, "prompt_tokens", 0)
                    or getattr(measured, "completion_tokens", 0)
                ):
                    usage = measured
                if getattr(chunk, "done", False):
                    finished = True
                yield chunk
        except GeneratorExit:
            cancelled = True
            raise
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            self._record(
                started,
                finished and not cancelled and not error,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                error_kind="cancelled" if cancelled else ("exception" if error else ""),
                error=error,
                streamed=True,
            )

    def _record(
        self,
        started,
        ok,
        prompt_tokens=0,
        completion_tokens=0,
        error_kind="",
        error="",
        model="",
        streamed=False,
    ):
        if self.call_log is None:
            return None
        try:
            return self.call_log.record(
                role=getattr(self.inner, "role", self.role) or self.role,
                provider=getattr(self.inner, "name", self.name),
                model=model or getattr(self.inner, "model", ""),
                ok=bool(ok),
                latency_ms=int((time.monotonic() - started) * 1000),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                error_kind=error_kind,
                error=error,
                streamed=streamed,
            )
        except Exception:
            return None
