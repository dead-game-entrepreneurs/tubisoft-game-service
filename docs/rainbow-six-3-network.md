# Rainbow Six 3 USA (PS2): GS compatibility notes

Tom Clancy's Rainbow Six 3 for PS2 (`SLUS-20883`) links the same Ubisoft GS SDK
family as Ghost Recon 2. Its sign-in sequence, bootstrap dialect, ranking proxy
and lobby records match GR2 closely. Its game-hosting path instead resembles
XIII's launcher registration. The early sections are **static** evidence from
the owned disc's executables. Later sections also record retail runs: two
unmodified clients in PCSX2 against a Tubisoft instance.

Evidence labels follow [the GR2 notes](ghost-recon-2-network.md):
**Static** (traced instructions/data on this disc), **Capture** (a related
title's client traffic, used only for comparison) and **Pending**.

## Executables and reproducibility

`SLUS_208.83` is a 143 KB loader linked at `0x01400000`. Like GR2's loader, it
expands a `.SOZ` file: a little-endian expanded-size word followed by one zlib
stream. `MP.SOZ` expands to 5,593,216 bytes. Pointer voting against string
offsets gives a load address of `0x00100000`. Startup at `0x00100198`
clears BSS from `0x00655880` to `0x007c9800` and sets GP to `0x0065cf70`.
Use those values with Ghidra's `r5900:LE:32:default` language.

| Input | SHA-256 |
| --- | --- |
| `SLUS_208.83` | `85847d677495cb971c9673e1c0f8b7c7093dfd2eb38a5750f252e647c550628b` |
| `MP.SOZ` | `731aec681af456bc77393c03bf5d9d441d12a304ca17f29fc4785ca3226aea27` |
| Expanded `MP.SOZ` | `0f86683517934e17af5a0cfc642d2f7946a924a24b5714052a1c79f0ecab285b` |

```python
import struct, zlib
raw = open("MP.SOZ", "rb").read()
image = zlib.decompress(raw[4:])
assert len(image) == struct.unpack("<I", raw[:4])[0]
```

The image is unsymbolized. The debug name table at `0x00440670` maps
message types to SDK names. Its types match the existing XIII/GR2
numbering (`0x26` GSSUCCESS, `0x3a` STILLALIVE, `0x4d` LOGINWAITMODULE,
`0x5d` JOINWAITMODULE, `0xd1` LOBBY_MSG, `0xd2` LOBBYSERVERLOGIN).
Keep extracted images, captures and listings outside the repository.

## Identity and bootstrap

| Item | Evidence | Value |
| --- | --- | --- |
| Product (`dp`) | Setup `0x003ff3d0` → setter `0x003ffde0`; getter `0x003ffe60` is appended after `&dp=` by `0x00561da0` | `RAINBOWSIX3PS2US` |
| SDK/login version | Setter `0x003ffd50`; getter `0x003ffe30` is the third primary-login string (`0x00407120`) | `RSPS21.0` |
| Game version | Setter `0x003ffcf0` | `1.0` |

`0x00561da0` resolves `gsconnect.ubisoft.com`, connects to TCP 80 and sends:

```http
GET /gsinit.php?user=<user>&dp=RAINBOWSIX3PS2US HTTP/1.1
HOST: gsconnect.ubisoft.com

```

Reader `0x003ff9c0` enumerates routers (type 0), NAT servers (type 3) and
IRC servers (type 4) through `0x00469f20`. That lookup uses section name
GP−`0x7880` = `Servers`, with keys `RouterIP%d`, `RouterPort%d`,
`NATServerIP%d`, `NATServerPort%d`, `IRCIP%d` and `IRCPort%d`.
`RouterLauncherPort%d` is read separately and defaults to 40005. Success
requires a nonempty router list and NAT list. The bootstrap response is
therefore the same sectioned dialect as GR2:

```ini
[Servers]
RouterIP0=192.0.2.10
RouterPort0=30020
RouterLauncherPort0=30021
NATServerIP0=192.0.2.10
NATServerPort0=30022
```

The NAT branch compares the NAT hostname with `gsnat.ubisoft.com`, as GR2
does. Its address-conversion branch remains **Pending** a live callback.

## Sign-in sequence

The game's online state machine `0x00541c70` logs each stage. In order:
LOGIN → LOGIN LOBBY → LOGIN FRIEND → GET PERSONAL INFORMATION → LOGIN CHAT →
NAT → LADDER (conditional) → DEDICATE → DEDICATE LOBBY. This is the same
shape as GR2's sign-in.

| Stage | Native sender / reader | Wire shape | Tubisoft handler |
| --- | --- | --- | --- |
| Key exchange | `0x00446610`, `0x00446830` (type `0xdb`, route 8→2) | Public key, then cipher key | Shared |
| Primary login | `0x00410220` via encrypted sender `0x0043af30` | `0x66` with username, password, `RSPS21.0`, one-byte binary | Shared 4-field path |
| Wait address | `0x004104f0` | Empty `0x5d` | Shared |
| Wait login | `0x004103b0` | `0x4d [username]` | Shared |
| Discovery | `0x00417500` | `0xd1 ["21", [product]]` | `R6Profile.discovery_login` |
| Folder record | Reader `0x00416f00` (14 fields); child reader `0x00416c20` (20 fields) | Same layout as GR2 | `R6Profile.parent_lobby` |
| Friends login | `0x00412650` | `0x4e` with two 4-byte binaries | Shared |
| Ignore list | `0x00414b40` | Empty `0x54` | Shared |
| Player info | `0x00410610` | `0x13 [username]` | Shared |
| Notices | Sender `0x00410aa0`; reader `0x00411f80` | `0xde [language]`; reply needs `[binary 0xde, [text, text]]`, each ≤ 0x201 bytes | `R6Profile.login_notices` |
| Ladder lookup/select | `0x004372d0`, `0x0042f2b0` | `0xcc ["1", ["ladderquery","0","0"]]`, then operation 2 | Shared proxy directory |
| Proxy login | `0x00452810` (route 8→11) | Plain `0x66 [username, version]`, then `0x5d`/`0x4d` | Shared proxy handoff |
| Ranking query | Serializer `0x004379d0` | `["1", [app-id, product, u32, u32, [selection 0–3, values], [cols, sort, filter]]]` | `R6Profile.ranking_table` (see below) |
| Lobby address | `0x00429300` | `0xd1 ["3", ["1"]]` | Shared |
| Lobby login | `0x00428f30` | `0xd2` with five fields (string, number, string, string, number) | Shared discovery path |

The language branches differ from GR2. `0x005129a8` selects `en`, `ge`,
`fr`, `es` and `it`; `0x00542dd4` selects `en`, `de`, `fr`, `es` and `it`.
The profile accepts both German codes.

The primary login object layout matches GR2: username at offset 4,
password at `0x14` and version at `0x25` (`0x0040dc30`). The proxy login
copies the version, so the profile's SDK version must stay `RSPS21.0`.

## Ladder stage requirements

The first retail run (Tubisoft in Docker, PCSX2) signed in, discovered the
parent folder, completed NAT and the proxy handoff, and received all eight
ranking replies. The game then disconnected and showed "Service is not
available right now". Empty tables were the cause.

Sign-in issues eight queries:

| Issuer | Selection | Columns | Result parser | Empty table |
| --- | --- | --- | --- | --- |
| `0x00485e80`, ladders 0–3 | type 1 `[alias, 1]` | none | `0x004849b0` | Accepted as status −10 |
| `0x00485db0`, ladder 0 | type 3 `[0, count]`, sorted by `GLOBAL_RANK` | `ALIAS`, `GLOBAL_RANK`, `RATING` | `0x00485410` | Accepted (zero rows) |
| `0x00485f50`, ladders 1–3 | type 2 `[alias]` | `ALIAS`, `LEVEL` | `0x00485250` | **Rejected** |

Callback `0x00485940` marks a result successful only when its parser
succeeds, and otherwise stores −10. The LADDER stage of `0x00541c70` tolerates
−10 for the type-1 entries (`0x00485780`) but requires success for the top
list and the three type-2 entries (`0x00485760`, `0x00485810`). Parser
`0x00485250` reads row 0 and needs `ALIAS` (string) and `LEVEL` (integer).

Deserializer `0x0043d330` reads `["1", [query-id, total, columns, rows]]`.
Each column is a two-string list (field name ≤ 128 bytes, second name ≤ 32
bytes). The first string is the lookup key (`0x0043d190`). Each row is a list
of strings, one per column. The profile now returns one row per requested
alias for type-2 queries, with `ALIAS` set and every other field `0`, and keeps
the other selections empty. No stats are stored or invented beyond those
unranked defaults.

## Entering the parent lobby

The second retail run passed the LADDER stage and reached the lobby list.
Selecting **Rainbow Six 3** sent lobby address `3`, five-field `0xd2`, then
`0xd1 ["23", ["1", "", "256"]]`. The profile did not yet implement operation
23, so the server closed the lobby connection and the game disconnected.

Lobby operations are issued through `0x0042a1d0` with the operation number
in `a3`; completion readers check the envelope with `0x00415d50`/`0x00415da0`.

| Operation | Sender | Fields | Reader |
| --- | --- | --- | --- |
| 23 parent join | `0x004177e0` | group ID, password, option | `0x0041a730`: group ID, optional string |
| 42 player data | `0x00419980` (from `0x004013a0`) | one 24-byte binary (GR2: 28) | `0x00421710`: envelope only |
| 24 game join | `0x004179d0` | number, string, number, number, string | `0x0041ab90` |
| 12 create | `0x00417dd0` | 11 values incl. two binaries | `0x0041ba60` |
| 8 leave, 9 refresh | `0x00417c00`, `0x004197e0` | group ID / group, number | `0x0041dd10`, `0x0041a2c0` |

Other issued operations: 10, 15, 17, 30, 31, 33, 34, 35, 44, 45, 70. The
profile now accepts the public parent join (`["1", ""]` reply) and the 24-byte
player record. Game-room create and join (12, 24) remain unsupported. Note that
R6 can create rooms through the lobby server (operation 12), as Splinter Cell
Chaos Theory does, in addition to the launcher `0xd5` path below.

### Lobby player count

In the twenty-second run, the lobby list did not count a player who joined
the Rainbow Six 3 lobby. Folder reader `0x00416f00` reads 14 fields. Its
last three numbers line up with fields 11–13 of the 20-field game record
(`0x00416c20`), where field 12 is the limit and field 13 the current
players. The parent record always sent 0 there. Member events
(`0x004034a0`) count players only for games (IDs ≤ 0), so they never change
the lobby's count. Folder updates (operation 54, `0x00421cd0`, folder
branch) go to callback slot `+0x24`, which R6 never registers. The count
can therefore change only when the list is fetched again: operation 21 at
discovery, 23 on joining, or 9.

