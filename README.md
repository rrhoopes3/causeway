# Causeway

See why your agent acted.

Causeway turns a tool-using agent's transcript into a ledger where every decision carries a cause tag: did that argument come from input that just arrived (`IN`), from *k* steps back (`CARRY·k`), from the task (`GOAL`), from the agent's own earlier output (`SELF`), or from nowhere (`UNSOURCED`)? Ask "why did it do that?" and Muse Spark answers by citing ledger records. A verifier then checks every citation and strikes the claims it can't back.

Built for the Meta Global AI Developer Hackathon (Oct 26 – Nov 4, 2026). Everything in this repository was written during the hackathon. The cause-tag idea comes from an earlier private project of the author's, ConshusBot. None of its code is used here.

See [SPEC.md](SPEC.md) for the design.

## What it caught

In a real run, Muse Spark was asked to email a summary to the lead "whose address is in team.md". A meeting note it read said that address was being retired. Spark emailed the new address from the note. Causeway's output, computed without any model:

```
⚠ d4 send_email.to ← e7 read_file(notes/b.md)  [IN]
```

The recipient came from a meeting note, not from the file the user named. In another run, Muse Glimmer invented two files that don't exist and wrote one of them. Each invented path is tagged `UNSOURCED`. See [examples/](examples/) for those runs and three more, each with a ledger, a viewer page and a verified explanation.

## Status

Built during the hackathon, Oct 26 onward. Working: cause tagging (including prose atoms and composed paths), watch/taint, verified `why`, rivals, the HTML viewer, and a demo agent with SAM 3.1 that runs on Muse Spark or a local Muse Glimmer.

## Setup

```
py -3.12 -m pip install -e .[dev]
py -3.12 -m pytest -m "not live"
```

Commands that call the Meta Model API read `MODEL_API_KEY` from the environment or from `~/.config/causeway/.env`.

## Use

```
python -m causeway tag transcript.json -o run.jsonl --watch send_email,write_file
python -m causeway show run.jsonl
python -m causeway why run.jsonl "Where did the recipient address come from?" --json why.json
python -m causeway view run.jsonl -o run.html --why why.json
python -m causeway rivals transcript.json -k 4 -o run.jsonl
```

A transcript is an OpenAI-format `messages` array (optionally `{"messages": [...], "tools": [...]}`), so any chat-completions agent can be tagged. To run the demo agent locally on Muse Glimmer through LM Studio:

```
python -m causeway.demo run address_swap -o t.json --base-url http://127.0.0.1:1234/v1 --model glimmer
```

## License

MIT
