# Agent instructions

This private repository owns GrooveMap whole-stack deployment configuration.

- Never add service source or sibling build contexts. Consume independently
  released, digest-pinned images.
- Never commit `.env`, `secrets/`, credentials, generated Docker auth, runtime
  volumes, or performance results.
- `just check` is the required local gate.
- Do not start, stop, or change a live environment without operator approval.
- Do not publish images or releases from this repository; it is intentionally
  unversioned because source repositories own release versions.
