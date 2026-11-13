"""Tubisoft beta service configured through TUBISOFT_* environment variables."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import hmac
import ipaddress
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
from types import SimpleNamespace

from . import accounts, bootstrap, hostalias, router
from .ladder import LadderStore
from . import results_api
from .access import AccessPolicy, networks
from .games.registry import bindings


class PrivacyFormatter(logging.Formatter):
    """Ephemeral peer identifiers by default; never persist a tracking key."""
    def __init__(self):
        super().__init__("%(asctime)s %(levelname)s %(message)s")
        self.key = secrets.token_bytes(32)

    def format(self, record):
        message = super().format(record)
        def replace(match):
            try:
                ipaddress.IPv4Address(match.group())
            except ValueError:
                return match.group()
            return "peer-" + hmac.new(self.key, match.group().encode(), hashlib.sha256).hexdigest()[:12]
        return re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", replace, message)


def ensure_key(path: Path) -> None:
    if path.exists():
        router.load_key(path)
        os.chmod(path, 0o600)
        return
    temporary = path.with_suffix(".generating")
    try:
        result = subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt",
                                 "rsa_keygen_bits:512", "-out", str(temporary)], capture_output=True)
        if result.returncode:
            raise RuntimeError("could not provision the legacy GS key")
        os.chmod(temporary, 0o600)
        router.load_key(temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def configuration(environment=None):
    env = os.environ if environment is None else environment
    address = str(ipaddress.IPv4Address(env["TUBISOFT_ADVERTISE_IP"]))
    if ipaddress.IPv4Address(address).is_unspecified:
        raise ValueError("advertised address cannot be a wildcard")
    allowed = env.get("TUBISOFT_ALLOWED_CIDRS", "0.0.0.0/0")
    networks(allowed)
    def port(name, default):
        value = int(env.get("TUBISOFT_" + name, default))
        if not 1 <= value <= 65535:
            raise ValueError("invalid service port")
        return value
    results_keyring=env.get('TUBISOFT_RESULTS_KEYRING')
    if bool(results_keyring)!=bool(env.get('TUBISOFT_RESULTS_PORT')):
        raise ValueError('Configure results port and private worker keyring together')
    return SimpleNamespace(
        games=bindings(env),
        native_rankings=env.get('TUBISOFT_NATIVE_RANKINGS','0')=='1',
        results_keyring=Path(results_keyring)if results_keyring else None,
        results_port=port('RESULTS_PORT',8081),
        bind=str(ipaddress.IPv4Address(env.get("TUBISOFT_BIND_IP", "0.0.0.0"))),
        advertise_ip=address, port=port("ROUTER_PORT", 30000), launcher_port=port("LAUNCHER_PORT", 30001),
        nat_port=port("NAT_PORT", 30002), http_port=port("HTTP_PORT", 8080),
        nat_allowed_cidr=allowed, access_policy=AccessPolicy(allowed), timeout=300,
        state=Path(env.get("TUBISOFT_STATE_DIR", "/state")),
        host_alias=hostalias.configure(env.get("TUBISOFT_HOST_ALIAS", "auto"), address),
        gr2_worker_state=Path(env['TUBISOFT_GR2_WORKER_STATE']) if env.get('TUBISOFT_GR2_WORKER_STATE') else None)


async def serve(args) -> None:
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.account_db = args.state / "accounts.sqlite3"
    args.private_key = args.state / "router-private.pem"
    accounts.initialize_database(args.account_db)
    args.ladder_store=LadderStore(args.account_db)
    ensure_key(args.private_key)
    # Accounts, RSA key and resource limits are process-wide. Each game's GS
    # listeners own its transient handoffs, NAT sessions and room registry.
    products = {}
    tasks = []
    if getattr(args,'results_keyring',None)is not None:
        grants=results_api.load_grants(args.results_keyring)
        tasks.append(asyncio.create_task(results_api.serve(args.ladder_store,grants,port=args.results_port)))
    for binding in args.games:
        profile = binding.profile
        worker_state = getattr(args, 'gr2_worker_state', None) if profile.slug == 'gr2' else None
        if worker_state is not None:
            profile = replace(profile, supports_rooms=True)
        game_args = SimpleNamespace(**(vars(args) | {
            "game_profile": profile, "port": binding.router_port,
            "launcher_port": binding.launcher_port, "nat_port": binding.nat_port,
            "worker_state": worker_state}))
        products[profile.product] = profile.bootstrap(
            args.advertise_ip, binding.router_port, binding.launcher_port,
            args.advertise_ip, binding.nat_port)
        logging.getLogger("ubisoft.service").info(
            "enabling game=%s product=%s discovery=%s rooms=%s ports=%s", profile.slug,
            profile.product, profile.supports_discovery or profile.supports_rooms, profile.supports_rooms,
            (binding.router_port, binding.launcher_port, binding.nat_port))
        tasks.append(asyncio.create_task(router.serve(game_args)))
    http = SimpleNamespace(bind=args.bind, port=args.http_port, products=products)
    tasks.append(asyncio.create_task(bootstrap.serve(http)))
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run(args) -> None:
    task = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    await serve(args)


def main() -> None:
    os.umask(0o077)
    handler = logging.StreamHandler()
    handler.setFormatter(PrivacyFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    try:
        args = configuration()
        asyncio.run(run(args))
    except (KeyError, ValueError) as exc:
        raise SystemExit("Invalid Tubisoft configuration; check advertised IPv4, CIDRs, game slugs and distinct ports.") from exc
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()
