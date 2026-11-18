# 187 Ride or Die PS2 network protocol

The opt-in `rod` adapter now implements bootstrap, shared authentication, the
parent lobby, product-wide discovery, protected player-hosted rooms, membership,
player data, settings updates, and match start/finish acknowledgments. It uses
the existing GS router and NAT service. Retail testing has verified account
creation/sign-in, parent-lobby entry, discovery, News session continuity, NAT
setup, room creation, a direct two-PC room join, and a two-player room join
through the opt-in forced relay after the NAT self-view fix.
**Race gameplay and reliable retry/rejoin remain unverified.** The adapter stays
experimental and does not change the default enabled games.

## Retail validation and remaining checks

On 2026-11-17, two Linux PCs using stock PCSX2 Sockets networking reached the
same room after restarting the joining game, with the other PC hosting. The
server build had not changed between the failed and successful attempts.
Earlier attempts in both host/guest directions reached room membership but
left after about 15 seconds. Native state inspection showed connection state 5
waiting for session completion. A same-address flag remained set after switching
roles; a fresh initialization clears it. That is a possible contributor, not an
established root cause of every failed attempt.

The local host also had another service occupying UDP 3658, so PCSX2 used a
mapped port discovered through NAT. A successful join still used a mapped port;
the port conflict alone does not explain the earlier failures. Packet-header
observations established bidirectional direct UDP, not a relay fallback. That retail run used no
relay. The later opt-in forced relay described below also reached a two-player
room in a retail test. Its full race gameplay remains unverified. Keep payload
captures and native diagnostics private.

Next checks are a complete race, another race, guest leave/rejoin without a game
restart, and both hosting directions from fresh starts. Until those pass, a
room join must not be reported as verified race gameplay or a resolved retry bug.

## Disc identity and reproducible analysis

Owned-disc input: USA English/French/Spanish, `SLUS-21116`. `SYSTEM.CNF` boots
`SLUS_211.16` directly, version 1.03, NTSC. The main file is an ELF32
little-endian MIPS executable (4,808,280 bytes), entry `0x00100008`.

| Input | SHA-256 |
| --- | --- |
| `SLUS_211.16` | `7dc688177a63f8f935c9c13af8be583f1326324c3992cd1af52da110a057a70b` |
| `SYSTEM.CNF` | `4907852c267456d5e58b966247f8a4d7884ec786fddcb63438caef8c87fbc05f` |
| `SCEADNAS.BIN` | `e2d580f4c3ef927cdfdf72734a9226bf0d59ba5b3cac21d424af8739063a0662` |
| `MODULES/DNAS280.IMG` | `15a44eec943a6f00a06274e9e3ae37741e3bd080ee653c8589482abc3483b62a` |

```sh
python tools/extract_187.py '/path/to/discs/187 - Ride or Die (USA) (En,Fr,Es).iso'
"$GHIDRA_HOME/support/analyzeHeadless" work/187/ghidra-project Ride187 \
  -import work/187/disc/SLUS_211.16 -processor r5900:LE:32:default \
  -scriptPath ghidra -postScript Inspect.java function 003606b0 00360ed0 00368dc0
```

Ghidra 12.1.4 with the R5900/Emotion Engine extension completed analysis of the
plain ELF; no GR2 `.SOZ` extraction is needed. The load segment starts at file
offset `0x100`, virtual `0x00100000`; for file-backed bytes in that segment,
virtual address = file offset + `0x000fff00`. GP is `0x0059c9f0`.
Addresses below refer only to this executable/hash. R5900 argument registers
and tail-call wrappers can hide arguments in decompiler output; the creation
and connection arguments were checked against the instructions as well.

Keep extracted files, Ghidra projects, decompilation and captures under ignored
`work/187/`. The public tree contains original Python code, synthetic fixtures,
hashes and paraphrased protocol notes, with no retail code or assets.

## Bootstrap and accounts

