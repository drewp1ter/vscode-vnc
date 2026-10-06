# Vast.ai auto-order script

## Context

GPU offers on Vast.ai that match a specific spec/price come and go. The goal is a Python script that
polls the marketplace until a matching **on-demand** offer appears, rents it immediately, waits
for the instance to come up, runs a setup command on it over SSH (e.g. `ollama pull qwen3`) and waits
for it to finish, then holds an SSH tunnel `localhost:11434 -> instance:11434` so the remote Ollama
is usable locally. It uses the `vastai` Python SDK (the same package vendored as the
project skill in `.agents/skills/vastai/`), with a configurable image defaulting to `ollama/ollama`.

## Files

- **New** `/workspace/vscode-vnc/vast_order.py` — the script (single file, argparse CLI).
- **New** `/workspace/vscode-vnc/requirements.txt` — `vastai`.
- No changes to `Dockerfile` / `docker-compose.yml`.

## SDK calls used (verified in `.agents/skills/vastai/sdk.py`)

- `VastAI(api_key=None)` — key resolved from `VAST_API_KEY`, then `~/.config/vastai/vast_api_key`
  (what `vastai set api-key` writes). Script adds an optional `--api-key` override.
- `show_user()` — auth check + credit balance at startup.
- `search_offers(query=<str>, type="on-demand", order=..., limit=..., storage=<disk>)` → list of offer
  dicts (`id`, `dph_total`, `gpu_name`, `num_gpus`, `reliability`, `geolocation`, ...).
- `create_instance(id, image=, disk=, ssh=True, direct=True, label=, env=, onstart_cmd=, cancel_unavail=True)`
  → `{"success": true, "new_contract": <instance_id>}`; raises `requests.HTTPError` if the offer is gone.
- `show_instance(id)` → dict with `actual_status`, `ssh_host`, `ssh_port`.
- `destroy_instance(id)` — cleanup of an instance that failed to boot.

## Defaults in the script

The three main filters are constants at the top of `vast_order.py`, so the script runs with no
arguments (`python vast_order.py`) and is tuned by editing these lines; the CLI flags only override them:

```python
DEFAULT_GPUS = ["RTX_4090"]   # accepted GPU models
DEFAULT_NUM_GPUS = 1          # GPUs per instance
DEFAULT_MAX_PRICE = 0.50      # max total $/hr

DEFAULT_IMAGE = "ollama/ollama"
DEFAULT_ONSTART = "nohup ollama serve > /var/log/ollama.log 2>&1 &"   # ssh runtype skips the image entrypoint
DEFAULT_REMOTE_CMD = "ollama pull qwen3"   # run over SSH once the instance is ready
DEFAULT_TUNNEL_PORT = 11434                # same port on both sides
```

(Starting values are placeholders — tell me the ones you want, or edit them after.) The effective
filters are logged at startup so it's always clear what will be rented.

## CLI

| Flag | Default | Purpose |
|---|---|---|
| `--gpu NAME [NAME ...]` | `DEFAULT_GPUS` | Accepted GPU model(s), e.g. `--gpu RTX_4090 RTX_3090` (spaces or underscores both accepted) |
| `--num-gpus` | `DEFAULT_NUM_GPUS` | Exact GPU count per instance |
| `--max-price` | `DEFAULT_MAX_PRICE` | Max total cost in $/hr (`dph_total`, includes storage for `--disk`) |
| `--extra-query` | none | Optional extra Vast filter terms appended verbatim (e.g. `geolocation=EU reliability>0.98`) |
| `--order` | `dph_total` | sort (cheapest first) |
| `--image` | `DEFAULT_IMAGE` (`ollama/ollama`) | Docker image; default changed from the generic base so `ollama` exists on the box |
| `--remote-cmd` | `DEFAULT_REMOTE_CMD` | Command run over SSH after boot; script waits for it. `''` skips |
| `--tunnel-port` | `DEFAULT_TUNNEL_PORT` | `-L PORT:localhost:PORT`. `0` disables the tunnel |
| `--ssh-key` | `~/.ssh/id_ed25519` | Private key (its `.pub` is registered with Vast if missing) |
| `--destroy-on-exit` | off | Destroy the instance when the tunnel is closed (Ctrl-C) |
| `--disk` | `20` | GB |
| `--label` | `auto-order` | instance label |
| `--env` | none | passed through |
| `--onstart-cmd` | `DEFAULT_ONSTART` | passed through |
| `--interval` | `30` | seconds between searches |
| `--timeout` | `0` (forever) | give up searching after N seconds |
| `--boot-timeout` | `600` | max seconds to reach `running` |
| `--dry-run` | off | search/poll and print the offer that would be rented, never create |
| `--api-key` | none | override stored key |

## Filtering

The two primary filters are GPU and max cost. `build_query(args)` turns them into the SDK query string:

```
gpu_name in [RTX_4090,RTX_3090] num_gpus=1 dph_total<=0.45 direct_port_count>=1 [extra-query]
```

- GPU names are normalised to underscore form (`"RTX 4090"` → `RTX_4090`); the SDK parser
  (`api/query.py` `parse_query`) converts underscores back to spaces and supports `in [a,b]` lists.
  A single name uses `gpu_name=...`.
