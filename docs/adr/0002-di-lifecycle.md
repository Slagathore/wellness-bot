ADR 0002: DI Container & Lifecycle Manager
==========================================

Context
-------
`unified_bot.py` holds globals for config, DB/vector clients, thread pools, and UI state. We need controlled construction, sharing, and teardown of resources with testability.

Decision
--------
- Create a lightweight DI container to register singletons (config, logger, metrics, DB pool, vector client, LLM client, event bus, schedulers).
- Add lifecycle manager with startup/shutdown hooks to orchestrate resources and ensure graceful teardown.
- Enforce dependency direction: domain services depend on container-resolved ports; adapters register implementations.

Consequences
------------
- Clear ownership and teardown of resources; fewer leaks and dangling threads.
- Tests can inject fakes/mocks per interface.
- Slight indirection cost; mitigated by caching singletons and simple container API.