| Stage | Native evidence | Implementation |
| --- | --- | --- |
| Identity | `RODPS2US` at `0x00528ae0`, `0x00529c30`, `0x00532970`; SDK `RODPS21.0` at `0x00529c08`, `0x00532980`; game version `1.0` at `0x00532968` | `rod` profile, ports 30040/30041/30042 |
| Bootstrap | `0x00355fc0` passes product through `0x00360cc0`; `0x003606b0` resolves `gsconnect.ubisoft.com`, TCP 80, `/gsinit.php?dp=%s` | Shared HTTP product dispatch |
| Bootstrap parsing | `0x00360ed0` loops indices 0–9; `0x00360500` searches key substrings and copies through CR/LF | Sectionless lowercase router/NAT keys, CRLF, index zero |
| Account creation | `0x003b4330` → `0x003648b0` → `0x00367810`: seven strings, encrypted type `0x01` through `0x0038fe50` | Existing shared-account creation |
| Sign-in | `0x003b4430` → `0x00364870` → `0x00367700`; serializer `0x00368dc0`: username, password, SDK version, one-byte flag, encrypted `0x66` | Existing GS RSA/Blowfish and account login |
| Redirect/wait | Success `0x003b4010` requests redirect; `0x003b4100` reconnects using returned address/port and signs in with username | Existing `0x5d`/`0x4d` handoff |
| Directory | `0x003b5970` → `0x00364b00` → `0x003681d0` → `0x00370450`: `0xd1 ["21", ["RODPS2US"]]` | Operation-21 success and parent folder |

Static analysis finds separate registration and login branches in
`0x0033f580`, with Create/Register localization keys in the registration branch.
The initial retail attempt showed only Sign In; after entering and confirming
a password in Account Info, the tester completed registration and sign-in. Observed Sign In
attempts send `0x66`, not account creation (`0x01`), and do not create a missing
account. Saved memory-card profiles are separate from the server database, and
local test accounts are separate from hosted accounts. Server username lookup
ignores case.

The Account Info screenshot shows the empty-password placeholder (`-----`),
not a saved masked password. `0x0033ef50` renders nonempty passwords as `*`
characters and uses that placeholder only for an empty buffer. Menu state 5
in `0x0033f580` hides the Register control while that buffer is empty. Circle
on the password field starts entry (state 0x11), followed by confirmation
(state 0x13); `0x00341060` then returns to state 5. The registration control is
available after entering and confirming a password in a new profile. The tester
confirmed successful entry after this step; the server observed account creation
`0x01`, sign-in `0x66`, redirect/wait login, directory and parent-lobby entry,
12-byte player publication, and completed NAT discovery.

Native failure decoder `0x00368a20` requires the original one-byte operation at
index 0 and a nested list at index 1 containing a four-byte little-endian error.
The 187 adapter returns `0x27 [0x66, [4]]` for an unknown account and
`0x27 [0x66, [15]]` for a wrong password; callback `0x003b4010` and the menu map
these to InvalidName and InvalidPassword. A duplicate create returns
`0x27 [0x01, [1]]` (AccountExist, `0x003b3f00`). Here the bracketed errors are
four-byte binary values, not GS decimal strings. Other titles retain their
existing failure behavior. Failed requests grant no authentication or handoff
and never create or overwrite an account.

The first retail News attempt sent `0xde [language]` on the authenticated
wait connection and disconnected because the profile had no notice handler.
Request sender `0x00367820` and result reader `0x0036a800` confirm the shared
notice contract: `0x26 [0xde, [text1, text2]]`, with each text copied into a
0x201-byte buffer. The profile now supplies two service messages, configurable
through `TUBISOFT_ROD_NEWS_1` and `_2` (printable ASCII, at most 512 bytes each).
Menu `0x0043c3b0` concatenates the two messages for its paged News display.
A real-socket regression reproduces the previous EOF, then verifies News replies,
heartbeats on both wait and lobby connections, and directory refresh afterward.
The next retail run received the News reply and kept both lobby connections alive.

