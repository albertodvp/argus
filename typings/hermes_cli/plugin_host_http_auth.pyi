from collections.abc import Awaitable, Callable, Coroutine

# an ASGI app as tests/hermes_integration_cases.py passes it
_App = Callable[
    [dict[str, object], Callable[[], Awaitable[object]], Callable[[object], Awaitable[None]]],
    Coroutine[object, object, None],
]

# used by tests/hermes_integration_cases.py only: the plugin host's carrier of the dashboard
# server's guard decision (`plugins.isolation: host`)
def with_hosted_admission(app: _App, authenticated: object) -> _App: ...