- SDK defaults (`verified=true rentable=true external=false`) stay on (`no_default=False`).
- The price cap is also re-checked client-side (`offer["dph_total"] <= max_price`) and the GPU name
  re-checked against the accepted set right before renting, so a stale/mismatched search result is
  never rented.
- Results are ordered by `--order` (default cheapest first), so with several accepted GPUs the
  cheapest matching one wins.

## Flow

1. Parse args, build client, `show_user()` → print balance; exit with a clear message on 401 / zero credit.
2. **Search loop**: `search_offers(...)` with `limit=5`. Network/HTTP errors here are logged and retried
   on the next tick (the loop must survive long waits). No offers → sleep `--interval`, repeat until
   `--timeout`.
3. **Rent**: iterate returned offers in order; `create_instance` on each; on `HTTPError` (offer taken
   between search and rent) try the next one; if all fail, go back to the search loop.
   Exactly one instance is ever created per run.
4. **Wait for boot**: poll `show_instance` every 10 s until `running`. If status is
   `exited`/`unknown`/`offline` or `--boot-timeout` elapses (per the SKILL.md poll-loop warning) →
   `destroy_instance`, add that offer ID to a skip set, resume the search loop.
5. **Wait for SSH**: resolve the endpoint the same way the CLI does (`cli/commands/misc.py`): direct
   `public_ipaddr` + `ports["22/tcp"][0]["HostPort"]` if present, else proxy `ssh_host`/`ssh_port`.
   Retry `ssh ... true` every 5 s (up to 3 min) since sshd lags `running` by a few seconds.
6. **Remote command**: `ssh <opts> root@host -p port '<wait-for-ollama>; <remote-cmd>'` via
   `subprocess.run`, stdio inherited so pull progress is visible (`-t` when stdout is a TTY). The
   command is prefixed with `until ollama list >/dev/null 2>&1; do sleep 2; done` only when it starts
   with `ollama`, so the pull doesn't race the server start. Blocks until done; non-zero exit → log
   the error, leave the instance up, print SSH + destroy commands, exit 1 (no tunnel).
7. **Tunnel**: `ssh -N -L 127.0.0.1:11434:localhost:11434 -o ExitOnForwardFailure=yes
   -o ServerAliveInterval=30 ...` in the foreground. Before starting, check local port 11434 is free
   and fail with a clear message if not. If ssh drops while the instance is still `running`,
   reconnect after 5 s. Runs until Ctrl-C.
8. **Exit / Ctrl-C**: the instance keeps running (and billing) by default — the script prints its ID
   and `vastai destroy instance <id> -y`. With `--destroy-on-exit` it is destroyed instead. Applies
   at any stage after creation. Exit 130 on interrupt.

Common SSH options: `-i <key> -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=~/.ssh/known_hosts
-o ConnectTimeout=10`.

**SSH key pre-flight (step 1)**: Vast only injects keys registered *before* creation. Read
`<ssh-key>.pub` (exists here: `~/.ssh/id_ed25519.pub`); if it's not in `show_ssh_keys()`, add it with
`create_ssh_key(pub)`. Missing key file or `ssh` binary → exit before renting anything.

Logging via `logging` with timestamps to stderr; the final result line goes to stdout.

## Notes

- A copy of this plan is saved in the repo at `docs/vast_order_plan.md` (re-synced after implementation
  if anything changes).
- System `python3` (3.12, PEP 668) has neither `vastai` nor `requests`; run from a venv:
  `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` (`venv` is available here).
  Alternatively the interpreter bundled by the Vast installer,
  `~/.local/share/vastai/current/bin/python`, already imports `vastai` and can run the script as-is.
- No API key is configured yet in this environment (`~/.config/vastai` absent, no `VAST_API_KEY`), so
  a real run needs `vastai set api-key <KEY>` or `VAST_API_KEY` first.

- The tunnel binds `127.0.0.1:11434` where the script runs. This repo's opencode config points at
  `host.docker.internal:11434`; if the script runs inside the container, tools there should use
  `http://localhost:11434` (or run the script on the Docker host with Ollama not already on that port).
- `ollama/ollama` under Vast's SSH runtype doesn't run its entrypoint, hence `ollama serve` in onstart.

## Verification

1. `python vast_order.py --help` — arg parsing; help shows the in-script defaults. `python vast_order.py --dry-run --timeout 30` with no filter flags uses the constants.
2. `python vast_order.py --gpu RTX_4090 RTX_3090 --max-price 0.50 --dry-run --timeout 60` — exercises
   auth, query building, search and the poll loop without spending anything (requires an API key);
   check every printed offer has an accepted GPU and `dph_total <= 0.50`.
3. `--dry-run --gpu RTX_4090 --max-price 0.001 --timeout 45` — confirms polling and clean timeout
   exit code.
4. Without an API key: unit-check `build_query` against the SDK's `parse_query` (single GPU, multiple
   GPUs, names with spaces) to confirm it parses to the expected filter dict.
5. Real rental (spends credits, so I'll ask before running it): run without `--dry-run`; expect the
   pull progress to stream, then `curl http://localhost:11434/api/tags` in another shell lists `qwen3`
   through the tunnel. Ctrl-C, then `vastai destroy instance <id> -y` (or use `--destroy-on-exit`).