Router entries require `routeripN`, `routerportN`, `routerlauncherportN`;
NAT entries require `natserveripN`, `natserverportN`. No `[Servers]` section is
needed. No `ladderquery` string was found and no ranking proxy is enabled.
Rankings, result scoring and friend-location joining are outside this adapter;
their absence from the traced startup is not proof of every possible menu path.

## Discovery and room records

`0x0036fd90` reads the common 14-field parent folder; `0x0036fab0` reads the
20-field game record. Both contain product at index 9 and binary data at 10;
game records add limits/counts, versions and two address strings. The adapter
uses a local parent with ID 1 and capacity 100; that capacity is an operator
choice, not an original-server capture. Game IDs are negative, as the game's
callbacks require (`0x003b5280`, `0x003b55a0`, `0x003b5070`).

Operation 109 (`0x003b6660` → `0x00364d00` → `0x00368400`) carries exactly
`["RODPS2US"]` on the wait connection. Its selector is stored at
`0x005955ec`. The caller clears its folder count before sending. There is no
109 completion case in dispatcher `0x00367ca0`; returning a fabricated success
would not complete a native handler. Instead the server sends operation 53,
flag `0x100`, with the parent followed by active matching game records in the
child list. That re-adds the cleared parent through `0x0037d000` →
`0x00372800` → callback slot `0x24` → `0x00387a00` → `0x003b48b0`.

Catalog refresh retains membership and does not replay member joins. Parent
join 23 sends its success, current rooms, existing members and a self-member
seed. `0x003b4b20` clears the game's old room/member caches on parent-join
success. The seed matters: NAT completion looks up the client's own member
before it can publish data (`0x0043eda0` → `0x003b6580`). Operation 9 can
refresh room records; it retains members rather than adding peers again.

## Creation, joins and peer membership

| Operation | Request/response contract | Native evidence |
| --- | --- | --- |
| 23, parent join | Request `[parent, password, numeric flags]`; success `[parent, text]` | Sender `0x003707a0`, reader `0x00372fe0` |
| 12, create | Eleven fields: parent, name, product, type, player limit, spectator limit, binary session info, password, game version, SDK version, binary extra info | `0x0041e580` → `0x00364b40` → `0x003682b0` → `0x00370e90` |
| 12, completion | Room ID, text, parent ID; creator immediately sends operation 24 | Reader `0x00374930`, callback `0x0041ded0` |
| 24, game join | Room ID, password, numeric flags, boolean as number, game version; success ID/text | Sender `0x00368290`, reader `0x00373590`, callback `0x0041df90` |
| 24, wrong password | Failure `[11, room ID, text]` | Reader failure offset; callback `0x0041df90` maps reason 11 to state 9 |
| 24, full room | Failure `[7, room ID, text]`; lobby connection remains usable | Callback `0x0041df90` maps reason 7 to unavailable/full state 8 |
| 50, member | Name, numeric flag, group ID, public/local addresses, binary player data, numeric u16 | `0x0037ec00` → `0x003b5280` |
| 51, departure | Name and group ID; server ID comes from connection context | `0x003b55a0` clears negative room membership or removes positive lobby membership |
| 66, player update | Name and binary player data | `0x003b5770` updates an existing record |
| 55, room removal | Room ID | `0x003b5070` removes the cached room and resets the joined client's state |

The native create caller uses type 5, zero spectators, game version `1.0` and
SDK `RODPS21.0`. The service validates those identities and sizes, bounds text
and additional data, and limits the advertised player count to 1–64 as a local
resource bound. This is not a claim that the retail menu offers 64 players.
Game joins enforce the host's configured room limit, including the owner's
reserved slot before self-join. Existing members can retry without consuming
another slot. A full-room refusal happens before changing the player's current
membership or host binding; leaving releases a slot for the next admission.
This check applies to hosted match rooms, separately from the parent directory.
Passwords are held only with the transient room and are never logged.
Protected rooms advertise flag `0x01`: browser `0x00346df0` calls
`0x003b64f0`, whose predicate is the room flags at entry `+0x30` AND 1.
Without it the retail browser does not request a password. Operation-31 flag
updates preserve this protection bit.

