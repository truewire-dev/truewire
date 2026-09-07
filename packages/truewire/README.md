# truewire

Typed clients, true to the wire.

`truewire` turns an API — documented or not, REST or WebSocket — into a typed, validated
client, by recording real wire examples and generating code, tests and mocks from them.

- `truewire check` — lint a spec and replay every recorded example against its schema.
- `truewire examples` — paired-example coverage, with `--require-verified` as a gate.
- `truewire surface` — reconcile every spec against the callables the package really has.
- `truewire mock` — serve a project's recorded examples over HTTP and WebSocket.
- `truewire generate` — generate the Python package from the spec.
- `truewire standards` — every mechanically-checkable production rule, in one pass.
- `truewire docs check|lint` — type-check and lint the code blocks in a project's docs.

A project is a directory holding a `truewire.toml`; see `truewire init`.
