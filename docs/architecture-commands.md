# Command architecture

How a `popcorn` command family is declared, and why it is declared exactly once.

## The problem

Adding one command family used to mean editing five places in `src/popcorn_cli/cli.py`:

```
                          ┌─ build_parser()          argparse subparsers
                          ├─ main()                  the elif dispatch chain
a new family  ──────────▶ ├─ _BASH_COMPLETION        compgen -W word list
   (5 edits)              ├─ _ZSH_COMPLETION         _values subcommand list
                          └─ _COMMAND_{CATEGORIES,   what `popcorn commands
                             DESCRIPTIONS}            --json` reports
```

Nothing tied them together, so they drifted independently — and the last one
matters more than it looks: `popcorn commands --json` top-level keys are
**frozen at 1.0.0** (`SPEC.md` § Versioning). A family that reached argparse but
not the description table shipped a schema that under-reported the CLI's own
surface, with no test to catch it.

## The registry

`src/popcorn_cli/registry.py` holds one declaration and derives every consumer
from it:

```
commands/<name>.py                    registry.py                    consumer
──────────────────                    ───────────                    ────────
                                 ┌──▶ add_to_parser(sub) ──────────▶ argparse
Command(                         │
  name=…, category=…,            ├──▶ dispatch(args) ──────────────▶ main()
  description=…,      ──register─┤
  subcommands=[                  ├──▶ completion_groups() ─────────▶ bash
    Subcommand(                  │    completion_words(name) ──────▶ zsh
      name=…, help=…,            │
      handler=…,                 ├──▶ categories() ────────────────▶ commands
      arguments=[Argument(…)],   │    descriptions()                 --json
      subcommands=[…],           │
    ),                           ├──▶ schema() ────────────────────▶ commands
  ],                             │                                   --json
)                                └──▶ descriptions() ──────────────▶ fuzzy
                                                                     match
```

Three dataclasses, one entry point:

| Type | Role |
|---|---|
| `Argument` | one flag or positional; `add_to(parser)` knows the argparse call |
| `Subcommand` | a leaf (has `handler`) **or** a group (has `subcommands`) |
| `Command` | a top-level family, e.g. `popcorn flow` |
| `register(cmd)` | appends to `registry.COMMANDS`; called at module import |

### Derivations

| Function | Feeds | Notes |
|---|---|---|
| `add_to_parser(subparsers)` | `build_parser()` (`cli.py:3672`) | `help=SUPPRESS`, matching the hand-written families — the epilog renders the listing |
| `dispatch(args)` | `main()` (`cli.py:3795`) | returns `False` for an unregistered command so the hand-written chain still runs; raises a usage error at whatever level the user stopped short |
| `schema()` | `cmd_commands` via `_command_categories` / `_command_descriptions` (`cli.py:2868`) | see [Where the schema really comes from](#where-the-schema-really-comes-from) |
| `completion_groups()` | `_render_bash_completion` (`cli.py:2730`) | families **and** nested groups, flat — bash keys on the previous word alone |
| `completion_words(name)` | `_render_zsh_completion` (`cli.py:2746`) | family level only, matching the template's hand-written families |
| `descriptions()` | zsh command list, bash top-level words, `_ALL_COMMAND_NAMES` (`cli.py:3710`) | |

### Argument naming

Positionals use `name` verbatim, so declare them with underscores
(`Argument("record_id", …, positional=True)` → `args.record_id`). Flags accept
either form: `Argument("page-token", …)` and `Argument("page_token", …)` both
become `--page-token` with dest `page_token`.

### Nesting and `dest`

Each level appends to the previous `dest`, so the namespace attribute is
predictable at any depth:

```
popcorn flow                 args.command             = "flow"
popcorn flow runs            args.flow_command        = "runs"
popcorn flow runs get        args.flow_runs_command   = "get"
popcorn table row patch      args.table_row_command   = "patch"
```

`registry._nested_dest` computes this and `dispatch` walks it the same way, so
the parser and the dispatcher can never disagree about where to look.

## Where the schema really comes from

`popcorn commands --json` does **not** emit `registry.schema()` directly. Per-argument
detail (`flags`, `type`, `choices`, `default`) is introspected off the built
argparse parser by `_introspect_parser` / `_describe_subcommands` — argparse
knows things the declaration does not. The registry supplies `name`, `category`
and `description`.

```
registry ──▶ argparse ──▶ _introspect_parser ──▶ arguments
    └──────────────────────────────────────────▶ name, category, description
```

So the registry is still the single source: it feeds the parser, and the parser
feeds the schema. `registry.schema()` is the registry's own whole-family view,
and `tests/test_registry.py::test_registry_families_reach_the_commands_schema`
diffs it against the emitted schema — that is what catches a family that
declared but never reached the parser.

## Migration status

| Family | Declared in | |
|---|---|---|
| `flow` | `commands/flow.py` | registry |
| `table` | `commands/table.py` | registry |
| `auth` | `cli.py` | pending |
| `channel` | `cli.py` | pending |
| `message` | `cli.py` | pending |
| `site` | `cli.py` | pending |
| `vm` | `cli.py` | pending |
| `webhook` | `cli.py` | pending |
| `workspace` | `cli.py` | pending |
| flat commands (`api`, `commands`, `completion`, `doctor`, `env`, `upgrade`, `version`, `whoami`) | `cli.py` `_COMMANDS` | pending |

Registry and hand-written families coexist indefinitely — `dispatch()` returning
`False` is the seam. Migrate opportunistically when you are already editing a
family.

## How to add a family

1. Create `src/popcorn_cli/commands/<name>.py`.
2. Write one handler per leaf subcommand, taking `args: argparse.Namespace`.
3. `register(Command(...))` at module level.
4. Import it in `commands/__init__.py`.
5. Add one line to `build_parser`'s epilog (see below).

Do **not** touch the completion generators, the schema builder,
`_COMMAND_CATEGORIES`, `_COMMAND_DESCRIPTIONS`, or `_ALL_COMMAND_NAMES`.

### The one surface still hand-maintained

`build_parser`'s `epilog` groups registry and non-registry families together
under prose headings (`Flows:`, `Tables:`, …), so it is not derived.
`test_registry_families_appear_in_the_help_epilog` fails if you forget it —
that is the intended feedback, not a silent gap.

## How to migrate a family

1. Move each dispatch branch out of the `cmd_<name>` function into its own
   handler in `commands/<name>.py`, bodies **verbatim**.
2. Declare the family and `register()` it.
3. Delete all five duplicates: the parser block, the `elif` dispatch branch,
   the bash branch, the zsh branch, and the `_COMMAND_*` entries.
4. Lean on the existing parser tests as the regression net — `test_parser.py`
   already covers the pre-migration parse shapes and must stay green untouched.

### Parser tests do not prove dispatch

A family can parse perfectly and be completely unreachable: `add_to_parser` and
`dispatch` are separate wirings. `tests/test_registry.py::TestDispatchIsWired`
drives `main()` end to end and asserts the handler ran. During the `flow`
migration every parse test passed while all four routing tests failed — that gap
is real, and only the routing tests see it.

## The import-cycle convention

`cli.py` imports `commands/` at module load to build the parser, so a handler
module must not import `cli` at module level. Import the helpers **inside** the
handler body:

```python
def _flow_list(args: argparse.Namespace) -> None:
    from ..cli import _attach_pagination, _get_client, _output   # ← inside

    client = _get_client(args)
    ...
```

`popcorn_core` imports (`operations`, `errors`) are safe at module level —
nothing in `popcorn_core` imports `popcorn_cli`.
