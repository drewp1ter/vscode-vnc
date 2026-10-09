#!/usr/bin/env python3
"""Wait for a matching Vast.ai offer and launch a Vast template on it.

Flow: poll offers -> rent the cheapest match with the template -> wait until
running -> optionally run a remote command over SSH (--remote-cmd) and hold an
`ssh -L` tunnel (--tunnel-port) until Ctrl-C.

Requires the `vastai` package (pip install -r requirements.txt) and an API key
(`vastai set api-key <KEY>` or the VAST_API_KEY environment variable).
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from vastai import VastAI

try:  # terminal key listener; unavailable on Windows
    import select
    import termios
    import tty
except ImportError:
    termios = None

# --- Defaults: edit these, or override them with the matching CLI flags ------

DEFAULT_GPUS = []  # accepted GPU models (--gpu)
DEFAULT_NUM_GPUS = 1  # GPUs per instance (--num-gpus)
DEFAULT_MIN_GPU_RAM = 0  # min RAM per GPU in GB, 0 = any (--min-gpu-ram)
DEFAULT_OFFER_TYPE = "on-demand"  # or "interruptible" (--type)
DEFAULT_MAX_PRICE = 0.40  # max total $/hr (--max-price)
DEFAULT_MAX_DOWNLOAD_TB_COST = 3.12  # max internet download cost, $/TB (--max-download-tb-cost)

DEFAULT_TEMPLATE_HASH = ""  # https://cloud.vast.ai/template/readme/<hash>
# Optional SSH steps after the instance is running; leave empty/0 to just start it.
DEFAULT_REMOTE_CMD = "ollama pull robit/qwen3.8-27b-obliterated-e03:27b"  # command run over SSH once the instance is ready
DEFAULT_TUNNEL_PORT = 11434  # forwarded as localhost:PORT -> instance:PORT

# -----------------------------------------------------------------------------

VAST_TYPES = {"on-demand": "on-demand", "interruptible": "bid"}  # --type -> API search type
ORDER_LOG_FILE = Path(__file__).with_name("vast.log")  # one JSON line per rented order
BLACKLIST_FILE = Path(__file__).with_name("blacklist.jsonl")  # one JSON line per blacklisted order
GB_PER_TB = 1024  # Vast prices bandwidth per GB; Vast's own UI shows $/TB as $/GB * 1024
OFFERS_PER_SEARCH = 5
BOOT_POLL_SECONDS = 10
SSH_READY_TIMEOUT = 180
SSH_RETRY_SECONDS = 5
# These statuses never recover to "running"; the instance must be replaced.
DEAD_STATUSES = {"exited", "unknown", "offline"}

log = logging.getLogger("vast_order")


class OrderError(Exception):
    """Fatal error with a message meant for the user."""


class Blacklisted(Exception):
    """The user pressed the blacklist key: drop the current instance and find another."""


class Blacklist:
    """Offers the user rejected, persisted in BLACKLIST_FILE.

    Offer ids are short-lived, so the host machine is blocked as well.
    """

    def __init__(self, path: Path):
        self.path = path
        self.offer_ids: set = set()
        self.machine_ids: set = set()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            self._remember(record.get("order_id"), record.get("machine_id"))
        if lines:
            log.info("loaded %d blacklisted order(s) from %s", len(lines), path.name)

    def _remember(self, order_id, machine_id) -> None:
        if order_id is not None:
            self.offer_ids.add(order_id)
        if machine_id is not None:
            self.machine_ids.add(machine_id)

    def blocks(self, offer: dict) -> bool:
        return offer.get("id") in self.offer_ids or offer.get("machine_id") in self.machine_ids

    def add(self, offer: dict) -> None:
        self._remember(offer.get("id"), offer.get("machine_id"))
        record = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "order_id": offer.get("id"),
            "machine_id": offer.get("machine_id"),
            "gpu": offer.get("gpu_name"),
            "cost_per_hour": offer.get("dph_total"),
        }
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as exc:
            log.warning("could not write %s: %s", self.path, exc)

    def __len__(self) -> int:
        return len(self.offer_ids) + len(self.machine_ids)


class KeyWatcher:
    """Background thread that sets `pressed` when the blacklist key is typed in the terminal."""

    def __init__(self, key: str):
        self.key = key.lower()
        self.pressed = threading.Event()
        self._saved = None
        self._fd = None

    def __enter__(self):
        if termios is None or not sys.stdin.isatty():
            log.warning("no interactive terminal: the blacklist key is disabled")
            return self
        self._fd = sys.stdin.fileno()
        self._saved = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)  # keys arrive immediately; Ctrl-C still works
        threading.Thread(target=self._listen, daemon=True).start()
        log.info("press '%s' to blacklist the current order, cancel it and search again", self.key)
        return self

    def __exit__(self, *exc):
        if self._saved is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)

    def _listen(self) -> None:
        while True:
            try:
                if select.select([self._fd], [], [], 0.2)[0]:
                    ch = sys.stdin.read(1)
                    if not ch:
                        return
                    if ch.lower() == self.key:
                        self.pressed.set()
            except (OSError, ValueError):
                return

    def check(self) -> None:
        if self.pressed.is_set():
            raise Blacklisted

    def sleep(self, seconds: float) -> None:
        """Sleep, but wake up and raise Blacklisted as soon as the key is pressed."""
        if self.pressed.wait(seconds):
            raise Blacklisted

    def run(self, cmd: list[str]) -> int:
        """subprocess.run that is killed when the key is pressed."""
        with subprocess.Popen(cmd, stdin=subprocess.DEVNULL) as proc:
            try:
                while True:
                    try:
                        return proc.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        self.check()
            except BaseException:
                proc.terminate()
                raise


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--gpu", nargs="+", default=DEFAULT_GPUS, metavar="NAME",
                   help="accepted GPU model(s), e.g. RTX_4090 RTX_3090")
    p.add_argument("--num-gpus", type=int, default=DEFAULT_NUM_GPUS,
                   help="GPUs per instance")
    p.add_argument("--min-gpu-ram", type=float, default=DEFAULT_MIN_GPU_RAM,
                   help="min RAM per GPU in GB (0 = no filter)")
    p.add_argument("--max-price", type=float, default=DEFAULT_MAX_PRICE,
                   help="max total cost in $/hr")
    p.add_argument("--max-download-tb-cost", type=float, default=DEFAULT_MAX_DOWNLOAD_TB_COST,
                   help="max internet download cost in $/TB of downloaded data (negative = no filter)")
    p.add_argument("--extra-query", default="",
                   help="extra Vast filter terms, e.g. 'geolocation=EU reliability>0.98'")
    p.add_argument("--type", dest="offer_type", choices=["on-demand", "interruptible"],
                   default=DEFAULT_OFFER_TYPE,
                   help="interruptible = cheaper spot instance that can be preempted")
    p.add_argument("--bid-price", type=float, default=None,
                   help="$/hr bid for interruptible instances (default: the offer's suggested min bid)")
    p.add_argument("--order", default="dph_total", help="offer sort field")
    p.add_argument("--template-hash", default=DEFAULT_TEMPLATE_HASH,
                   help="Vast template to launch (image, env and onstart come from it); "
                        "if empty, you pick one of your own templates from a list")
    p.add_argument("--disk", type=float, default=48, help="disk size in GB")
    p.add_argument("--label", default="auto-order", help="instance label")
    p.add_argument("--remote-cmd", default=DEFAULT_REMOTE_CMD,
                   help="command run over SSH once the instance is ready ('' to skip)")
    p.add_argument("--tunnel-port", type=int, default=DEFAULT_TUNNEL_PORT,
                   help="port forwarded from localhost to the instance (0 to disable)")
    p.add_argument("--ssh-key", default="~/.ssh/id_ed25519",
                   help="SSH private key; its .pub is registered with Vast if missing")
    p.add_argument("--destroy-on-exit", action="store_true",
                   help="destroy the instance when the script exits")
    p.add_argument("--interval", type=float, default=30,
                   help="seconds between offer searches")
    p.add_argument("--timeout", type=float, default=0,
                   help="give up searching after this many seconds (0 = never)")
    p.add_argument("--boot-timeout", type=float, default=600,
                   help="max seconds for a rented instance to reach 'running'")
    p.add_argument("--blacklist-key", default="b", metavar="KEY",
                   help="key that blacklists the rented order, cancels it and searches again")
    p.add_argument("--dry-run", action="store_true",
                   help="print the offer that would be rented and exit without renting")
    p.add_argument("--api-key", default=None, help="override the stored Vast API key")
    args = p.parse_args()
    args.gpu = [normalize_gpu(g) for g in args.gpu]
    args.ssh_key = Path(args.ssh_key).expanduser()
    # The instance is only reached over SSH when a command or tunnel is requested.
    args.use_ssh = bool(args.remote_cmd.strip() or args.tunnel_port)
    return args


def normalize_gpu(name: str) -> str:
    """'RTX 4090' -> 'RTX_4090' (the form the Vast query syntax expects)."""
    return "_".join(name.split())


def build_query(args: argparse.Namespace) -> str:
    parts = [
        f"num_gpus={args.num_gpus}",
        f"dph_total<={args.max_price}",
        "direct_port_count>=1",
    ]
    if args.max_download_tb_cost >= 0:
        parts.append(f"inet_down_cost<={args.max_download_tb_cost / GB_PER_TB:.9f}")  # field is $/GB
    if len(args.gpu) == 1:
        parts.append(f"gpu_name={args.gpu[0]}")
    else: 
        if len(args.gpu) > 1:
            parts.append(f"gpu_name in [{','.join(args.gpu)}]")
    if args.min_gpu_ram > 0:
        parts.append(f"gpu_ram>={args.min_gpu_ram:g}")  # per-GPU RAM, GB
    if args.extra_query.strip():
        parts.append(args.extra_query.strip())
    return " ".join(parts)


def offer_matches(offer: dict, args: argparse.Namespace) -> bool:
    """Re-check the key filters locally so a stale search result is never rented."""
    price = offer.get("dph_total")
    down_cost = offer.get("inet_down_cost")
    if args.max_download_tb_cost >= 0 and (
        down_cost is None or down_cost * GB_PER_TB > args.max_download_tb_cost + 1e-6
    ):
        return False
    return (
        price is not None
        and price <= args.max_price
        and offer.get("num_gpus") == args.num_gpus
    )


def describe_offer(offer: dict) -> str:
    return (
        f"offer {offer.get('id')}: {offer.get('num_gpus')}x {offer.get('gpu_name')} "
        f"{(offer.get('gpu_ram') or 0) / 1024:.0f}GB/GPU "
        f"${offer.get('dph_total', 0):.3f}/hr "
        f"down=${(offer.get('inet_down_cost') or 0) * GB_PER_TB:.2f}/TB "
        f"reliability={offer.get('reliability', 0):.3f} "
        f"location={offer.get('geolocation')}"
    )


def bid_price(args: argparse.Namespace, offer: dict) -> float | None:
    """$/hr bid for an interruptible offer: --bid-price, else the offer's suggested minimum bid."""
    if args.offer_type != "interruptible":
        return None
    if args.bid_price:
        return args.bid_price
    return offer.get("min_bid") or args.max_price