The creator receives its room listing before self-joining. On join success the
client receives its own room membership, then the authoritative room info.
This order matters: `0x003b5280` checks self membership before opening peers,
and increments the cached room count; `0x003b4e60` then sets the count from the
room record. The guest callback `0x0041df90` already opens cached peers via
`0x003b6460`, so the server does not replay those peers after joining.

Repeating a join notification would call `0x0041ea10` again and allocate a
second local peer entry. Consequently changed player data uses operation 66,
and catalog/group refresh keeps known member state. A self departure clears
the negative ID through operation 51 and returns the self record to the parent.
These ordering rules are profile capabilities; R6/BiA's existing behavior is
retained.

## Session data, NAT and gameplay endpoints

The adapter preserves the game's **36-byte** session blob. `0x0041e580` copies
`0x24` bytes, and `0x003b4bf0`/`0x003b4e60` reject a different size. The
fields needed by the service are the player limit at `+0x1c` and mapped NAT
port at `+0x20` (u16). Other settings remain client-owned opaque data.

The **12-byte** player record has different offsets from BiA:

| Offset | Field | Evidence |
| --- | --- | --- |
| `+0` | Opaque player state byte | Preserved |
| `+1` | NAT endpoint available flag | `0x003b6580`, `0x0041ea10` |
| `+2` | Two opaque bytes | Preserved |
| `+4` | IPv4 host-order u32, stored little-endian | `0x003b6580`, NAT completion `0x0043eda0` |
| `+8` | Port, little-endian u16 | `0x003b6580`, `0x0041ea10` |
| `+10` | Two opaque bytes | Preserved |

Both parent and in-room operation 42 require exactly 12 bytes before storage or
forwarding. This also bounds `0x003b5770`, which copies the supplied byte count
into that fixed record. Empty data is used only for the initial self seed.

The previously unidentified port **6779 is the NAT client's requested local
control port**, not a new remote service. Bootstrap NAT entries reach
`0x0043ebe0`; `0x0043e740` passes their address/port structure and local 6779 to
`0x0043cc50`. `0x0043dde0` stores the remote port separately at `+0x8e` and
local port at `+0x90`. The SRP connection goes through `0x003997c0` →
`0x003ad880` → socket creation/binding `0x003a5f10`/`0x003a7320`.

`0x0043e340` sends the existing `0xdd [1, [request ID, username, product, SDK]]`
NAT protocol. `0x0043e040` dispatches subtype 2 (token/probe port) and 3 (observed
endpoint). `0x0043dc30` converts the token with `htonl` before sending its four
bytes through the game socket; `0x0043dc90` delivers the endpoint result.
The profile requests **big-endian address numbers** from the shared NAT helper,
matching the host-order word stored in the player record.

Actual race traffic goes directly between clients. Guest connector
`0x0042dbc0` takes the room's public address at entry `+0x50`, or local address
at `+0x60` for the same-address path. It uses the session NAT port at entry
`+0x94` (session `+0x20`), falling back to **3658** when zero or on the local
path. Peer opening `0x0041ea10` uses the member's mapped endpoint when available
and otherwise the GS address with port 3658. Direct mode uses no gameplay relay or dedicated worker. The optional forced
relay below replaces the advertised game endpoints; remote reachability and
retail relay gameplay still require testing.

## Optional forced UDP relay

`TUBISOFT_ROD_RELAY_MODE=relay` opts 187 into a native opaque UDP relay.
The default is `direct`. The first relay implementation deliberately forces
relay use for validation; it does not promise automatic direct-first fallback.
No emulator modification or additional client protocol is required.

