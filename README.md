# Mech Royale bot (Builders Buildathon)

A `mechfight/1` bot for the 16-mech battle royale on the Ohio State campus. Placement scores
(`16 - place`), so the bot plays to **survive**: zone safety first, then defence, then loot,
then combat quality.

## Layout

| File | What it is |
| --- | --- |
| `bot.py` | The bot: decision loop, survival / defence / economy / combat logic. |
| `world.py` | Map module: loads `/arena/v2/world.json` + `terrain.i16`, builds a 4 m walkable grid, A\* pathfinding, line-of-sight. |
| `mechfight.py` | Protocol wrapper (stdin/stdout loop, typed messages, debug drawing). Unchanged from the kit. |
| `Dockerfile` | Submission image. Build from the repo root. |
| `check/check.py` | Conformance check from the kit. |
| `NOTES.md` | Change log: commit hash → change → official score. |
| `arena/`, `mechfight.schema.json`, `protocol.md` | Kit map, schema and protocol (for local checks). |

## Run the check locally

```sh
python check/check.py -- python -u bot.py
```

Container form (what the scorer runs):

```sh
docker build -t my-bot .
python check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" my-bot
```

## Outside code and libraries

- **`mechfight.py`** — the protocol wrapper, copied unchanged from the official
  `mechfight-kit.tar.gz` (Builders Buildathon). MIT licensed.
- **`check/check.py`**, **`mechfight.schema.json`**, **`protocol.md`**, **`arena/`** — official kit
  files, unmodified, kept only for local conformance checks.
- No other outside libraries: the bot is Python standard library only.

## License

MIT — see `LICENSE`.
