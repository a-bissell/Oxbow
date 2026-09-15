# Oxbow: offline install

This directory is a release of Oxbow, the oxide dielectric triage assistant, packaged for a machine
that will never see the network. Everything the tool needs is here; nothing is fetched.

```
oxide-triage-<version>-offline/
├── OFFLINE.md                        this file
├── manifest.json                     what the cache was built from; SHA-256 of cache.sqlite
├── bundle/
│   ├── cache.sqlite                  every retrieved record, with its source and timestamp
│   └── manifest.json                 same manifest, beside the file it describes
├── docker/
│   ├── compose.yml                   the base deployment
│   ├── compose.offline.yml           overlay: cache-only, bundle mounted, image never pulled
│   └── OFFLINE.md
├── config/                           shipped configuration (default.yaml and the profiles)
└── .env                              runtime settings for an offline site (no keys needed)
```

Two more files ship beside this directory as separate downloads, so the cache can be refreshed
without re-downloading the image:

* `oxide-triage-<version>-image.tar.zst`: the application container image.
* `oxide-triage-<version>-wheels-<platform>.tar.gz`: the Python package and every dependency as
  wheels, for a machine with Python 3.11+ and no Docker.

`SHA256SUMS` lists every download. Check it before carrying anything across:
`sha256sum -c SHA256SUMS`.

## With Docker

```bash
zstd -d oxide-triage-<version>-image.tar.zst        # if your docker cannot read zstd directly
docker load < oxide-triage-<version>-image.tar
cd oxide-triage-<version>-offline
docker compose -f docker/compose.yml -f docker/compose.offline.yml \
    run --rm app oxide-triage bundle install /bundle
docker compose -f docker/compose.yml -f docker/compose.offline.yml up -d
```

Open http://localhost:8000. `bundle install` verifies the checksum, the self-check recorded at
build time and the release stamp inside the cache before it copies anything; it refuses to
replace a populated cache unless you pass `--force`. To refresh the data later, replace the
`bundle/` directory with a newer one and run the install step again with `--force`.

## Without Docker

Python 3.11 or newer on the same platform the wheels were built for (named in the archive).

```bash
tar xzf oxide-triage-<version>-wheels-<platform>.tar.gz
python3 -m venv triage && . triage/bin/activate
pip install --no-index --find-links wheels "oxide-triage[web,llm,mcp]"
cd oxide-triage-<version>-offline           # holds config/; or set OXIDE_TRIAGE_CONFIG_DIR
export OXIDE_TRIAGE_CACHE=$PWD/data/cache.sqlite OXIDE_TRIAGE_OFFLINE=1
oxide-triage bundle install bundle
oxide-triage serve --open              # or: oxide-triage query --offline
```

## What to know

* **The cache has a date.** `manifest.json` records when each source was retrieved and the
  commit the release was built from. `oxide-triage doctor` prints the release line once
  installed, and so does the web app's status. Nothing updates itself.
* **Only the shipped universe is answerable.** A request about a compound the warm did not
  fetch is reported as not retrieved, never guessed at. `add-material` and `fill-gaps` need the
  network and say so.
* **No language model is included.** The assistant is driven by rules, which reads the same
  request vocabulary and narrates the same result object. To use a local model, bring its
  weights and an OpenAI-compatible server (vLLM, Ollama, llama.cpp) on your own media and set
  `LLM_PROVIDER=openai_compatible`, `LLM_BASE_URL` and `LLM_MODEL`.
* **Every output still names its sources.** The records in the cache are the same records a
  networked install holds, with the same timestamps and identifiers, so the audit view and the
  HTML report cite exactly what a colleague online can open.
