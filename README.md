# Tubisoft Game Service

An experimental Ubisoft Game Services revival for PS2. The name is a play on Tubi and Ubisoft.

Two unmodified **XIII USA** clients (`SLUS-20677`, CRC `FCD97245`) have created accounts, signed in, discovered a room, joined the same LAN match, and played together. NAT discovery, host-side peer-opening notifications, score-list updates, and host-exit room removal have been exercised. This release packages that implementation for an **open beta**. Anyone can create a game account and connect; no tester approval or IP enrollment is required.

The common `ubisoft` package provides GS framing, crypto, shared SQLite accounts, lobby state, and NAT discovery. Title adapters live in `ubisoft.games`, with isolated per-game listeners and transient sessions. Existing accounts work across enabled games without copying credentials. See the [multi-game architecture and extension guide](docs/multi-game-architecture.md).

XIII remains enabled by default. Ghost Recon 2 is an opt-in experimental profile with shared accounts, native bootstrap, login, lobby discovery and empty ranking-table startup. A separate owned-disc gameplay worker can advertise a dedicated Assault room. An unmodified retail client has discovered that room, joined and reached player setup/Ready; movement, combat and round transitions still need multi-player testing. Persistent GR2 rankings are not implemented.

The independent Python gameplay worker, private data preparation tools and historical emulator research live in [gr2-ps2-dedicated-server](https://github.com/princebyyourlogic/gr2-ps2-dedicated-server). Tubisoft contains only shared services and the validated worker-directory interface. See [GR2 workers](docs/gr2-workers.md) and [GR2 protocol research](docs/ghost-recon-2-network.md).

## Run a beta instance

Requires Docker with Compose. The image is self-contained; no TSFP checkout, disc image, or existing lab image is needed.

```sh
cp .env.example .env
chmod 600 .env
# Edit the advertised IPv4 address in .env. Open access is the default.
docker compose up -d --build
docker compose ps
curl --noproxy '*' 'http://127.0.0.1:18080/gsinit.php?dp=XIIIPS2US'
```

The game requires `/gsinit.php?dp=XIIIPS2US` on **TCP 80**. The default deployment exposes bootstrap only at loopback port 18080 so it can share an existing web server; add the location from [deploy/nginx-bootstrap.conf](deploy/nginx-bootstrap.conf) to the appropriate HTTP server. On a standalone machine without an existing HTTP listener, set `TUBISOFT_HTTP_BIND=0.0.0.0` and `TUBISOFT_HTTP_PUBLISH_PORT=80`.

Use the existing platform DNS/DNAS services when sharing a TSFP host. They are not included in this repository. Redirect `gsconnect.ubisoft.com` to Tubisoft's reachable address. Do not start a second listener on shared ports 53 or 443.

See [deployment and backup instructions](docs/deployment.md) and the [tester guide](docs/local-test.md) before inviting testers.

## Persistence and privacy

Accounts are stored in `/state/accounts.sqlite3` in the `tubisoft-state` Docker volume. SQLite uses WAL, schema versioning, atomic unique names, and salted scrypt password hashes. The application does not store plaintext passwords, email addresses, or client IPs in the account database. Room membership and handoffs are transient and reset on restart.

The legacy router key is generated on first start in the same private volume and reused afterward. Neither the key nor the database belongs in Git. Logs use per-process pseudonymous peer identifiers rather than raw addresses. Tests create only synthetic accounts in temporary storage.

```sh
docker compose exec game-service tubisoft-admin backup \
  --database /state/accounts.sqlite3 --output /state/backups/accounts-YYYYMMDD.sqlite3
```

Copy backups to private storage outside the host. Never use `docker compose down -v` unless intentionally deleting the account database and key. [Backup and restore details](docs/deployment.md#backup-and-restore) include legacy database migration.

## Beta limits

- The original game requires obsolete RSA/Blowfish formats. Use a unique game-only password. Resource and authentication-attempt limits remain enabled; optional `TUBISOFT_ALLOWED_CIDRS` restrictions are available for private installations.
- Some handoffs still correlate by source IP and account name. Launcher login verifies its own account credentials and permits separate accounts sharing a public IP; simultaneous shared-NAT retail coverage and a complete session-token model remain follow-up work.
- XIII peer gameplay is direct UDP. GR2 uses a separate dedicated gameplay worker; an optional operator-managed relay can expose that worker. GR2 remote gameplay remains experimental. NAT discovery alone does not make every XIII host reachable.
- Invalid-login and duplicate-account failure replies, password-protected rooms, persistent rankings, and full SDK operation coverage remain incomplete. Failed operations can time out.
- Normal round-end readiness is handled by the game. Its disconnect UI can return to sign-in even when the GS connection remains alive. Long-duration sequence rollover and repeated-round testing remain beta work.

## Development

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
python tools/publication_audit.py
```

The CI workflow runs tests, audits every tracked publication file, and builds the container. It does **not** deploy to a VPS automatically. The publication audit reports locations and categories without printing matched secrets; it is one review layer, not a universal secret detector.

Robin provides optional AI review through GitHub Actions. Configure repository Actions secrets
`LLM_API_KEY` (your provider key), `LLM_BASE_URL` (`https://openrouter.ai/api/v1`), and
`LLM_MODEL` (`openai/gpt-6-luna` for GPT Luna). New non-draft PRs from this repository
are reviewed automatically; a maintainer can comment `/robin` to review a fork PR or
request another pass, or `/summary` for an overview. Reviews are advisory and do not
replace CI or gameplay checks. The workflow pins Robin's implementation and reads PR
files without checking out or executing PR code; pushes alone do not request another review.

The [protocol implementation map](docs/xiii-network-protocol.md), [workflow analysis](docs/xiii-online-workflow.md), [crypto notes](docs/ubisoft-crypto-research.md), and [disc identity notes](docs/xiii-ps2-network.md) distinguish observed behavior from remaining hypotheses. Tools and Ghidra scripts are included; retail code, extracted scripts, captures, memory cards, and generated analysis are excluded. To analyze your own disc:

```sh
python tools/extract_disc.py '/path/to/discs/XIII (USA).iso'
python tools/extract_gr2.py '/path/to/discs/Ghost Recon 2 (USA).iso'
```

For missing platform-auth responses, the [DNAS preservation workbench](docs/dnas-preservation.md) recovers protected GR2 library code and provides private input extraction, replay inventory, transport and comparison tools. It does not yet derive an accepted GR2 response or change the running DNAS service.

Original project code is licensed under **GPLv3**. Referenced MIT code retains its [third-party notice](THIRD_PARTY_NOTICES.md). No game assets, DNAS replays, credentials, or personal deployment configuration are distributed.

## Shared match results

The optional [raw-stat ladder and result ingestion](docs/shared-ladder.md) reuse
the account SQLite database across titles and modes. Gameplay workers submit
authenticated, idempotent round reports; open guest play remains unranked. This
feature is disabled by default and has not been deployed by this change.