For R6, the parent record's field 13 is now the number of players in the
lobby, including players in games and a player who is joining. Field 12
stays 8. Whether the list shows field 13 as the current count, and whether
8 acts as a limit, still need a retail check.

## Creating a game (lobby operation 12)

The third retail run entered the empty parent lobby and selected Create Game.
The client sent `0xd1 ["12", [...]]` with eleven values on the lobby-server
connection, not the launcher `0xd5` registration. Sender `0x00417dd0` builds:
parent ID, room name, product, three u16 values, game data (binary), password,
two version strings and additional data (binary). Its caller `0x00404080`
fills the u16 values from its own new entry: the group type (`+0x00`, 5 in
retail), the player limit (`+0xc8`) and a constant 0 (`+0xd0`). A Create Game
with six players therefore sends `5`, `6`, `0`. An earlier version of the
profile read these as players, spectators and flags, so the list showed a
maximum of 5.

Reader `0x0041ba60` expects `["38", ["12", [room-id, text, parent-id]]]`;
`0x004284f0` files the new room under that parent. A failure (`39`) carries
the error text at the same index. Owner operations 15, 17, 30, 31 and 33 each
have a reader that only checks the envelope and reads the room ID.

`tubisoft` now creates an active room for the creating lobby connection
(with the creator's group type), answers the owner's join (`[room-id, ""]`, reader `0x0041ab90`), withholds
GAME_STARTED from the owner, accepts the owner updates above (operation 33
supplies the game port and local address), lists the room on refresh and
removes it when the owner leaves or disconnects. Password-protected rooms are
rejected.

The fourth run showed the owner sending `0xd1 ["34", [room-id]]` about three
seconds after creating its room, while the match loaded. Operations 34, 35,
44, 45 and 70 (senders `0x00419440`, `0x00419270`, `0x004184b0`,
`0x00418320`, `0x00418640`) carry only the room ID and have no completion
reader. Rejecting 34 had closed the owner's lobby connection and removed the
room before a second PC could list it. The profile now accepts these reports
without a reply. In that run every client also appeared from the same source
address, because Docker's port forwarding replaced the real client addresses;
guest joins need the real public/LAN addresses.

## Listing a hosted game

With real client addresses, the second PC still showed **No Game Available**
although the server listed the room. A PCSX2 save state of that PC showed the
room had reached the game's own list: the lobby list object (global
GP−`0x4768`, vector at `+0x1a4`) held one entry for room 1000, added by the
group-new handler `0x00404780`. That handler only accepts a room whose
product is `RAINBOWSIX3PS2US`, versions are `1.0`/`RSPS21.0` and game data is
exactly 84 bytes, which all matched.

