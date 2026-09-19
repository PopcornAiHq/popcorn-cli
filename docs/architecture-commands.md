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
| `add_to_parser(subparsers)` | `build_parser()` (`cli.py`) | `help=SUPPRESS`, matching the hand-written families — the epilog renders the listing |
| `dispatch(args)` | `main()` (`cli.py`) | returns `False` for an unregistered command so the hand-written chain still runs; raises a usage error at whatever level the user stopped short |
| `schema()` | `cmd_commands` via `_command_categories` / `_command_descriptions` (`cli.py`) | see [Where the schema really comes from](#where-the-schema-really-comes-from) |
| `completion_groups()` | `_render_bash_completion` (`cli.py`) | families **and** nested groups, flat — bash keys on the previous word alone |
| `completion_words(name)` | `_render_zsh_completion` (`cli.py`) | family level only, matching the template's hand-written families |
| `descriptions()` | zsh command list, bash top-level words (`cli.py`) | |

### Argument naming

Positionals use `name` verbatim, so declare them with underscores
(`Argument("record_id", …, positional=True)` → `args.record_id`). Flags accept
either form: `Argument("page-token", …)` and `Argument("page_token", …)` both
become `--page-token` with dest `page_token`.

### Arguments with two spellings

Some arguments answer to both a positional and a flag, so a caller's guess
carried from one command to the next is always right. `registry.py —
add_dual_spelled_argument` is the one mechanism: it declares the positional
`nargs="?"` (which is what lets the flag stand in for it), declares the flag
into a scratch dest prefixed `registry.FLAG_DEST_PREFIX`, and records a
`DualSpelledArgument` spec on the parser. `cli.py —
_fold_dual_spelled_arguments` resolves each spec into the positional's dest
before any handler runs, so a handler reads one attribute and never learns
which spelling produced it.

Because `nargs="?"` makes argparse treat the positional as optional, the
requirement moves onto the spec and is enforced in the fold — which is also
what `commands --json` reports, rather than argparse's answer.

Two arguments use it:

- **The channel.** The hand-written families in `cli.py` (`message`,
  `channel`, `webhook`) take it positionally and go through `cli.py —
  _add_channel_argument`. The registry families declare it as
  `Argument("channel", …)` — a flag, never a positional: they put their own
  positionals first (`table rows <table>`), where an optional leading
  positional could not be told apart from the ones after it.
- **The directory.** Declared in the registry as `Argument("directory", …,
  positional=True, flag_alias="--dir")`, on the `app` commands and
  `template check`.

Declaring a bare `conversation`/`channel`/`directory` positional instead
re-splits the surface, and `tests/test_parser.py` — `TestChannelArgument` and
`TestDirectoryArgument` — fails if you do.

A command declaring two dual-spelled arguments would work (specs accumulate in
a tuple), but none does today: the channel families have no directory and the
directory commands take the channel as a flag.

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

Every family with subcommands is now registry-declared.

| Family | Declared in | Handlers |
|---|---|---|
| `app` | `commands/app.py` | alongside |
| `channel` | `commands/channel.py` | `cli.py`, late-bound |
| `channel-config` | `commands/channel_config.py` | alongside |
| `flow` | `commands/flow.py` | alongside |
| `message` | `commands/message.py` | `cli.py`, late-bound |
| `schedule` | `commands/schedule.py` | alongside |
| `table` | `commands/table.py` | alongside |
| `template` | `commands/template.py` | alongside |
| `webhook` | `commands/webhook.py` | alongside |
| `auth` | `commands/auth.py` | `cli.py`, late-bound |
| `workspace` | `commands/workspace.py` | `cli.py`, late-bound |
| flat commands (`api`, `commands`, `completion`, `doctor`, `env`, `upgrade`, `version`, `whoami`) | `cli.py` `_COMMANDS` | no subcommands; the registry has nothing to collapse |

`dispatch()` returning `False` is still the seam, and still needed: the flat
commands go through the hand-written chain.

### Deprecating a family

Nothing is deprecated right now — `site` and `vm` were, and both were removed
outright rather than left marked. The mechanism stays because the reason for it
holds regardless: `CLAUDE.md` names `commands --json` as the supported way for
an agent to learn what this CLI can do, so a deprecation that lives only in a
design document is a deprecation its main audience cannot see.

Set `Command.deprecated` to one line saying where to go instead. It reaches the
`deprecated` key in the schema via `registry.deprecations()`. It does **not**
reach `--help`: that listing is a hand-written epilog string in
`cli.py — build_parser` that nothing generates, so mark the family there in the
same commit. `tests/test_registry.py — TestDeprecatedFamilies` exercises the
schema path end to end by registering a throwaway family, since there is no
real one left to assert against.

The lesson from removing both: a marker is worth setting when a family has
callers who need a signal, and worth skipping when it has none. `vm` had no
caller anywhere and could have gone straight out; `site` had four plugin skills
and a local-filesystem role nothing else filled, so it was marked first and
removed only once those callers were retired.

### Surface-only migrations

A family can be declared here while its handler bodies stay in `cli.py`,
resolved at call time by `commands/_late.py — late_handler`. The registry's
guarantee is that the **surface** is declared once — argparse, dispatch, both
completions and `commands --json` cannot drift apart. Where a function body
lives is a separate question, and moving one buys nothing the registry cares
about: `cmd_auth_login` alone is ~150 lines of OAuth flow with its own test
module importing it by name.

The cost is that a handler name is a string resolved at call time, so a typo
survives every argument-parsing test and fails only when a user runs the
command. `test_registry.py — TestLateBoundHandlers` asserts every late-bound
name resolves on `cli`; three of `message`'s nine were wrong on first draft.

### Before you migrate a family

`tests/test_parser_parity.py` records the exact `Namespace` each subcommand
parses to, invoked minimally, with every optional argument named, and — where
an argument is dual-spelled — through its flag form. Every family is covered,
migrated or not. Re-declaring a family has to reproduce all of it.

It pins parsed VALUES, not argparse metadata, so a change to how an option is
spelled or ordered in `--help` passes through it silently. Check
`popcorn commands --json` by hand when you touch a declaration's flags. This exists because a migration deletes the
hand-written parser in the same commit, so there is otherwise nothing left to
diff against, and two behaviour losses had already gone through that gap.

## How to add a family

1. Create `src/popcorn_cli/commands/<name>.py`.
2. Write one handler per leaf subcommand, taking `args: argparse.Namespace`.
3. `register(Command(...))` at module level.
4. Import it in `commands/__init__.py`.
5. Add one line to `build_parser`'s epilog (see below).

Do **not** touch the completion generators, the schema builder,
`_COMMAND_CATEGORIES`, or `_COMMAND_DESCRIPTIONS`.

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
