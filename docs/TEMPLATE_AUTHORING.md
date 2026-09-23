# Authoring a channel template

This guide now lives on the docs site:

**<https://docs.popcorn.ai/guides/template-authoring.html>**

Plain Markdown, for an agent or a `curl`:

```
https://docs.popcorn.ai/guides/template-authoring.md
```

It moved because it describes the platform rather than this CLI — the bundle
grammar, the manifest semantics and the install rules are the same whichever
surface you author from, and a copy here would be a second version of them to
disagree with.

This file stays so that references to `docs/TEMPLATE_AUTHORING.md` elsewhere in
this repository keep resolving. Section numbers are unchanged, so a pointer to
"§2" still finds the same section on the site.

The tools remain the authority over the guide wherever they disagree:

```bash
popcorn flow activities --summary          # what can I call?
popcorn flow validate my_flow.yaml         # is this reference real?
popcorn template check ./mytemplate        # does the bundle hold together?
```
