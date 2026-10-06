from __future__ import annotations

from contextlib import ExitStack
from typing import Any

import uvicorn

from api_migration_proxy.app import control_authorizer, create_app, create_listener_app
from api_migration_proxy.environment import RunOptions
from api_migration_proxy.proxy.runtime import ProxyRuntime


def run_server(runtime: ProxyRuntime, options: RunOptions) -> int:
    snapshot = runtime.config.current
    assert snapshot is not None
    server_options: dict[str, Any] = {
        "workers": 1,
        "access_log": False,
        "proxy_headers": False,
        "server_header": False,
        "date_header": False,
        "lifespan": "on",
        "timeout_graceful_shutdown": snapshot.budgets.shutdown_grace_seconds,
    }
    if not options.control_enabled:
        uvicorn.run(create_app(runtime), host=options.host, port=options.port, **server_options)
        return 0
    app = create_listener_app(
        runtime,
        control_port=options.control_port,
        authorize=control_authorizer(options.control_token),
    )
    config = uvicorn.Config(app, host=options.host, port=options.port, **server_options)
    control_config = uvicorn.Config(
        app, host=options.control_host, port=options.control_port, **server_options
    )
    with ExitStack() as stack:
        sockets = [
            stack.enter_context(config.bind_socket()),
            stack.enter_context(control_config.bind_socket()),
        ]
        server = uvicorn.Server(config)
        try:
            server.run(sockets=sockets)
        except KeyboardInterrupt:
            pass
    return 0 if server.started else 3