The entry's addresses were the problem. The host ran on the server PC, so its
connections reached the container from the Docker bridge gateway. The room
record carried that gateway address, NAT discovery reported it to the host,
and the host embedded it in the last eight bytes of its 84-byte game data
(IPv4 and little-endian game port). Its own local address was PCSX2's
emulated `192.0.2.100`. Neither is reachable from another PC, so the entry
was never shown.

`ubisoft.hostalias` now reports clients that arrive from the container's
default gateway at `TUBISOFT_ADVERTISE_IP` (`TUBISOFT_HOST_ALIAS=auto`,
active only inside a container). Router rooms, handoffs, NAT replies and the
XIII UDP host login use the reported address; replies still go to the real
source. The R6 profile also ignores the emulated `192.0.2.0/24` local address.

With correct addresses the list was still empty. The next save state showed
the pinger (`0x0046a690`) had measured the host at 4 ms, so reachability was
not the cause. The match-list filler `0x004e1ea0` (`P_lobby_matchlist`)
applies these checks to every entry:

- It skips the player's own room.
- If the game data's word at `+0x48` is nonzero (the R6 host sets it to 1),
  `0x00401720` must find the room owner (`+0x34`) in the lobby member list
  (`+0x1b0`), filed under the room's ID and lobby server ID. Otherwise the
  room is hidden.
- When a search is active, it also applies the search filters.

The server never sent member records, so the guest's member list was empty
and every player-hosted room was hidden. Members come from operation 50
(reader `0x004237f0`; callback `0x004034a0`):

```
["50", [name, flag, group ID, address, local address, player data, u16]]
```

The callback files the member with that one group and the connection's lobby
server ID. A later record for the same name replaces the earlier one
(`0x00403070`). Operation 51 (reader `0x00423ea0`) carries `[name, group ID]`.
When the player data is the 24-byte parent record (IPv4 and port first) and
the receiver owns its current room, the callback sends `PERMIT PING`
(`0x0050e2a0`) to that address. Operations 66 (`0x00425800`, `[name, data]`)
and the operation-12 result callback (`0x00403f60`) can do the same.

The R6 profile now keeps a per-service directory of parent members. A member
is recorded at operation 23 and announced to the others once operation 42
supplies its player data. Each member's group is the room it hosts or has
joined, otherwise `1`. On parent join, the server sends the joiner the
records it already has. Room creation, joining or leaving a room, and a closed
room re-announce the member's group. Leaving the parent or disconnecting
sends operation 51. Room records now carry the owner's sign-in name exactly
as the game sent it (`Room.owner_name`), because both `0x00401720` and the
owner check `0x00401180` compare names byte for byte. **Pending:** a retail
check with two PCs.

## Player counts and joining

In the eighth retail run the game was listed but showed `0/5` instead of
`1/6`. A guest joining from the second PC lost its lobby connection: the
server rejected its in-room player data (operation 42) because the profile
had no R6 player-port parser. The reverse direction reached the match even
though the same rejection occurred.

The list's players column is entry `+0xcc` / `+0xc8` (filler `0x004e1ea0`).
The group-new handler always starts `+0xcc` at 0 and ignores the record's
current-player field. Only the member callbacks change it: `0x004034a0`
increments it (or `+0xd4` when the member's flag character is nonzero), and
`0x00403220` (operation 51) decrements it. Both do so **only for group IDs
of 0 or less**. The same sign test chooses between the "in game room" and
"in lobby" state bits (`0x00405090`, `0x004050c0`) for the player's own
record. Operation-51 handling for the player itself leaves the whole lobby
for a positive ID. The group-change callback `0x00401dd0` (operation 57)
also treats IDs of 0 or less as games. This SDK therefore gives game rooms
non-positive IDs, and `Num` fields are parsed with `atoi` and written with
`%d`, so they can be negative.

The profile now:

- numbers R6 rooms `-1000`, `-1001`, … (`negative_room_ids`). The router
  accepts a leading `-` in join (24) and additional-info (46) requests;
- sends other parent members the new game's record (`53`, children only)
  when it is created, then the owner's member record with the room's ID, so
  the owner is counted;
- sends a joining guest the members already in the room, so they enter its
  room member list (`+0x1bc`, via `0x00403070` -> `0x00401810`);
- on leaving a room, sends `51 [name, room]` before re-announcing the member
  in the parent, and sends `55 [room]` to parent members when a room closes
  (reader `0x0041b820` -> `0x00403b40`);
- accepts in-room player data from R6 guests as a member update. The join
  callback `0x00404300` republishes it through `0x004013a0`;
- no longer sends GAME_STARTED. R6's callback for it (`0x00402520`, reached
  from reader `0x004168e0`) is empty.