The normal GS NAT token exchange proves each real game socket's address/port.
The relay returns a pending virtual endpoint only for an existing signed-in
187 parent member with the matching product, SDK, name and observed source IP.
That player's authenticated TCP player-data publication must confirm the exact
returned endpoint before any forwarding is allowed. A second NAT request from
a shared public IP cannot replace an admitted mapping without that confirmation.

A relay socket represents one player to the others. A datagram addressed to
player B's relay port, received from player A's proved game endpoint, is sent to
B from A's relay port. Each receiver therefore sees the endpoint it opened from
GS member data. Native game bytes, including zero-length packets, remain intact.
Both players must still be connected, share the same active negative room ID,
and belong to that room's authenticated membership set. Knowing a protected
room's advertisement grants no forwarding rights; its normal password check
must succeed first.

The relay holds up to eight initial datagrams per sender for at most five
seconds until the destination has sent to that sender's relay port. The NAT
probe opens a path to the discovery server; it does not open the peer relay
path in PCSX2's sockets backend or an endpoint-dependent firewall. The first
return packet is forwarded, then the held packets are released in order,
with their original bytes and source endpoint. Membership changes and mapping
retirement clear these paths and queues. No gameplay messages are fabricated.

A retail trace showed two reliable join requests preceding the host's first
native permit by about two seconds. Forwarding was bidirectional, but the join
did not complete. With the startup queue, the next retail run exchanged a
successful native join response, in-session publication, confirmation and
bidirectional keepalives. It then timed out in the menu. A further trace showed
the host's native `0x87` announcement replacing its relay endpoint with its
direct local address and port 3658. After the self-view fix below, a fresh
two-player retail test successfully entered the room. The passive trace showed
the native session and application messages traversing the relay, without that
direct-address announcement. This verifies that room join, not a completed race
or repeated rejoin.

Other players' GS public/local addresses, room addresses, the 12-byte player
NAT endpoint and the session port at offset `0x20` name the relay. Each client
retains its real self-member GS address and its real IP in its own SDK NAT
record, while keeping its allocated relay port. This prevents both the browser
and the post-join NAT announcement from mistaking two relay mappings for the
same home network and overriding the negotiated endpoint with local-IP:3658.
Other players still receive the canonical relay NAT record. An authenticated
client may republish its self view only for an already-confirmed lease; the
server normalizes it back to the canonical relay record before storage and
broadcast. Unconfirmed mappings still require the exact token-proved endpoint.
Real addresses remain the internal ownership/admission identity. Settings
updates cannot replace the owner's advertised relay endpoint.

The single-address relay targets two-player rooms. Endpoint announcements
between multiple other guests can still compare equal relay IPs and force port
3658. Larger relay rooms are unsupported pending distinct relay addresses or
another solution to that native topology update.

The default pool is UDP 30400–30431, with at most 128 configured ports. Each
pending/confirmed mapping occupies one socket. Pending claims expire after
30 seconds, disconnects release mappings, and freed ports have a five-second
reuse delay. Confirmed waiting-room mappings survive NAT-control FIN and remain
bounded by live signed-in members. Packets are limited to 2048 bytes and 600 per
second per sender. Logs contain port/counter summaries, not payloads or names.

```sh
TUBISOFT_GAMES=rod TUBISOFT_ROD_RELAY_MODE=relay docker compose \
  -f compose.yaml -f deploy/compose.rod-relay.yaml \
  up -d --build
```

Publish the whole configured UDP pool from the same public IPv4 address named
by `TUBISOFT_ROD_RELAY_ADVERTISE_IP`. Keep that address different from the
clients' observed public addresses; this includes a game client on the relay
machine. For a local same-machine test, use a separate reachable LAN address
alias and host networking so Docker hairpin/source translation cannot change
the relay's source address. `TUBISOFT_ROD_RELAY_BIND_IP` can bind that alias.
A Docker deployment for remote clients may use the published-port overlay.
If the relay address equals the client's observed address, admission declines
rather than sending it down the native fixed-port path.

