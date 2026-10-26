# Causeway

See why your agent acted.

Causeway turns a tool-using agent's transcript into a ledger where every decision carries a cause tag: did that argument come from input that just arrived (`IN`), from *k* steps back (`CARRY·k`), from the task (`GOAL`), from the agent's own earlier output (`SELF`), or from nowhere (`UNSOURCED`)? Ask "why did it do that?" and Muse Spark answers by citing ledger records. A verifier then checks every citation and strikes the claims it can't back.

Built for the Meta Global AI Developer Hackathon (Oct 26 – Nov 4, 2026). Everything in this repository was written during the hackathon. The cause-tag idea comes from an earlier private project of the author's, ConshusBot. None of its code is used here.

See [SPEC.md](SPEC.md) for the design.

## Status

Under construction during the hackathon.

## Setup

```
py -3.12 -m pip install -e .[dev]
py -3.12 -m pytest
```

Commands that call the Meta Model API read `MODEL_API_KEY` from the environment or from `~/.config/causeway/.env`.

## License

MIT