The ninth retail run confirmed the count, hosting from either PC and joining
in both directions, with both players in the match. Names over other players
were still not drawn. The guest's save state shows the GS side is complete.
The current room is `-1001`, and the host appears in both the lobby and room
member lists, filed under `-1001` with lobby server ID 1. Both names also
exist as Unreal strings in the match. The client builds its own travel URL
with `?Name=` and its GS name (`0x0020af90`, using `0x003ffe50`), and the
server takes no part in that. Drawing names is UnrealScript HUD logic
(`SetIdentifyTarget`, the server option `ShowNames`), so it is most likely a
game-mode or option effect rather than a lobby-protocol gap.

In the tenth run, both players returned to the lobby after the round ended
and opened its player list. The client then sent `0x13 [""]` (player info,
native `0x00406ec0` -> sender `0x00410610`) on the wait-module connection.
The router only answered requests for the caller's own name, so it dropped
the connection and the game signed out. The reply reader `0x0040fed0`
(via `0x004110a0`) accepts `0x26` with the profile strings or `0x27` with a
four-byte error code. R6 now answers with the profile for any signed-in
parent member and with `0x27 [0x13, [0x00000000]]` for an empty or unknown
name. XIII's behaviour is unchanged.

## Player search and ladder browsing

In the eleventh run, browsing the ladder sent further ranking queries (type 2
and type 3 selections). They received the existing empty or zero-value
tables without errors. Searching for a player to add as a friend sent
`0x08` with seven strings on the wait-module connection (sender
`0x00412c50`: five strings, a `0`/`1`/`2` character and one more string).
The router had no handler, so the connection closed.

Reader `0x00413d80` accepts one result per `0x26` reply,
`[0x08, [name, u32 status, product]]`. Callback `0x003fd3d0` keeps up to 20
results in a `0xd0`-byte entry list. It treats `0x27` with error code `0x30`
as the end of the search and any other code as a failure. The friends UI
(`0x004e0700`, jump table `0x005cfa20`) shows status 0, 2, 3, 6 and 8 as
online, 1 and 7 as offline, and 9–11 as in game. For 8–11 it also compares
the product string with its own.

R6 now searches the account names (case-insensitive substring of the first
field, at most 20, excluding the searcher). Each match is reported as
online (0) if the player is in the parent lobby, in game (9, with the
product) if the player is in a room, and offline (1) otherwise. The search
ends with `0x27 [0x08, [0x30 00 00 00]]`.

## Friends list

The twelfth run went from a search result to player info (`0x13` for an
account that was not in the lobby, which got the failure reply), the
player's ladder rows and then Add Friend. The client sent
`0x4b [name, group, u32]` (sender `0x004127a0`), which had no handler.

| Operation | Request | Reply | Client handling |
|---|---|---|---|
| Add `0x4b` | `[name, group, u32]` | `0x26 [0x4b, name]` / `0x27 [0x4b, [code]]` | reader `0x00412ea0`, callback `0x003fdc70` (state only) |
| Remove `0x4c` | `[name]` | `0x26 [0x4c, name]` / `0x27` | reader `0x00412f70`, callback `0x003fdbe0` removes the entry (`0x003fcf50`) |
| Friend record `0x4a` | pushed | `[name, u32 status, group, u32, u32, product]` | reader `0x00413bb0`, callback `0x003fdd90` builds a `0xd0`-byte entry (`0x003ff1c0`) and files it (`0x003fd0d0`) |

The client adds a friend to its list only when it receives a `0x4a` record,
not from the add reply. The callback slots come from the `clFriendsCB`
vtable at `0x00606810`.

The account database now has a `friends` table (owner, friend, group). It
is created on first use, keeps the schema version, and allows at most 100
friends per account. For R6:

- friends login (`0x4e`) is followed by one `0x4a` record per stored friend;
- a successful add replies with the friend's display name and pushes that
  friend's `0x4a` record;
- remove deletes the row;
- player info (`0x13`) also answers for existing accounts that are not
  signed in.

Status comes from the lobby member directory: 0 if the friend is in the
parent lobby, 9 with the product if in a room, otherwise 0 if the friend
has a friends channel and 1 if not.

In the fifteenth run, a friend in the same game still showed as offline: the
status was computed only at friends login and when the friend was added. The
service now keeps each signed-in watcher's stored friends and pushes a fresh
`0x4a` record to every watcher of a player whose status changes: lobby
announcements, room joins and departures, friends login and friends logout.
`0x003fd0d0` first drops any entry with the same name (16-byte compare) and
then files the record by status, so a repeated record updates the entry in
place.

## Messages and personal info

In the thirteenth run, adding a friend and reloading the list worked. Sending
a message sent `0x58 [[recipients], text]` (sender `0x00412a40`), and changing
the account's country sent an encrypted `0x62` with six strings (sender
`0x00410750`: an empty string and five profile fields). Neither had a
handler, and each closed a connection.

- `0x58` reply (reader `0x00413170`): `0x26 [0x58, name]` or
  `0x27 [0x58, [code, name]]`.
- Incoming messages are pushed as `0x65 [from, text, time]` (reader
  `0x00413040` -> `0x003fca90`). The game keeps them in a 10-entry inbox of
  `0x130`-byte records: sender at `+0x108`, text at `+0x08` (0x100 bytes) and
  the time at `+0x118`. Parser `0x003fef10` reads fixed fields: year-1900
  (3 digits), then month-1, day, hour, minute and second (2 digits each).
  `0x005146c0` shows them as `Y-M-D h:mm GMT`, so 2026-11-12 22:05:40 UTC is
  sent as `1261012220540`. The first retail run sent `HH:MM` and displayed
  `1922-6-0 0:00 GMT`.
- A `0x5a [from, [u32 length, bytes]]` reader exists (`0x00413470`). Its
  callback slot (`+0x2c`) is never registered, so R6 ignores it.