A real-socket synthetic check performs RSA/Blowfish sign-in, parent join, native
NAT SYN/token/probe, TCP mapping confirmation, protected-room advertisement and
join, and bidirectional UDP forwarding with the expected relay source ports.
It also verifies that traffic is blocked before room join and after leave.
Additional checks cover unconfirmed claims, shared NAT, cross-room isolation,
spoofed endpoints, bounded capacity/rate/packet size, expiry and disconnects.
The socket test also verifies delayed peer readiness and release of an early
join packet; queue bounds, expiry and membership cleanup have separate coverage.
These checks need no unlocked retail content. They verify the server transport;
a completed retail race, another round and repeated joins remain separate tests.

## Room updates and match transitions

- Operation 31 (`0x00371770`) uses a field-selection mask, optional flag values,
  then selected limits/name/session/extra data. The adapter parses the mask,
  validates the entire request before mutation, and pushes operation 53 with
  flag `0x40` so clients update their existing room record.
- Start preparation `0x0041ef10` sends operation 15. Its callback
  `0x0041e190` then sends operation 33 (binary data, port, local address), which
  also needs a room-ID success. Empty data/zero port/empty address are valid.
- Operation 17 (`0x00371170`) includes **three** fields: room ID, numeric state
  and numeric u8. The server acknowledges it and sends operation 62 with room
  ID/match ID (reader `0x00377f40`). Match IDs are transient; no rating formula
  or persistent results were invented.
- Operations 34/35/44/70 are one-way room reports. Operation 45 needs success
  with a room ID (`0x0037f900`); the lobby-server ID is obtained from SDK context.
- Game-start operation 56 is not emitted: this title gets its endpoint from
  the directory/session data, and its registered game callback is empty.

## Validation and first retail test

Synthetic checks use temporary accounts, actual RSA/Blowfish and loopback GS
sockets. They cover shared XIII/187 accounts and isolated handoffs; parent and
product catalogs; two members; protected creation, wrong-password retry and
join; self membership ordering and counts; operation-66 updates; settings
changes; both start-preparation acknowledgments; two match start/finish cycles;
room capacity under concurrent joins, owner reservation, existing-member retries
and refused switches that preserve a hosted room;
guest leave/refresh/rejoin; owner disconnect/removal; and rejection of malformed
player data before forwarding it. NAT fixtures verify the token transform and
address order. These checks establish server behavior, not a race simulation.

For a local test, set the advertised address for your client and opt in:

```sh
TUBISOFT_GAMES=xiii,rod docker compose config --quiet
TUBISOFT_GAMES=xiii,rod docker compose up -d --build
curl --noproxy '*' 'http://127.0.0.1:18080/gsinit.php?dp=RODPS2US'
PYTHONPATH=src:tests python -m unittest test_rod_profile test_rod_rooms -v
```

Use the existing platform DNS/DNAS service. Route `gsconnect.ubisoft.com` TCP 80
to this instance's bootstrap, router TCP 30040, and NAT UDP 30042. `compose.yaml`
also maps router UDP 30040 and launcher TCP 30041 for consistency; hosting uses
lobby operation 12 rather than a launcher registration. DNS/DNAS are separate
from this container; no second listener on shared ports 53 or 443 is needed.

The disc includes `DNAS280.IMG` and `SCEADNAS.BIN`. The existing platform uses a
captured-response lookup keyed by a request's game key/query type, not by GS
product. Its acceptance for this disc cannot be established from a successful
GS socket test. On the first retail run, record the DNAS lookup hit/miss and
client result privately, then confirm HTTP bootstrap, account login and lobby
join. Preserve the first unsupported request if any; do not guess an auth
response or publish captures.

Then use two unmodified clients to verify: create/browser/password join,
Ready/race, next race, guest leave/rejoin, host exit and refresh. Repeat across
two remote networks and behind the same public NAT. Check for duplicate peers,
counts and repeated member updates, mapped addresses/ports and timeouts. Only
retail acceptance should promote this profile beyond experimental status.
