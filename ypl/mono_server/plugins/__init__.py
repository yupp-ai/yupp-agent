"""Gateway plugin implementations for the AHS monolith.

Each module in this package provides a concrete :class:`GatewayPlugin`
implementation that wraps one external HTTP gateway.  New gateways are added
by creating a module here and registering the class in
``ypl.mono_server.server.discover_plugins``.
"""