- `0x62` reply (reader `0x00411ea0`): `0x26 [0x62]` or `0x27 [0x62, [code]]`.

R6 now records a friends channel per signed-in account at friends login.
`0x58` delivers `0x65` to each recipient that has a channel. It replies
success when all recipients received the message, and otherwise `0x27` with
the first one that didn't. Offline messages are not stored. `0x62` is
acknowledged. Its country is stored as described in "Country" below.

In the fourteenth run, a message from PC2 reached PC1, but PC2 stayed on the
Send Message screen. The server had sent `0x26 [0x58, [name]]`. Reader
`0x00413170` and callback `0x003fdb60` set the send-message request state
(friends object `+0x60`: bits 2 = finished, 4 = success), as the add-friend
path does for `+0x50`. Native code never reads these states, so they are
presumably polled from UnrealScript.

A save state of the stuck PC2 showed `+0x60` still 0, while the friends
module's current message (`+0x04`) was our `0x26` reply, so the reader ran
but returned 0. The message tree uses value types 1 (string), 2 (binary) and
3 (list). Reader helper `0x00466e10` returns an element only if it is a
string (type 1), but the reply carried `[name]`, a list. Replies to `0x58`,
`0x4b` and `0x4c` now carry the name as a plain string in field 1. Adding a
friend had appeared to work only because the pushed `0x4a` record fills the
list on its own.

## Country

After the ignore list, a changed country still did not persist. The
profile screen (menu handler `0x00518af0` -> `0x00406f40` -> sender
`0x00410750`) sends `0x62` with an empty string and five strings. The fifth
is a country code: `0x005140e0` looks it up from the name chosen in the
`P_country` list. Player info (`0x13`, reader `0x004110a0`) returns seven
strings. Reader `0x004110a0` and callback `0x004068e0` -> `0x00407350` file
them at player object (`gp[0x658804]`) `+0x228`, permuted:

| `0x13` field | Offset | Size | `0x62` field | Personal Settings |
| --- | --- | --- | --- | --- |
| 0 | `+0x114` | 10 | — | — |
| 1 | `+0x0` | 16 | — | alias |
| 2 | `+0x10` | 33 | 3 | menu `+0x130` |
| 3 | `+0x31` | 33 | 2 | menu `+0x124` |
| 4 | `+0x52` | 65 | 5 (country code) | country (`+0x13c`) |
| 5 | `+0x93` | 129 | 4 | menu `+0x148` |
| 6 | `+0x11e` | 129 | — | — |

Personal Settings (`0x00542e40`) shows `+0x52` as the country:
`0x00514250` maps the code back to its `P_country` name, and an empty or
unknown code shows "NO LIMIT". Sign-in requests the player's own info, so a
server that echoes the code back in field 4 makes the choice persist.

The first two attempts used fields 5 and 3, read from the argument order
without the reader's permutation. A save state from the twenty-sixth run
showed the code at `+0x31` (field 3) and the alias from field 0 at `+0x114`,
which established the table above.

R6 now stores only the country code (`countries` table, printable ASCII, at
most 15 characters; empty clears it). The twenty-seventh run confirmed that
the choice persists across sign-in. Ladder rows report the code's numeric
`COUNTRY_ID` (see Ladders). Every `0x13` reply carries
it in field 4, for the player's own info and for other players. The other four
`0x62` fields may be personal details. They are neither stored nor logged;
the log records only their lengths.

## Ignore list

In the twenty-third run, ignoring a player sent `0x4f [name]` (sender
`0x00414880`). It had no handler, so the connection closed. The friends
dispatcher (`0x0040dee0`) routes the ignore operations:

| Operation | Request | Reply | Client handling |
|---|---|---|---|
| Ignore `0x4f` | `[name]` | `0x26 [0x4f, [name]]` / `0x27 [0x4f, [code, [name]]]` | reader `0x00414c60`, callback `0x003fd8b0` adds the name (friends object `+0x3c`) and completes `+0x80` |
| Unignore `0x50` | `[name]` (sender `0x004149e0`) | same shape | reader `0x00415140`, callback `0x003fd700` removes the name and completes `+0x88` |
| List `0x54` | empty | `0x26 [0x54]` | reader `0x004157f0`, callback `0x003fd680` completes `+0x90` |
| Entry `0x55` | pushed | `[name]` | reader `0x004156b0`, callback `0x003fd4f0` adds the name if missing |

Unlike `0x4b`, these readers take element 1 as a list. The failure codes
0xd and 0xe map to specific errors; other codes are a generic failure.

The account database now has an `ignores` table (owner, ignored), created
on first use and limited to 100 entries per account. For R6:

- `0x54` is followed by one `0x55` per stored entry.
- `0x4f` and `0x50` update the table.
- A message (`0x58`) from an ignored player is dropped, and the sender still
  gets the normal success reply.

Removing a friend (`0x4c`) returned straight to the menu, with no
confirmation. The success reply completes the request, and callback
`0x003fdbe0` removes the entry. What the screen shows afterwards is decided
by the game's menu script, not the service.

## Match start and results

The fifteenth run had a full match but no scores, and the client never sent
operation 30 (match results). The chain is:

- End-of-match handler `0x00400520` submits results only when flag `+0x18d`
  of the GS object (`gp[0x65883c]`) is set.
- The owner's operation 17 (sender `0x00402990`, `[room, state]`, sent at
  every round start) clears that flag before sending.
- Only the operation 62 callback `0x00402320` sets it. Its reader
  `0x0041e1b0` takes `["62", [room, match ID]]`. The callback stores the match
  ID at `+0x10c`, rebuilds the room member list and republishes the player
  data.
- Results then go through `0x00401070` -> `0x0040b340` -> `0x0040ed80` ->
  sender `0x00418ec0`, which sends operation 30 as `[room, results]` through
  `0x0042a1d0`. The `MatchResults` object lives at GS client `+0x160` (ctor
  `0x0040ec20`, add `0x00456370`, serialize `0x004568f0`: two numbers, then
  one entry per player from `0x00457370`). Reader `0x0041e8c0` reads only
  the room ID back.

