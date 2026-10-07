# Public engine, private alpha library

This repository holds the engine: data, cost model, backtester, validation, experiment registry, Risk Governor,
execution, paper book, economics and the UI. Live research candidates do not live here. Their exact rules,
parameters, signal plug-ins, structure specs and research-round configs sit in a separate, private **alpha library**.

`src/project100c/alpha.py` is the boundary:

- **Discovery.** `$P100C_ALPHA_DIR` (must exist if set), else `<repo>/private_alpha/` (gitignored). Absent = the public
  engine on its own; every test passes that way.
- **Plug-ins.** If the library has a `private_alpha` Python package with a `PLUGINS` dict, the strategy library adds
  those ids at import (`strategies.library.PRIVATE_PLUGIN_IDS`). A private id may never replace a public one.
- **Specs.** `load_library_specs` reads the public `specs/` and then the library's `specs/`; structure specs come from
  `specs/structures/`. Private specs go through the same schema, loaders and Governor.
- **Configs.** `alpha.config_path(rel, configs)` returns the library's `configs/<rel>` when present (for example a
  research round or holdout with candidate looks), else the public file.

Nothing in the library gains live authority: live orders still need a VALIDATED spec, the Governor, the kill
switches and the live lock. Synthetic stand-ins for the private structures are in `examples/`.