def log_order(args: argparse.Namespace, instance_id: int, offer: dict) -> None:
    """Append one JSON line per rented order to ORDER_LOG_FILE."""
    record = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "order_id": offer.get("id"),
        "instance_id": instance_id,
        "machine_id": offer.get("machine_id"),
        "host_id": offer.get("host_id"),
        "cost_per_hour": offer.get("dph_total"),
        "type": args.offer_type,
        "bid_price": bid_price(args, offer),
        "gpu": offer.get("gpu_name"),
        "num_gpus": offer.get("num_gpus"),
        "gpu_ram_mb": offer.get("gpu_ram"),
        "cpu": offer.get("cpu_name"),
        "cpu_cores": offer.get("cpu_cores_effective"),
        "cpu_ram_mb": offer.get("cpu_ram"),
        "disk_gb": args.disk,
        "location": offer.get("geolocation"),
        "reliability": offer.get("reliability"),
        "inet_down_mbps": offer.get("inet_down"),
        "inet_up_mbps": offer.get("inet_up"),
        "template_hash": args.template_hash,
        "label": args.label,
    }
    try:
        with open(ORDER_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as exc:  # never lose the instance over a logging problem
        log.warning("could not write %s: %s", ORDER_LOG_FILE, exc)


def error_text(exc: Exception) -> str:
    """Include the API's response body, which carries the actual reason."""
    body = getattr(getattr(exc, "response", None), "text", "") or ""
    return f"{exc} {body.strip()[:300]}".strip()


# --- Pre-flight ---------------------------------------------------------------


def check_account(vast: VastAI) -> None:
    try:
        user = vast.show_user()
    except Exception as exc:
        raise OrderError(
            f"cannot authenticate with Vast.ai ({error_text(exc)}). "
            "Run `vastai set api-key <KEY>` or set VAST_API_KEY."
        ) from exc
    credit = user.get("credit")
    log.info("account %s, credit $%s", user.get("email") or user.get("id"), credit)
    if isinstance(credit, (int, float)) and credit <= 0:
        raise OrderError("account has no credit; add funds at https://cloud.vast.ai/billing/")


def ensure_ssh_key(vast: VastAI, key_path: Path) -> None:
    """Vast only injects keys that are on the account before the instance is created."""
    if not shutil.which("ssh"):
        raise OrderError("`ssh` not found in PATH")
    pub_path = Path(f"{key_path}.pub")
    if not key_path.is_file() or not pub_path.is_file():
        raise OrderError(f"SSH key pair not found: {key_path} / {pub_path}")
    pub = pub_path.read_text().strip()
    fields = pub.split()
    if len(fields) < 2:
        raise OrderError(f"{pub_path} is not a valid SSH public key")
    key_body = fields[1]

    registered = vast.show_ssh_keys()
    if isinstance(registered, dict):
        registered = registered.get("ssh_keys") or registered.get("keys") or []
    if any(key_body in str(k.get("public_key", "")) for k in registered if isinstance(k, dict)):
        log.info("SSH key %s already registered with Vast", pub_path)
        return
    vast.create_ssh_key(pub)
    log.info("registered SSH key %s with Vast", pub_path)


def check_port_free(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError as exc:
            raise OrderError(
                f"local port {port} is already in use ({exc}); "
                "stop what is listening there or pass --tunnel-port"
            ) from exc


def pick_template(vast: VastAI) -> str:
    """List the account's own templates and let the user choose one; returns its hash."""
    if not sys.stdin.isatty():
        raise OrderError("no template given: pass --template-hash or set DEFAULT_TEMPLATE_HASH")
    try:
        user_id = vast.show_user().get("id")
        templates = vast.search_templates(f"creator_id={user_id}")
    except Exception as exc:
        raise OrderError(f"could not fetch your templates: {error_text(exc)}") from exc
    templates = [t for t in templates if t.get("hash_id")]
    if not templates:
        raise OrderError("you have no templates; create one at https://cloud.vast.ai/templates/")
    templates.sort(key=lambda t: (t.get("name") or "").lower())
    for i, t in enumerate(templates, 1):
        image = f"{t.get('image')}:{t.get('tag') or t.get('default_tag') or 'latest'}"
        print(f"{i:3d}) {t.get('name') or '(unnamed)'}  [{image}]  {t['hash_id']}", file=sys.stderr)
    while True:
        try:
            answer = input(f"Select template [1-{len(templates)}]: ").strip()
        except EOFError:
            raise OrderError("no template selected") from None
        if answer.isdigit() and 1 <= int(answer) <= len(templates):
            chosen = templates[int(answer) - 1]
            log.info("using template %r (%s)", chosen.get("name"), chosen["hash_id"])
            return chosen["hash_id"]


# --- Search / rent / boot -----------------------------------------------------


def find_and_rent(vast: VastAI, args: argparse.Namespace, query: str,
                  skip_machines: set, blacklist: Blacklist,
                  deadline: float | None) -> tuple[int, dict] | None:
    """Poll until an offer is rented. Returns (instance_id, offer), or None on timeout/dry-run."""
    while True:
        try:
            offers = vast.search_offers(
                query=query, type=VAST_TYPES[args.offer_type], order=args.order,
                limit=OFFERS_PER_SEARCH + len(skip_machines) + len(blacklist), storage=args.disk,
            )
        except Exception as exc:  # keep polling through transient API/network errors
            log.warning("search failed: %s", error_text(exc))
            offers = []

        candidates = [
            o for o in offers
            if offer_matches(o, args) and o.get("machine_id") not in skip_machines
            and not blacklist.blocks(o)
        ]
        for offer in candidates:
            log.info("found %s", describe_offer(offer))
            if args.dry_run:
                print(f"[dry-run] would rent {describe_offer(offer)}")
                return None
            # An interruptible instance is created by naming a bid price.
            price = bid_price(args, offer)
            bid = {"price": price} if price else {}
            if price:
                log.info("bidding $%.3f/hr", price)
            try:
                resp = vast.create_instance(
                    offer["id"], template_hash=args.template_hash, disk=args.disk,
                    label=args.label, cancel_unavail=True, **bid,
                )
            except Exception as exc:  # usually: someone else took the offer first
                log.warning("could not rent offer %s: %s", offer["id"], error_text(exc))
                continue
            instance_id = resp.get("new_contract")
            if resp.get("success") and instance_id:
                log.info("rented instance %s", instance_id)
                log_order(args, instance_id, offer)
                return instance_id, offer
            log.warning("unexpected response renting offer %s: %s", offer["id"], resp)

        if deadline is not None and time.monotonic() >= deadline:
            return None
        if not candidates:
            log.info("no matching offer; retrying in %gs", args.interval)
        time.sleep(args.interval)


def wait_until_running(vast: VastAI, instance_id: int, timeout: float,
                       keys: KeyWatcher) -> dict | None:
    """Return the instance dict once running, or None if it will not come up."""
    deadline = time.monotonic() + timeout
    last = object()
    while time.monotonic() < deadline:
        try:
            inst = vast.show_instance(instance_id) or {}
        except Exception as exc:
            log.warning("status check failed: %s", error_text(exc))
            inst = {}
        else:
            status = inst.get("actual_status")
            if status != last:
                log.info("instance %s status: %s %s", instance_id, status,
                         inst.get("status_msg") or "")
                last = status
            if status == "running":
                return inst
            if status in DEAD_STATUSES:
                return None
        keys.sleep(BOOT_POLL_SECONDS)
    log.warning("instance %s did not reach 'running' within %gs", instance_id, timeout)
    return None


def destroy(vast: VastAI, instance_id: int) -> None:
    try:
        vast.destroy_instance(instance_id)
        log.info("destroyed instance %s", instance_id)
    except Exception as exc:
        log.error("FAILED to destroy instance %s (%s) -- destroy it manually: "
                  "vastai destroy instance %s -y", instance_id, error_text(exc), instance_id)


# --- SSH ----------------------------------------------------------------------


def ssh_endpoint(inst: dict) -> tuple[str, int] | None:
    """Prefer the direct port mapping, fall back to the Vast SSH proxy (same as `vastai ssh-url`)."""
    try:
        direct = (inst.get("ports") or {}).get("22/tcp")
        if direct:
            return inst["public_ipaddr"].strip(), int(direct[0]["HostPort"])
        port = int(inst["ssh_port"])
        if "jupyter" in (inst.get("image_runtype") or ""):
            port += 1
        return inst["ssh_host"], port
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def ssh_base(key_path: Path, host: str, port: int) -> list[str]:
    return [
        "ssh", "-i", str(key_path), "-p", str(port),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "BatchMode=yes",
        f"root@{host}",
    ]


def wait_for_ssh(vast: VastAI, instance_id: int, inst: dict, key_path: Path,
                 keys: KeyWatcher) -> tuple[str, int]:
    """sshd comes up a little after 'running'; the port mapping may also appear late."""
    deadline = time.monotonic() + SSH_READY_TIMEOUT
    while True:
        keys.check()
        endpoint = ssh_endpoint(inst)
        if endpoint:
            probe = subprocess.run(
                ssh_base(key_path, *endpoint) + ["true"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True,
            )
            if probe.returncode == 0:
                log.info("SSH ready at root@%s port %s", *endpoint)
                return endpoint
            reason = probe.stderr.strip().splitlines()[-1:] or ["no output"]
        else:
            reason = ["no SSH endpoint reported yet"]
        if time.monotonic() >= deadline:
            raise OrderError(f"SSH not reachable after {SSH_READY_TIMEOUT}s: {reason[0]}")
        log.info("waiting for SSH (%s)", reason[0])
        keys.sleep(SSH_RETRY_SECONDS)
        try:
            inst = vast.show_instance(instance_id) or inst
        except Exception as exc:
            log.warning("status check failed: %s", error_text(exc))


def run_remote(key_path: Path, endpoint: tuple[str, int], command: str, keys: KeyWatcher) -> int:
    if command.lstrip().startswith("ollama"):
        # onstart launches the server in the background; don't race it.
        command = f"until ollama list >/dev/null 2>&1; do sleep 2; done; {command}"
    log.info("running on instance: %s", command)
    # No ssh TTY: the key watcher owns the terminal's stdin.
    return keys.run(ssh_base(key_path, *endpoint) + [command])


def hold_tunnel(vast: VastAI, instance_id: int, key_path: Path,
                endpoint: tuple[str, int], port: int, keys: KeyWatcher) -> None:
    """Keep `ssh -L` up until interrupted, reconnecting while the instance is running."""
    cmd = ssh_base(key_path, *endpoint)
    cmd[1:1] = ["-N", "-L", f"127.0.0.1:{port}:localhost:{port}",
                "-o", "ExitOnForwardFailure=yes"]
    while True:
        log.info("tunnel open: http://localhost:%s -> instance %s (Ctrl-C to close)",
                 port, instance_id)
        rc = keys.run(cmd)
        log.warning("tunnel closed (ssh exit code %s)", rc)
        keys.sleep(SSH_RETRY_SECONDS)
        try:
            inst = vast.show_instance(instance_id) or {}
        except Exception as exc:
            log.warning("status check failed: %s", error_text(exc))
            continue
        if inst.get("actual_status") != "running":
            raise OrderError(
                f"instance {instance_id} is no longer running "
                f"(status: {inst.get('actual_status')}); tunnel not restored"
            )
        endpoint = ssh_endpoint(inst) or endpoint
        log.info("reconnecting tunnel")


# --- Main ---------------------------------------------------------------------


def run(vast: VastAI, args: argparse.Namespace, state: dict,
        keys: KeyWatcher, blacklist: Blacklist) -> int:
    check_account(vast)
    if not args.dry_run and args.use_ssh:
        ensure_ssh_key(vast, args.ssh_key)
        if args.tunnel_port:
            check_port_free(args.tunnel_port)

    query = build_query(args)
    log.info("looking for: %s (template %s, %g GB disk)",
             query, args.template_hash, args.disk)
    deadline = time.monotonic() + args.timeout if args.timeout > 0 else None
    skip_machines: set = set()

    while True:
        rented = find_and_rent(vast, args, query, skip_machines, blacklist, deadline)
        if rented is None:
            if args.dry_run:
                return 0
            log.error("no matching offer within %gs", args.timeout)
            return 2
        instance_id, offer = rented
        state["instance_id"] = instance_id
        keys.pressed.clear()  # ignore keys typed before there was an order
        try:
            inst = wait_until_running(vast, instance_id, args.boot_timeout, keys)
            if inst is None:
                log.warning("instance %s failed to start; destroying it and looking again",
                            instance_id)
                skip_machines.add(offer.get("machine_id"))
            else:
                return use_instance(vast, args, instance_id, offer, inst, keys)
        except Blacklisted:
            blacklist.add(offer)
            log.warning("blacklisted order %s (machine %s); cancelling it and searching again",
                        offer.get("id"), offer.get("machine_id"))
        destroy(vast, instance_id)
        state["instance_id"] = None


def use_instance(vast: VastAI, args: argparse.Namespace, instance_id: int, offer: dict,
                 inst: dict, keys: KeyWatcher) -> int:
    summary = (f"instance {instance_id} running: {offer.get('num_gpus')}x "
               f"{offer.get('gpu_name')} ${offer.get('dph_total', 0):.3f}/hr")
    if not args.use_ssh:
        print(f"{summary} -- open it from https://cloud.vast.ai/instances/", flush=True)
        return 0

    endpoint = wait_for_ssh(vast, instance_id, inst, args.ssh_key, keys)
    print(f"{summary} -- ssh -i {args.ssh_key} -p {endpoint[1]} root@{endpoint[0]}", flush=True)

    if args.remote_cmd.strip():
        rc = run_remote(args.ssh_key, endpoint, args.remote_cmd, keys)
        if rc != 0:
            log.error("remote command failed with exit code %s", rc)
            return 1
        log.info("remote command finished")

    if args.tunnel_port:
        hold_tunnel(vast, instance_id, args.ssh_key, endpoint, args.tunnel_port, keys)
    return 0


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        vast = VastAI(api_key=args.api_key)
    except Exception as exc:
        log.error("cannot create Vast client: %s. Run `vastai set api-key <KEY>` "
                  "or set VAST_API_KEY.", exc)
        return 1

    if not args.template_hash:
        try:
            args.template_hash = pick_template(vast)
        except (OrderError, KeyboardInterrupt) as exc:
            log.error("%s", exc or "interrupted")
            return 1

    state: dict = {"instance_id": None}
    blacklist = Blacklist(BLACKLIST_FILE)
    try:
        with KeyWatcher(args.blacklist_key) as keys:
            code = run(vast, args, state, keys, blacklist)
    except OrderError as exc:
        log.error("%s", exc)
        code = 1
    except KeyboardInterrupt:
        log.info("interrupted")
        code = 130

    instance_id = state["instance_id"]
    if instance_id:
        if args.destroy_on_exit:
            destroy(vast, instance_id)
        else:
            log.warning("instance %s is STILL RUNNING and billing. Destroy it with: "
                        "vastai destroy instance %s -y", instance_id, instance_id)
    return code


if __name__ == "__main__":
    sys.exit(main())