The service now answers the owner's operation 17 and then sends
`["62", [room, match ID]]` to every member filed under the room, the owner
included. Match IDs are a per-instance counter.

In the sixteenth run (a Sharpshooter round), operation 62 enabled the
results. The owner then sent operation 30 about every 15 seconds during the
round, ending just before the round-end reports (70/45). Each message is
`[group, lobby server, entry...]` with the parent group (`"1"`, `"0"`), not
the room, and one entry per player. The entry (serializer `0x00457370`) is
`[name, highest field ID, field count, field-ID bit mask, values]`. Retail
sent field IDs 0–19 (`"19"`, `"20"`, `"1048575"`). Values are in field-ID
order. `0x00456370` finds or adds the player's entry, and `0x00457280` sets
one field and updates the mask and counts.

In that run, field 0 stayed constant per player (6 and 5) and field 1 rose
from 0 to 3 for both players as they scored. Fields 2–15 stayed 0, and
fields 16–19 were cut off in the log. The ladder column names in `MP.SOZ`
are `GLOBAL_RANK`, `ALIAS`, `LEVEL`, `RATING`, `FRAGSCORE`, `KILLS`,
`DEATHS`, `RATIO`, `TEAMKILLS`, `ROUND`, `TEAMROUND`, `TEAMWIN`,
`TEAMRATIO`, `TIMEPLAYED` and `COUNTRY_ID`. The ladder names are
`SURVIVAL`, `TEAM SURVIVAL` and `SHARP SHOOTER`. The script that fills each
field ID lives outside `MP.SOZ`, so the mapping has to come from retail
results.

Operation 30 is acknowledged with `["38", ["30", [first value]]]`.

The seventeenth run (one Sharpshooter round, then Survival rounds hosted
by the other PC) showed what the two leading values are. The first is the
match ID from operation 62 (1 for the first match, then 2, 3 and 4 for
each Survival round), not the parent group. `0x00400520` creates the
results with that ID (`+0x10c`) and 0, then adds fields 0–19 for each
player in its statistics list (`+0x1c8`). Field meanings seen so far:

| Field | Meaning |
| --- | --- |
| 0 | Team (5 and 6 in Sharpshooter and Survival, 2 and 3 in Team Survival) |
| 1–16 | Kills, by the victim's level (0–15) for the room's ladder |
| 17 | Deaths |
| 18 | Team kills, suicides included |
| 19 | Seconds played in the round |

Kill recorder `0x004006d0` (from native `0x0035e120`) explains the fields.
For a kill of another team it adds a death to the victim and one to the
killer's field 1 + victim level. For a same-team kill, including a
suicide, it adds a death and a team kill to the killer. `0x00400610` sets
field 19. In the eighteenth run, a suicide added a death and a team kill
and left the kills unchanged.

The owner's operation 17 state is the game mode: 1 Survival, 2 Team
Survival, 3 Sharpshooter. Each Survival and Team Survival round is a
separate match.

Both players reported that the scoreboard (Start) showed no scores and no
end-of-round message appeared. Two client paths were waiting on the
service:

- Pushed operation 71 (reader `0x00426040`, callback slot `+0xb4`,
  `0x00400c40`) carries `[match ID, result, [[name, [values]], ...]]`.
  The callback ignores any match ID other than its own (`+0x10c`), stores
  the records (`+0x200`), and merges them into the per-player statistics
  (`0x00400e10`). It then completes the results request (`+0x17c`) that
  `0x00401130` reset before each submission. The merge copies the first
  12 values and adds value 0 to the player's level for the room's ladder
  (`+0x1f8`), clamped to 0–15. `0x00400ae0` then copies the levels to the
  lobby member (`+0x12c`). Value 0 is therefore a level change.
- The owner's operation 45 (`0x00402860`, round end) leaves a request
  state (`+0x15c`) pending. Reader `0x00424410` takes
  `["38", ["45", [number]]]`, and callback `0x004027e0` completes it.

The service now pushes operation 71 to every member of the sender's room
after each operation 30, and completes operation 45 with the room ID.
Operations 34, 35, 44 and 70 have no reader in the dispatcher
(`0x0040e470`) and stay one-way.

The eighteenth run, which echoed the submitted fields, showed scores on
both PCs. Echoing is wrong, though. Scoreboard row filler `0x00478590`
shows value 3 (`0x00400300`) and value 1 (`0x00400410`) once the results
request has succeeded, and `0x003f63b0` picks the player with the highest
value 3. Value 0 is a level change. The 12 values are taken to be the
ladder columns after `GLOBAL_RANK` and `ALIAS`. The nineteenth run is
consistent with that: the kills appeared under the column with an operative
icon (value 3, `KILLS`), and the column beside it (value 1, `RATING`) stayed
at 0. The column order is: `LEVEL`, `RATING`, `FRAGSCORE`,
`KILLS`, `DEATHS`, `RATIO`, `TEAMKILLS`, `ROUND`, `TEAMROUND`, `TEAMWIN`,
`TEAMRATIO` and `TIMEPLAYED`. Operation 71 now sends per-match statistics
in that order:

- kills: the sum of fields 1–16;
- deaths: field 17;
- team kills: field 18;
- frag score: kills minus team kills;
- round: 1 in Survival and Sharpshooter;
- team round: 1 in Team Survival;
- team win: 1 for the only team with a player who did not die (Team
  Survival has no respawn);
- time played: field 19.

The per-match `RATIO` and `TEAMRATIO` are 0. `LEVEL` and `RATING` were 0
until the Elo ladder below was added. The mode comes from the operation 17 state
stored with the match ID. The owner's game submits results on a fixed
interval of about 15.3 seconds (runs 16–18), and the online scoreboard
shows only server statistics. In the eighteenth run, Sharpshooter scores
therefore lagged kills by up to that interval. Short Survival rounds get
one update. The service answers each submission at once.
**Pending:** store the final values for each match and feed the ladder
tables, which still return zeros.

## Latest News

