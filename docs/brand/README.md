# Oxbow brand assets

Oxbow is the product name; `oxide-triage` stays the package, command and environment-variable
prefix, so a later component (say, an `oxide-discovery`) can sit beside it under the same name.

| File | Use |
|---|---|
| `oxbow-mark.svg` | the mark alone: favicon, avatar, top bar (also `ui/public/favicon.svg`) |
| `oxbow-lockup.svg` | mark, wordmark and tagline on white |
| `oxbow-lockup-dark.svg` | the same on ink, for dark grounds |

**The mark.** An oxbow in plan view: the river takes the short path along the bottom and the
bend it abandoned sits above as a lake. Triage, literally: the channel is what goes forward,
the lake is what was cut off, and the tool tells you which is which. That it also reads a little
like a figure is fine for an assistant.

**Colours.** Ink `#14213d`, river `#2a6f97`, lake `#8ecae6`; in the web app the river takes the
UI accent and the lake `--brand-lake`. One rust `#c2552b` is reserved for the oxide if an accent
is ever needed. The wordmark is a system sans in these files; a print version should outline it.
