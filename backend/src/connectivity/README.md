# connectivity

Broker abstraction layer. All live market data and order routing flows through
the `BrokerAdapter` ABC; concrete implementations swap in without changing
upstream code.

## Public API

| Symbol | File | Purpose |
|---|---|---|
| `BrokerAdapter` | `state.py` | ABC for live adapters (subscribe, place_order, get_positions) |
| `CanonicalContract` | `state.py` | Instrument identity; `instrument_key` is a computed property (not a constructor arg) |
| `SessionState` / `assert_transition` | `state.py` | State machine (DISCONNECTED → CONNECTED → LIVE); raises on illegal transitions |
| `QuoteSnapshot`, `SessionEvent` | `state.py` | Typed event payloads |
| `get_adapter` / `set_adapter` / `build_adapter_from_env` | `adapter_registry.py` | Process-global registry; `BROKER_MODE=ibkr\|mock` drives `build_adapter_from_env` |
| `Session` | `session.py` | Thin wrapper around an adapter; manages connect/disconnect lifecycle |

## Failure modes

- `assert_transition` raises `ValueError` if the state machine is called out of order — check that `connect()` precedes any subscription.
- `build_adapter_from_env` returns `None` when `BROKER_MODE` is unset; callers must guard.
- IBKR adapter may silently drop subscriptions if TWS gateway is not accepting new market data lines (max 100 simultaneous).