The two login notices (`0xde`, reader `0x00411f80`) are the game's "Latest
News" items 1 and 2. Both were empty, so the twenty-second run showed only
the numbers. R6 now has built-in text for both. `TUBISOFT_R6_NEWS_1` and
`TUBISOFT_R6_NEWS_2` replace them (`deploy/compose.r6.yaml` passes them
through). Each item must be printable ASCII and at most 512 characters,
because the reader copies it into a 0x201-byte buffer. An unset item keeps
the built-in text, and a single space blanks it. Other profiles use the same
`TUBISOFT_<SLUG>_NEWS_1/_2` settings; GR2's default notices stay empty.

## Ladders and the Tubisoft Elo rating

Ubisoft's server computed `RATING` and `LEVEL`, and the game does not
contain that formula. At the project owner's request, R6 now uses
Tubisoft's own Elo rating (`src/ubisoft/games/r6_ladder.py`). It is not the
original formula.

Ranking queries: `0x00427770` stores the query's fourth value (`+0x30`),
which is the ladder index. Sign-in asks for ladders 0–3, and for `LEVEL`
on ladders 1–3. The column index table at `0x0058f4c0` lists `GLOBAL_RANK`,
`ALIAS`, `LEVEL`, `RATING`, `FRAGSCORE`, `KILLS`, `DEATHS`, `RATIO`,
`TEAMKILLS`, `ROUND`, `TEAMROUND`, `TEAMWIN`, `TEAMRATIO`, `TIMEPLAYED` and
`COUNTRY_ID`. Native getters read `GLOBAL_RANK`, `LEVEL`, `RATING` and
`COUNTRY_ID` as integers and `ALIAS` as a string. Ladders 1–3 are taken to
be Survival, Team Survival and Sharpshooter, the order of the ladder names
and of the operation 17 mode numbers. Ladder 0 combines all modes. This
mapping still needs a retail check.

Recording:

- Every operation 30 keeps the match's latest results. The owner's
  operation 45, at round end, records them.
- Only the room's owner sends operation 30 in the retail runs, and the
  service accepts it only from the owner and only for the match ID that the
  room's latest operation 17 started. Another player's report is ignored
  without a reply. The owner's report for a finished or replaced match is
  acknowledged but neither stored nor broadcast.
- The game only counts up, and no retail report held a negative value. A
  report with a negative or non-decimal value, or one above a field's bound,
  is rejected before anything is rated, stored or broadcast, and the
  connection is closed. The bounds are 255 for the team, 9,999 for each kill,
  death and team-kill count, and 86,400 seconds (`r6_ladder.FIELD_LIMITS`).
- Only players who were in the room at some point since the match's
  operation 17 can be rated. An entry naming anyone else is dropped from the
  stored results and the operation 71 broadcast. A player who leaves
  mid-round stays eligible. A report that names a player twice is rejected.
- The statistics themselves are only as trustworthy as the room owner. The
  game runs the match on the owner's console, and only that console reports
  the results, so the service cannot verify them. These checks limit a
  dishonest owner to misreporting the players who were really in the room.
- A round that is abandoned (a new operation 17 with no operation 45, or
  the room removed first) is not recorded. Its match ID, mode and latest
  results are dropped with it, so abandoned rounds leave no state behind
  (`RoomRegistry.forget_match`).
- Each player with an account gets a row on the mode's ladder and on
  ladder 0 (table `r6_ladder` in the account database). The row holds the
  rating, the counters (frag score, kills, deaths, team kills, rounds, team
  rounds, team wins and seconds) and the match count.
- The room's members then get a final operation 71 carrying the rating
  change (value 1) and the level change (value 0).
- Earlier operation 71 messages carry the rating change so far, with no
  level change, because the client adds value 0 to the level every time.

Rating:

- New players start at 1000, with K = 32.
- Every pair of rated players in a match is one Elo game. The expected
  score is 1 / (1 + 10^((opponent - player) / 400)).
- A player's change is K × (sum of score − expected) ÷ the number of
  opponents, rounded.
- Sharpshooter compares frag scores (kills − team kills).
- Survival ranks surviving first, then frag score.
- Team Survival compares only players on opposite teams, using the round's
  winning team. A round with no single winning team is a draw.
- Players without an account are neither rated nor counted as opponents.
- Level = (rating − 1000) ÷ 50, rounded down and clamped to 0–15.

Queries:

- Type 2 (named players) returns a row per alias. A player without a
  finished match gets zeros, as before.
- Type 3 `[start, count]` returns rows from a 0-based position.
- Type 1 `[alias, count]` returns a window around the player.
- Rows are ordered by rating, then kills. `GLOBAL_RANK` is the position.
- `RATIO` is kills ÷ deaths and `TEAMRATIO` is team wins ÷ team rounds,
  each ×100 and rounded down (1.5 is sent as 150). Every cell except
  `ALIAS` must be a whole integer. Int getter `0x00427530` (`0x0043c650`,
  `strtol`) throws `CLadderResults::NonNumericValue` otherwise, and My Rank
  (`0x004849b0`) needs exactly one row and reads all 15 columns as
  integers. In the twenty-first run, `"1.50"` left My Rank blank while Top
  10 (which does not read `RATIO`) worked. `TIMEPLAYED` is in seconds.
- `COUNTRY_ID` is the numeric ID of the player's stored profile country
  (see Country), or 0 for none.
- An empty result keeps the empty form that the sign-in stage accepts.
- Every column is returned, with the requested ones first. In the
  twentieth run, the My Rank screen (type 1 `[alias, 1]`, ladders 0–3) and
  the ladder screens (type 3 `[0, 10]`) sent no column names. The service
  had answered them with the empty form, which left My Rank blank and
  showed no new results. After the first recorded match, the sign-in top
  list (type 3 on ladder 0, requesting `ALIAS`, `GLOBAL_RANK` and `RATING`)
  returned rows, and refreshing showed an error. Parser `0x00485410` reads
  `GLOBAL_RANK`, `ALIAS`, `COUNTRY_ID` (`0x00486210`) and `RATING` from
  every row and fails when a column is missing.
