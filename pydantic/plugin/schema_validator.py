"""Pluggable schema validator for pydantic."""
from __future__ import annotations

import contextlib
import functools
from typing import Any, Callable, ClassVar, Iterator, TypeVar

from pydantic_core import CoreConfig, CoreSchema, SchemaValidator, ValidationError
from typing_extensions import Final, Literal, ParamSpec

from .plugin import Plugin, Step

P = ParamSpec('P')
R = TypeVar('R')


def create_schema_validator(
    schema: CoreSchema, config: CoreConfig | None = None, plugin_settings: dict[str, Any] | None = None
) -> SchemaValidator:
    """Get the schema validator class.

    Returns:
        If plugins are installed then return `PluggableSchemaValidator`, otherwise return `SchemaValidator`.
    """
    from ._loader import plugins

    if plugins:
        return PluggableSchemaValidator(schema, config, plugins, plugin_settings)  # type: ignore
    return SchemaValidator(schema, config)


class _Plug:
    """Pluggable schema validator."""

    EVENTS: Final = {'validate_json': 'on_validate_json', 'validate_python': 'on_validate_python'}
    """Events for plugins."""

    _in_call: ClassVar[set[str]] = set()

    def __init__(
        self,
        schema: CoreSchema,
        config: CoreConfig | None = None,
        plugins: set[Plugin] | None = None,
        plugin_settings: dict[str, Any] | None = None,
    ) -> None:
        self.schema = schema
        self.config = config
        self.plugins: set[Plugin] = plugins if plugins is not None else set()
        self.plugin_settings = plugin_settings

    def __call__(self, func: Callable[P, R]) -> Callable[P, R]:
        """Call plugins for pydantic."""
        enter = self.prepare_enter(func)
        on_success = self.prepare_on_success(func)
        on_error = self.prepare_on_error(func)

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            enter(*args, **kwargs)
            try:
                result = func(*args, **kwargs)
            except ValidationError as error:
                on_error(error)
                raise
            else:
                on_success(result)
                return result

        return wrapper

    def prepare_enter(self, func: Callable[..., Any]) -> Callable[..., None]:
        enter_calls = self.gather_calls(func, callback_type='enter')
        return self.run_callbacks(enter_calls)

    def prepare_on_success(self, func: Callable[..., Any]) -> Callable[[Any], None]:
        success_calls = self.gather_calls(func, callback_type='on_success')
        return self.run_callbacks(success_calls)

    def prepare_on_error(self, func: Callable[..., Any]) -> Callable[[ValidationError], None]:
        error_calls = self.gather_calls(func, callback_type='on_error')
        return self.run_callbacks(error_calls)

    def run_callbacks(self, callbacks: list[Callable[..., None]]) -> Callable[..., None]:
        def wrapper(*args: Any, **kwargs: Any) -> None:
            for callback in callbacks:
                with contextlib.suppress(NotImplementedError), self.run_once(callback) as callback_once:
                    if callback_once is None:
                        continue
                    callback_once(*args, **kwargs)

        return wrapper

    def gather_calls(
        self, func: Callable[..., Any], callback_type: Literal['enter', 'on_success', 'on_error']
    ) -> list[Callable[..., None]]:
        try:
            step = self.EVENTS[func.__name__]
        except KeyError as exc:
            raise RuntimeError(f'Unknown event for {func.__name__}') from exc

        calls: list[Callable[..., None]] = []
        for plugin in self.plugins:
            with contextlib.suppress(AttributeError, TypeError):
                step_type: type[Step] = getattr(plugin, step)
                on_step = step_type(self.schema, self.config, self.plugin_settings)
                calls.append(getattr(on_step, callback_type))

        return calls

    @contextlib.contextmanager
    def run_once(self, func: Callable[..., Any]) -> Iterator[Callable[..., Any] | None]:
        _callback_key = func.__qualname__

        if _callback_key in self._in_call:
            yield None
            return

        self._in_call.add(_callback_key)
        yield func
        self._in_call.remove(_callback_key)


class PluggableSchemaValidator:
    """Pluggable schema validator."""

    def __init__(
        self,
        schema: CoreSchema,
        config: CoreConfig | None,
        plugins: set[Plugin],
        plugin_settings: dict[str, Any] | None = None,
    ) -> None:
        self.schema_validator = SchemaValidator(schema, config)

        self.plug = _Plug(schema, config, plugins, plugin_settings)

        self.validate_json = self.plug(self.schema_validator.validate_json)
        self.validate_python = self.plug(self.schema_validator.validate_python)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.schema_validator, name)