- The log records each query's ladder, selection (names only by length),
  columns and filters, so the browsing screens' requests can be checked.

### Country IDs and the country filter

The profile screen loads three parallel `P_country` lists into the online
menu object: names at `+0x190`, codes at `+0x19c` and numeric IDs at
`+0x1a8`. There are 240 entries. A save state from the twenty-sixth run
gave the values: for example "NoLimit" `nll` 0, Canada `ca` 153, Germany
`de` 35, United Kingdom `uk` 308 and United States `us` 309. France and two
French territories share 189. Operation `0x62` stores the code
(`0x005140e0`). Ladder rows carry the ID, which `0x00513eb0` maps back to a
name (rank screens `0x00529460` and `0x005298e0`; an unknown ID shows the
default). `r6_ladder.COUNTRY_IDS` holds the code-to-ID table, and
`COUNTRY_ID` is filled from it.

The ladder query handler `0x00518af0` adds a filter when a country other
than "No Limit" is chosen. `0x00513fc0` turns the chosen name into its ID,
and `0x004863f0` stores a 0x8c-byte record: `COUNTRY_ID` padded with NULs
to 0x84 bytes, then two little-endian words, operator 0 and the ID.
Serializer `0x004379d0` sends each record as a binary value in the query's
third column list (columns, sort, filters). The service applies operator 0
on `COUNTRY_ID` to the type 1 and type 3 selections and ignores other
filters. `GLOBAL_RANK` stays the overall position, and the total is the
number of matching rows. A filter value that is not a 0x8c-byte binary
gets the empty form.

## Leaving a room

After a guest left a game in the sixteenth run, that game was missing from
the guest's list until Search Game re-entered the parent lobby. The success
callback for operation 8 (`0x00403880`, callback slot `+0x60`) handles a
room ID (≤ 0) by clearing the current room (`+0x194`), freeing every game
and member entry (`0x004051c0`), and then requesting the parent group again
with operation 9 (`0x0040b470` -> `0x004197e0`). The operation 9 reply
listed the game again, but its owner was no longer a known member, so match
list filler `0x004e1ea0` hid it. The service now follows the operation 9
listing with the announced members' records (operation 50), as it does
after operation 23. `0x00401880` skips a member already filed under the
same group, so a repeated record is not counted twice.

## Launcher hosting path (XIII-like)

The launcher client (global GP−`0x46f0`) logs in with three strings
(`0x004539b0`) and registers a room with `0xd5` (`0x00453ca0`). Its body is
`["12", <number>, [16 values]]`: string, number, string, string, number,
two numbers, two binaries, three strings, number, local address string,
port number and a final binary. That matches XIII's 16-field registration.
Host operations 17 (`0x00454a50`) and 45 (`0x00454c40`) also match XIII's
host-update subtypes. Child records use the 20-field reader `0x00416c20`.

R6 also has DEDICATE and DEDICATE LOBBY sign-in stages, so the retail
client may look for dedicated servers too. The `0x00541c70` branches
that choose between them are **Pending**.

The retail client did not use this launcher path. It creates games with
lobby operation 12 and joins them with operation 24 (see Creating a game and
Player counts and joining), so `supports_rooms` is now true for R6. XIII's
group flag `0x08`, four-byte player port and member-event layout are not
reused.

## Comparison capture

A Splinter Cell Chaos Theory PS2 client capture (client-to-server direction
only) shows the same order in a later title: key exchange,
encrypted `0x66`, `0x5d`, `0x4d`, discovery `21`, friends `0x4e`, notices
`0xde`, `ladderquery` lookup/select/report, proxy `0x66`/`0x5d`/`0x4d`,
`1281` ranking queries, lobby address `3`, five-field `0xd2`, parent join
`23` and 28-byte player data `42`. The capture is not R6 evidence. It
supports the order and helps interpret R6's static readers. The captured
account names and addresses are not reproduced here.

## Implemented profile

`ubisoft.games.r6` registers slug `r6`, product `RAINBOWSIX3PS2US`, SDK
version `RSPS21.0`, default ports 30020 TCP/UDP, 30021 TCP and 30022 UDP,
the `[Servers]` bootstrap, discovery, empty notices, the ladder proxy and
ranking tables, parent-lobby join and the 24-byte parent player record. Rooms
are created through lobby operation 12, and parent members are announced with
operations 50 and 51. Guests join with operation 24. Two retail clients have
hosted and joined games in both directions, played rounds, and had results
recorded on the Tubisoft ladder. The friends, messages, ignore list and
country settings work too. Remaining limitations: tests used two players on
PCSX2; the DEDICATE stages are untraced; the lobby player count is not pushed
live; and ratings use Tubisoft's own Elo formula, not Ubisoft's.

```sh
TUBISOFT_GAMES=xiii,r6 ...
docker compose -f compose.yaml -f deploy/compose.r6.yaml --env-file .env up -d --build
curl --noproxy '*' 'http://127.0.0.1:18080/gsinit.php?dp=RAINBOWSIX3PS2US'
```

`tests/test_r6_profile.py` covers the static shapes. It also runs the
real-socket sequence above: shared XIII/R6 account, discovery, notices with
`ge`, proxy ranking query and lobby login. A second real-socket test covers
a guest joining, the room's member records, player data, match start and
results, friends and personal info, and leaving. It also covers an owner
leaving a room before its self-join and an owner disconnecting, both of
which remove the game from other members' lists.

Notifications to another player's connection are bounded: a recipient whose
unsent backlog would exceed 256 KiB has stopped reading and is disconnected
(`router.NOTIFICATION_BACKLOG_LIMIT`).

## Next retail checks

1. Check the ladder `COUNTRY_ID` column and the ladder screen's country
   filter.
2. Play with more than two players and confirm room counts, results and
   ratings.
3. Trace the DEDICATE and DEDICATE LOBBY branches of `0x00541c70`.
4. For any new `unsupported lobby request` log, trace its sender and reader
   in `MP.SOZ` before implementing it. A PCSX2 save state of the online state
   is useful when a reader depends on runtime objects.
