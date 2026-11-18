# Brothers in Arms: Earned in Blood (PS2): GS compatibility notes

Brothers in Arms: Earned in Blood for PS2 (`SLUS-21310`, October 2005) is
the sequel to Road to Hill 30 and links the same Ubisoft GS client library
and the same `BIAGLClient` wrapper. Its lobby protocol routes the identical
set of 42 operations, and its readers have the same field lists, so the
`eib` profile subclasses the [Road to Hill 30 profile](brothers-in-arms-network.md)
and reuses its sign-in, lobby, room, match and friend handling. Most of
those notes apply unchanged.

New in Earned in Blood: a co-op **Skirmish** mode for one or two players
against the AI (results with eleven values, and skirmish ladder boards), an
**IRC chat server** used for lobby chat and matchmaking invitations, a
**room master** push (operation 59) and a **lobby type**.

Evidence labels follow [the GR2 notes](ghost-recon-2-network.md):
**Static** (traced instructions or data on this disc), **Retail** (an
unmodified client against Tubisoft) and **Pending**. Sections are **Static**
unless a retail run is named.

## Executables and reproducibility

| Input | SHA-256 |
| --- | --- |
| `SLUS_213.10` | `a55ee872f2e8923a226e9cadadce5357bbaa22627cc1008c0defd0f0de25659d` |
| `MP.SOZ` | `b75927618721f605e184677e85ce838db459ce8d7ccfaaf7fa6a899c30993f42` |

Like Road to Hill 30's, `MP.SOZ` is a plain ELF, not a size word plus zlib
stream. Its first program header is at file offset `0x100` and loads
`0x5ae500` bytes at `0x00100000`, with a memory size of `0x6d0380` and entry
`0x00100008`. GP is `0x006b5f70`. To get a flat image:

```python
d = open('MP.SOZ', 'rb').read()
b = d[0x100:0x100 + 0x5ae500]; b += b'\0' * (0x6d0380 - len(b))
open('MP.bin', 'wb').write(b)          # base 0x00100000, GP 0x006b5f70
```

## Identity and bootstrap

The pointer table at `0x0061df50` has Road to Hill 30's layout:

| Field | Earned in Blood | Road to Hill 30 |
| --- | --- | --- |
| Bootstrap host | `gsconnect.ubisoft.com` | same |
| Request | `GET /gsinit.php?dp=%s&user=%s` | same |
| Product (`dp`, friend product) | `BIAEIBPS2US` (`0x006642e8`) | `BROTHERSINARMSPS2US` |
| SDK version | `BIAEIBPS2US1.0` (`0x006642f8`) | `BIAPS2US1.0` |
| Game version | `1.0` (`0x00664308`) | `1.0` |

`Friend::UpdateFriend` (`0x004f9b40`) compares a friend's product with the
table's product slot (`0x0061df60`), so presence records carry
`BIAEIBPS2US`. The `[Servers]` keys are Road to Hill 30's
(`0x0061dda0`-`0x0061de30`). The GS library also reads `IRCIP%d` and
`IRCPort%d` (reader `0x004bca20`); Tubisoft serves `IRCIP0`/`IRCPort0` for
its chat server (see "IRC chat").

The profile uses ports 30050-30052 and TCP 30053 for chat
(`TUBISOFT_EIB_*` overrides, `TUBISOFT_EIB_CHAT_PORT` for chat), the block
after 187: Ride or Die's 30040-30042 (Road to Hill 30 has 30030-30032), and its own MOTD (`TUBISOFT_EIB_NEWS_1/_2`). The
notice request uses the same five language codes (`en`, `fr`, `de`, `it`,
`es` at `0x00660430`).

## Library layout

| Part | Earned in Blood | Road to Hill 30 |
| --- | --- | --- |
| GS library (lobby) | about `0x004bc000`-`0x004dc000` | `0x004ef000`-`0x00518000` |
| `BIAGLClient` / `UGameServiceSubsystem` | about `0x004eb000`-`0x00502000` | `0x0051b000`-`0x00532000` |
| IRC client | about `0x005dd000`-`0x005e1000` | (unused) |
| `CNatService` | about `0x005d7000`-`0x005d9000` | about `0x0060e000`-`0x00610000` |

The `UGameServiceSubsystem` vtable starts at `0x006a9a70`; the
lobby-callback vtable at `0x006a9f7c` keeps Road to Hill 30's slot offsets.

| Function | Road to Hill 30 | Earned in Blood |
| --- | --- | --- |
| Lobby reply dispatcher | `0x004ef620` | `0x004bef80` |
| JoinLobby | `0x0051e300` | `0x004ee420` |
| CreateRoom | `0x0051e960` | `0x004eea10` |
| StartMatch / FinishMatch | `0x0051f7e0` / `0x0051fb10` | `0x004ef8c0` / `0x004efdb0` |
| ChangeMatchMap (Play Next Game) | `0x0051fe80` | `0x004f0130` |
| JoinFriend / JoinFriendLobby | `0x00520bd0` / `0x005212b0` | `0x004f0e80` / `0x004f1590` |
| SubmitMatchResults | `0x00524570` | `0x004f4af0` |
| Ladder request | `0x00524910` | `0x004fb3d0` (and `0x004fb570`) |
| Friend::UpdateFriend | `0x00527ca0` | `0x004f9b40` |
| Tick (also drives IRC) | `0x0051d510` | `0x004ed060` |
| GetCurrentLobbyType | (none) | `0x004f79d0` (vtable `+0xe0`) |
| LoginRcv_PlayerInfo | | `0x004fba00` |
| NewRoom / NewLobby / RoomInfo | `0x0052b370` / `0x0052b790` / `0x0052bad0` | `0x004fd770` / `0x004fdb80` / `0x004fdef0` |
| MemberJoined / MemberLeave | `0x0052bf00` / `0x0052cb20` | `0x004fe350` / `0x004fee90` |
| MasterChanged (slot `+0x48`) | empty | `0x004ff450` |
| FinalMatchResults (slot `+0xb4`) | `0x0052dec0` | `0x00500440` |

The friend status mapping and the JoinFriend thresholds are unchanged (6 or
more joins a game, `0x004f0fb8`; 5 or more a lobby, `0x004f1004`), so
lobby friends are reported as GS status 8 and players in a room as 9.

## Sign-in

The expected order is Road to Hill 30's. Two details checked for this disc:

- **Player information (`0x13`).** Reader `0x004c0d00` reads seven strings
  and calls `LoginRcv_PlayerInfo` (`0x004fba00`). Field 0 is the player's
  **IRCID**: the callback copies it with a 10-byte limit and logs "Got the
  IRCID %s for user %s". Field 1 is the alias. This is the field that Road
  to Hill 30 and Rainbow Six 3 file at `+0x114`. The player's own goes to
  `+0x20c` and becomes its IRC nick; once its chat login has succeeded
  (`+0x216` set), a looked-up player's goes to `+0x217`, the target of an
  invitation. A copy of 10 bytes cuts a long name short and leaves it
  unterminated, so Tubisoft sends a short chat ID instead of the username:
  `p` and eight base-32 characters of the SHA-256 of the case-folded name
  (`EIBProfile.player_info_id`).
- **NAT byte order.** `CNatService::CBRequestFinished` (`0x005d8610`)
  prints the NAT server's address word byte 3 first ("Got RAW NAT info"),
  as does PERMIT PING (`0x005d82d0`). CreateRoom (`0x004eea10`, "Set room
  nat info") copies that word and the port into session info bytes 4-7
  and 8-11. This is Road to Hill 30's order, so the profile keeps
  `nat_address_byteorder = "big"`. (The Road to Hill 30 debug strings are
  still present, at `0x0066ff00`-`0x0066ffa0`.)

### Account creation (Retail run 1)

Account creation (`0x01`, seven strings) arrived as for Road to Hill 30.
The name was already taken (accounts are shared between games), and the
service sent no reply, so the game waited on the create-account screen.

A refusal is GSFAIL (`0x27`) with `[0x01, [reason]]`, the reason a
four-byte binary: the shared reply reader (`0x004bfd10`) copies it after
checking the message type. `LoginRcv_AccountCreationResult`
(`0x004fc2c0`) turns reasons 1, 2, 3, 5, 11, 13 and 100 into separate
events and anything else into a generic one. The events' text is in
UnrealScript, but Rainbow Six 3's registration screen handles the same
reasons and names its message boxes (category 4 at `0x005185c0`, names
in table `0x0059f7b0`):

| Reason | Rainbow Six 3 message box |
| --- | --- |
| 1 | `UC_MSGBOX_ACCOUNTNAEEXIST` (the name exists) |
| 2, 3 | `UC_MSGBOX_NOTALLOWNAME` |
| 5 | `UC_MSGBOX_ACCOUNTNAMERESERVED` |
| 11 | `UC_MSGBOX_NOTALLOWPASSWORD` |
| 13 | `UC_MSGBOX_PASSWORDCONTAINNAME` |
| 100 | `UC_MSGBOX_SERVERNOTAVAILABLE` |

The profile's `account_failure` now answers a taken name with reason 1, a
name the service would refuse (empty, over 16 characters or with control
characters) with 3, and such a password with 11; the router sends it as
GSFAIL. Titles without verified codes still get no reply. As before, the connection closes after the
reply; the game opens a new one for each attempt. **Retail (run 15):** a
taken name now shows the game's error message and returns the player to
the account creation screen.

## Lobby type

Lobby records keep the 14-field layout (reader `0x004c6670`, called
through `0x004c9260`). `LobbyRcv_NewLobby` (`0x004fdb80`) logs
`%s, (%d, %d, %d)`: the name, field 2 (lobby ID), field 3 (lobby server)
and **field 11**, the lobby type. It stores the type in the lobby entry
(`+0x30`), which `GetCurrentLobbyType` (`0x004f79d0`) returns. NewLobby
re-joins a lobby on its own only when a join request (23) is pending, and
only if the record's ID, server and type match the current lobby's.

The lobby entry constructor (`0x004f9600`) stores field 2 at `+0x24`,
field 3 at `+0x28`, fields 12 and 13 at `+0x2c`/`+0x2e` and field 11 at
`+0x30`. No C++ code compares the type with a constant; only the script
natives `GetLobbyType` and `GetCurrentLobbyType` (`0x00507620`) read it.

**Retail run 2.** With one parent lobby of type 0, the lobby screen showed
a single lobby, labelled Adversarial, and no Skirmish lobby. Tubisoft now
lists two parent lobbies: group 1, "Earned in Blood", type 0, and group 2,
"Earned in Blood Skirmish", type 1. Retail run 3 confirmed type 1: the
Skirmish lobby appeared, could be joined and led to the skirmish game
creation screen, which differs from the Adversarial one. Each player belongs to the parent lobby it joined
(operation 23): refreshes (9), member events (50/51), room listings and
room updates stay within that lobby, and a room must name its creator's
lobby as its parent. Friend locations report the friend's own lobby.
Every other title keeps a single lobby, group 1.

## Room master (operation 59)

The op-59 handler (`0x004d2ee0`) reads `[u32 room ID, string (16 bytes),
string (0x90 bytes), string]` through `0x004cb710` and calls lobby-callback
slot `+0x48`, MasterChanged (`0x004ff450`, "LobbyRcv_MasterChanged(%d, %d):
%s"). New strings say that guests leave when the master leaves ("Room master
%s left the room, I will leave the room too"). Tubisoft does not send
operation 59: when the owner leaves, the room is removed (operation 55) as
in Road to Hill 30. **Retail (run 15):** with a guest still in the room,
the host left; the guest got a prompt that the host had left and, after
accepting it, left the room too, with no errors. Operation 59 is not
needed.

## Match results

`SubmitMatchResults` (`0x004f4af0`) has two branches, chosen by the game
mode (`+0x4e8`: 0 for versus, else the skirmish mission map).

- **Versus**: Road to Hill 30's eight values (field IDs 0-7: STATUS, KILLS,
  DEATHS, FIRED, HITS, TIME, TEAM, TYPE).
- **Skirmish**: eleven values, field IDs 0-10, sent through
  `LobbySend_SetMatchResult` (`0x004dade0`). All eleven are always sent;
  values the mission type does not use are 0.

| ID | Skirmish field | Sent by |
| --- | --- | --- |
| 0 | WIN_STATUS_SKR (1 win, -1 loss, 0) | every type |
| 1 | MAP_ID_SKR (set by the scripts, see run 6) | every type |
| 2 | SIDE_SKR (0 Allies) | every type |
| 3-5 | KILLS_SKR, OBJECTIVES_SKR (-1 for the host in run 7), SURVIVORS_SKR | Objective, Tour of Duty |
| 6 | TIME_SKR | Timed Assault |
| 7-9 | HITS_SKR, FIRED_SKR, DEF_KILLS_SKR | Defense |
| 10 | MAPS_REACHED_SKR | Tour of Duty |

The mission type is `(game mode - 1) / 10` (subsystem `+0x4e8`): 0
Objective, 1 Timed Assault, 2 Defense, 3 Tour of Duty. The game mode is not
part of the results. Skirmish results are submitted for Ladder matches
only (retail runs 4 and 5).

Operation 30 entries keep Road to Hill 30's form (name, highest field ID,
field count, mask, values by field ID). The profile accepts eight versus or
eleven skirmish values, requires every player in one report to name the
same map, and treats any other shape as unsupported (the router logs its
shape). Results are recorded when the owner ends the round (operation 45),
as in Road to Hill 30.

**Final statistics (operation 71).** `LobbyRcv_FinalMatchResults`
(`0x00500440`) reads thirteen values per player when the game mode is 0,
in Road to Hill 30's order. Otherwise it reads twelve: WIN_STATUS (wins),
PLAYED, WIN_RATIO, P_ALLIES, P_AXIS, COMPLETION, AVG_COMPLETION, TIME,
HITS, FIRED, ACCURACY, DEF_KILLS. Any other count is logged as unknown and
ignored. Tubisoft sends a skirmish player's totals on the board for the map
just played (Pending: whether the game expects the overall board instead).

## Ladder

The ladder request (`0x004fb3d0`) sends the product, then two numbers,
which become query fields 2 and 3:

| Board | Fields 2, 3 | Column table |
| --- | --- | --- |
| Versus | 0, 0 | top list `0x0061e110` (GLOBAL_RANK, ALIAS, SCORE, RANKED_WINS, RANKED_RNDS); player information `0x0061df80` (18 columns) |
| Skirmish, all missions | 1, 0 | `0x0061e020` (identity, WINS, PLAYED, W_RATIO, P_ALLIES, P_AXIS); player information `0x0061dfd0` (17 columns) |
| Skirmish, one map | 1, map ID | Objective and Tour of Duty `0x0061e050` (adds COMPLETION, AVG_COMPLETION); Timed Assault `0x0061e080` (adds TIME); Defense `0x0061e0b0` (adds HITS, FIRED, ACCURACY, DEF_KILLS) |

The identity columns are QUERY_RANK, GLOBAL_RANK, ALIAS, COUNTRY and
COUNTRY_ID. COUNTRY is new; Tubisoft sends it as 0, like COUNTRY_ID.
Query fields 2 and 3 are inferred from the request's arguments and Road to
Hill 30's query layout (Pending, retail run).

Tubisoft keeps the versus totals in their own table (`eib_ladder`) with
Road to Hill 30's SCORE rule (3 points per Ladder win, 1 per other Ladder
match). Skirmish results are stored per account and game mode
(`eib_skirmish`), the operation-17 value that the board request also uses.
As in Road to Hill 30, every reply carries every column of its board, the
requested ones first, and every cell except ALIAS is an integer.

Ubisoft's server computed the skirmish columns. The client only displays
them. Tubisoft's own rules, chosen by the project owner after retail runs
7-9:

- WINS and W_RATIO are 0: both players send WIN_STATUS 1 for every
  finished round, won or lost, so a win cannot be told apart (counting
  completed rounds showed wins nobody had, run 11). PLAYED counts rounds
  reported.
- P_ALLIES and P_AXIS are rounds played on each side.
- COMPLETION is the best single-round completion: maps reached (only Tour
  of Duty sends them) or else objectives completed (Objective).
  AVG_COMPLETION is the average per round, rounded down.
- TIME is the longest TIME_SKR, the seconds the player survived (only
  Timed Assault sends it; run 9).
- HITS, FIRED and DEF_KILLS are totals; ACCURACY is hits x 100 / fired.
- A mission's board (game mode 1-40) ranks by its own statistic:
  Objective and Tour of Duty by COMPLETION, Timed Assault by TIME, Defense
  by DEF_KILLS. The overall board ranks by PLAYED. Ties go to PLAYED, then
  the name. The overall board adds up
  the maps, with the best COMPLETION and the longest TIME.

## IRC chat

The chat client (`0x005dd000`-`0x005e1000`) speaks RFC 1459 lines inside
GS packets. Tubisoft's service is `ubisoft.irc`, on its own TCP port.

- **Framing.** Each packet holds one line: a 2-byte big-endian length
  (`0x0043aaf0`), then a GS Blowfish payload (zero-padded to eight bytes,
  ECB, 2-byte little-endian length trailer, as XIII's). The cipher is
  selector 0 of the GS crypto factory (`0x0049f550`; Blowfish tables at
  `0x0061c6b0`) with a **fixed 16-byte key** at `0x00670220`, created by
  `0x005e01c0`; there is no key exchange. The sender (`0x005e03b0`) builds
  `CMD a b :last\r\n` (`0x005dff80`, command names by index at
  `0x005dfd70`). The reader (`0x005e08a0`) decrypts and splits on CRLF,
  copying only the trailer's length with no terminator, so server packets
  end with a NUL.
- **Connect.** The Tick (`0x004ed060`) takes the first `IRCIP`/`IRCPort`
  entry ("Connect chat server: %s:%d", `0x005ddb00`). Runs 1-12 had no
  entry and never connected.
- **Login** (`0x005ddb60` -> `0x005e0b30`): no PASS, `NICK :<IRCID>` (own
  0x13 field 0, `+0x20c`) and `USER <SDK version> +i 0 :<username>`.
- **Replies.** Handlers are registered in the constructor (`0x005dddd0`):
  numeric 001 (`0x005de900`) sends `MODE <nick> :+i` and calls the game's
  welcome (`0x00502750`, state 4), after which the Tick joins
  `#LobbyGrp<lobby>.<lobby server>` (`0x005ddc00`, name `0x005df3a0`).
  Also registered: 301/305/306 (away), 353 (names), JOIN (`0x005de9c0`,
  which only logs, `0x005026b0`), PART, QUIT, ERROR, AUTH and PRIVMSG.
  The parser (`0x005e0470`/`0x005e06d0`) cuts the prefix at `!` and
  answers PING with PONG itself.
- **Invitations.** IRCSendPrivateMessage (`0x004f84c0` -> `0x005ddcf0`)
  formats `%s%%%d%%%d%%%d%%%d%%%s%%%s` (`0x005de2b0`) and sends `PRIVMSG
  <friend's IRCID>`; it returns without sending when there is no chat
  connection (`gp[0x6afea8]` null). The PRIVMSG handler (`0x005de460`)
  parses `%[^%]%%%d%%%d%%%d%%%d%%%[^%]%%` and needs all six fields; with
  its own nick as the target it calls the game's
  ChatRcv_SendPrivateMessage (`0x00502540`); `#GrpChnId`, `#LobbyGrp` and
  `#PrvGroup` targets go to other callbacks, which the game leaves unset.
- The GS library's own chat family (`CHAT`, `CHATALL`, `PAGER`,
  `PEERMSG` near `0x006ae030`) is separate.

Tubisoft's rules: a login is accepted only for a player signed in to the
friends service (operation 0x4e) from the same address, with that
player's own chat ID; a newer login replaces an older one. Every later
command is checked again, and when the friends session ends (sign-out or
a dropped connection) the server sends ERROR and closes that player's chat
connection; the game QUITs chat before it closes its friends session. It sends 001,
ignores MODE and PASS, echoes JOIN and PART to the channel's members,
answers PING, delivers PRIVMSG to a signed-in player's nick (dropped when
the recipient ignores the sender; 401 when offline) or to the members of
a channel the sender is in, and passes QUIT on to channel members. Limits:
510-character lines, 400-character messages, four channels per player, 256
players, 30 lines in 10 seconds, PING after 120 idle seconds and close
after another 120. Logs carry sizes and outcomes only, never names, chat
IDs or text.

## Voice

Headset voice (`bVoiceChatFlag`, `VoiceChannel`) appears to be in-game
Unreal traffic between peers. No server work is expected.

## Retail runs

1. **Account creation (one PCSX2 client).** Bootstrap with
   `dp=BIAEIBPS2US`, the key exchange and account creation reached the
   service. The name already existed, and the missing reply left the game
   waiting. Fixed by the GSFAIL reply above.
2. **Sign-in and lobby (one client, new account).** Login, the wait module,
   NAT discovery, discovery (21), player info, friends, notices, the lobby
   server login, the parent join (23), player data (42), the timed refresh
   (9 every half second) and leaving (8) all worked. No chat server
   connection was attempted in the log. Only an Adversarial lobby was
   listed; see "Lobby type".
3. **Skirmish lobby.** The type-1 lobby appeared as Skirmish and could be
   joined (operation 23 with group 2). Creating a skirmish game sent
   operation 12 with parent 2, a two-player limit and 0x50 bytes of session
   info, and the room was created. The game then asked the wait connection
   for the address of lobby server 2 (operation 3) and dropped its lobby
   connection. The creation reply's third value is the lobby server ID:
   CreateRoomReply (`0x004fc780`) passes it to LobbySend_JoinRoom
   (`0x004c70c0`), which picks the lobby server connection by it
   (`0x004d7d70`). Tubisoft sent the parent group ID there, which Road to
   Hill 30 never noticed because its parent was always 1. It now always
   sends 1.
4. **Skirmish matches (two PCSX2 clients).** A skirmish room was created,
   joined by the second PC and played twice: room updates (33, 31), the
   start (17, then 62 to both players), the players' reports (44, 34) and
   the end (70, 45, 35) all arrived. The owner never sent results
   (operation 30), so no final statistics (71) followed and the stats
   screen stayed empty. A skirmish ladder query (selection 1, the skirmish
   player-information columns) got the empty table. SubmitMatchResults
   (`0x004f4af0`) is called only by the script native
   `execSubmitMatchResults`; it sends nothing when the match-started flag
   (`+0x5a0`, set by MatchStarted `0x004ff7d0` on operation 62, cleared by
   FinishMatch) is clear. The matches were Friendly (see run 5).
5. **Ladder skirmish (two PCSX2 clients).** The owner sent operation 30
   about 80 seconds into the match: `[match, "0", entry, entry]`, 141
   bytes, which fits two entries of eleven values (Road to Hill 30's
   eight-value form would be about 25 bytes shorter). So skirmish results
   are submitted for Ladder matches only. Tubisoft rejected the report
   with a ValueError (a value outside `SKIRMISH_LIMITS`; the log did not
   say which), which closed the owner's connection and the room. Results
   that fail validation are now acknowledged, logged with their shape
   (numbers shown, names by length) and not kept, and the room stays open.
   Pending: the values, from the next server log or the game's debug
   output (printf at `0x00122bd0`, "SUBMITTED_*_SKR").
6. **Ladder skirmish again.** Same result: operation 30 refused, room
   closed. The service had not been rebuilt with the change above (the
   "match results rejected" line is missing). Both account names had 7
   characters and the frame was 139 bytes; encoding the report shows that
   21 of the 22 values were single digits and one had two characters, so
   TIME was 0 and a single-digit value was out of range. SetMapID
   (`0x004f48b0`) stores the scripts' number unchecked (result entry
   `+0x68`), so MAP_ID need not follow the game mode (1-40). Tubisoft now
   accepts MAP_ID 0-255 and WIN_STATUS -1 to 9 (only 1 is a win), and the
   ladder no longer derives the mission type from MAP_ID.
7. **Ladder skirmish, values logged.** Two won Objective matches on map 1.
   The reports were `[1, 1, 0, 2, -1, 4, 0, 0, 0, 0, 0]` (host) and
   `[1, 1, 0, 0, 0, 2, 0, 0, 0, 0, 0]` (guest), then
   `[1, 1, 0, 0, -1, 4, ...]` and `[1, 1, 0, 0, 0, 4, ...]`: WIN_STATUS 1,
   MAP_ID 1, SIDE 0, kills, **OBJECTIVES -1 for the host** and survivors.
   The -1 was the refused value in runs 5-7. OBJECTIVES now accepts -1,
   counted as 0. The rejected reports were acknowledged and the room stayed
   open for a second match. The skirmish ladder screens queried board 0
   (top list and around me, the 10-column table); the log now also shows
   query field 2 (0 versus, 1 skirmish).
8. **Ladder Timed Assault skirmish, recorded.** Operation 17 started the
   match with value 11, the game mode (StartMatch `0x004ef8c0` logs "Mode
   is %d" from `+0x4e8`; the Objective matches of run 7 sent 1). The
   results were accepted and recorded, and both players got operation 71:
   `[1, 1, 0, 0, 0, 0, 334, 0, ...]` (host) and
   `[1, 1, 0, 0, 0, 0, 562, 0, ...]` (guest). MAP_ID was 1 again, so it does
   not follow the game mode; skirmish boards are now keyed by the game mode
   from operation 17 (MAP_ID only without one). TIME differs per player
   and both values exceed the 281 seconds between operations 17 and 30, so
   it is not the match's elapsed time. The stats screen appeared on both
   PCs and showed 334 and 562. Both players had died, one before the
   other, yet both reported WIN_STATUS 1. The values fit the time left on
   a 10-minute countdown when each player died (deaths about 38 and 266
   seconds in, results at 281), but the timer starts at 15 minutes.
9. **Two more Ladder Timed Assault rounds.** The skirmish ladder screen
   asked for `kind=1`, board 0 (confirming the query layout) and got two
   rows. One round lasted 185 seconds from operation 17 to 30 and reported
   TIME 71 and 183: TIME is the seconds each player survived. Run 8's 562
   in a 282-second round remains unexplained. The tester confirmed that
   WIN_STATUS 1 marks a completed round, won or lost. The ladder rules
   below follow from these runs.
10. **Adversarial match, ladder screens, lobby counts, invitations.** A
    Ladder versus round (operation 17 game mode 0) sent Road to Hill 30's
    eight values and was recorded; Top 20 (kind 0, selection 3, table
    `0x0061e110`) displayed correctly. My Rank is QueryPlayerInfo
    (`0x004fb7e0`): kind 0, selection 1 `[name, 1]`, the eight columns of
    `0x0061e0f0`; the reply handler (`0x00500980`, branch at `0x0050134c`)
    reads GLOBAL_RANK, SCORE, ACCURACY, RANKED_WINS, RANKED_RNDS,
    FRIEND_WINS, FRIEND_RNDS and ALIAS by name into `m_PlayerRankResult`.
    The tester reported My Rank as wrong; replaying the round through the
    ladder gives consistent values (Pending: what the screen showed).
    **Lobby player counts** did not drop when a player left. The lobby list
    screen refreshes both lobbies (operation 9, flags 0x40) every two
    seconds, and operation 53's handler (`0x004d0cc0`) reads the group's
    own record only with flag 0x40: a record whose field 0 is 0 goes to
    lobby-callback slot +0x2c (`0x004fe060`), which updates the listed
    lobby's player count, limit and type. Tubisoft always answered with
    flags 0x100 (children only), so the counts from sign-in stayed. Earned
    in Blood's refresh replies now echo the request's 0x40 and 0x100.
    **Invitations:** "invite to lobby" sent two 35-character friend messages
    (0x58), shown as chat, and no invitation. InviteFriend (`0x004f0b70`)
    sends a 16-byte peer message (0x5b, sender `0x004c25d0`: name, length,
    data) with the room and lobby server; the receiver's
    FriendsRcv_PeerMsg (`0x00502060`, lobby-callback slot +0xdc) treats 16
    bytes as an invitation and 24 as a chat request. Tubisoft does not
    relay 0x5b yet, and matchmaking invitations need the IRC server.
11. **My Rank (one PC).** The leaderboard's My Rank is RequestAroundMe
    (`0x004fb570`), passed the current game mode: -1 builds the versus
    query (0, 0), 0 the overall skirmish board (1, 0), 1-40 a mission's
    board. After an Adversarial match the game mode is 0, so the
    Adversarial My Rank asked for (1, 0) with the skirmish columns, exactly
    like the Skirmish overall My Rank; Top 20 passes -1. The around-me
    handler reads GLOBAL_RANK, SCORE, RANKED_WINS, RANKED_RNDS and ALIAS by
    name for every list, which the skirmish reply lacked, so the scores
    were wrong. Every ladder reply now carries every column (versus and
    skirmish totals), and an around-me list on board 0 follows the versus
    order over everyone who has played either kind; top lists on board 0
    keep the skirmish order. The tester also saw three wins for three
    rounds none of which was won; WINS is now 0.
12. **Join Friend, lobby counts, lobby invitations.** Joining a friend's
    game from the friends list worked (operation 106, then 23 and 24), and
    the lobby list counts followed players leaving. The game has no way to
    invite a friend from inside a game room. A lobby invitation again sent
    one 35-character friend message (0x58), shown as chat. The receiver
    files pages as messages (FriendsRcv_Page `0x00501ef0` -> `0x004faed0`,
    event 0x4b); invitations come only from FriendsRcv_PeerMsg (event 0x5b,
    `0x004fb150`). The lobby invitation itself is an IRC private message:
    IRCSendPrivateMessage (`0x004f84c0`) calls the IRC library
    (`0x005ddcf0`), which formats `%s%%%d%%%d%%%d%%%d%%%s%%%s`
    (`0x005de2b0`) and sends PRIVMSG, and returns without sending when
    there is no chat connection (`gp[0x6afea8]` null). The receiver parses
    it (`0x005de460`) into its matchmaking invitations. Added the chat
    service (see "IRC chat"); the bootstrap now names it, and player info
    field 0 carries a chat ID (Pending: a retail invitation).
13. **Chat logins.** Both PCs fetched the new bootstrap, connected to the
    chat server and logged in (NICK/USER, 001) right after the friends
    login; the PC that entered a lobby joined its `#LobbyGrp` channel, and
    both quit and logged in again cleanly on a later sign-in. A lobby
    invitation from the friends list (inviter in a lobby, friend at the
    lobby list) again sent only a friend message (0x58, 35 characters):
    no PRIVMSG, no 0x13 lookup of the friend and no 0x5b. The 0x5b sender
    (`0x004c25d0`) uses the same wait-module connection as the page sender
    (`0x004c2420`), so InviteFriend was not called. Pages are filed as
    messages only (FriendsRcv_Page `0x00501ef0` -> `0x004faed0`, event
    0x4b; natives GetPageCount/GetPageMessage/GetPageSenderName). The
    scripts that choose between PageFriend, InviteFriend and
    IRCSendPrivateMessage are not in `MP.SOZ`.

    The disc settles it (**Static**, from `SYSTEM/LINEAR/MPMENU.LIN`, the
    multiplayer menu packages, and `SYSTEM/PSX2SHIP.UMD`, the localized
    text). The lobby menu (`ps2_LobbyMenu`) has INVITE TO LOBBY
    (`strInviteLobby`, `LCT_InviteToLobby`), which pages the friend
    `strInviteULB`, " invites you to join his/her lobby.": 35 characters,
    the page size of every run. **A lobby invitation is a page by design;**
    the friend follows with JOIN LOBBY / Join Friend (operation 106), which
    works. There is also `strInviteUGame` ("... join his/her game."). The
    menu packages name PageFriend, JoinFriend, GetPage*, GetInvitation*,
    GetTargetIRCID, IRCSendPrivateMessage and
    GetMatchmakingInvitation*/`strInvMsg` (" invites you to join his/her
    game (Skirmish)."), next to `gbx_SkirmishLobbyServerOnline`, but never
    InviteFriend or AcceptInvitation: the 0x5b invitation is unused. The IRC
    invitation is a Skirmish game invitation.
14. **Skirmish game invitation over chat (Retail).** With PC2 hosting a
    Skirmish game and PC1 in the Skirmish lobby, PC2 selected PC1 in the
    lobby's player list. Each press sent a 0x13 lookup (field 0, the chat
    ID) and then a PRIVMSG with seven `%`-separated fields, which the
    server delivered; PC1 showed "<inviter> invites you to join his/her
    game (Skirmish)." The receiver's callback (`0x00502540`) ignores the
    four numbers: unless slot +0x274 says otherwise, it looks in the
    lobby's room list (`+0x490`, entries built by `0x004f9680`: +0 room ID,
    +8 the room record's owner name, +0x74 an invited flag) for the room
    whose owner is the inviter, sets that flag, and files the message
    (`0x004fb010`). PC1 had PC2's room listed (it joined lobby 2 with one
    active room). PC1's Skirmish game list then showed PC2's game details
    highlighted in purple: the invited flag. Joining a Skirmish game from
    the list already worked in runs 4-9.
15. **Defense and Tour of Duty.** Two Ladder Defense rounds (game mode
    21) and one Tour of Duty round (game mode 31) were recorded with no
    refusal. Defense reports kills in DEF_KILLS (ID 9: 0/0, then 5/4) with
    KILLS and TIME at 0, so its board's statistic is right. The Tour of
    Duty report had only the host's entry: KILLS 3, OBJECTIVES -1,
    SURVIVORS 4, MAPS_REACHED 0, TIME 0, so its COMPLETION was 0. The
    host died first and the guest last, which ended the round, so the
    host's report left out a guest who played to the end. Only the host
    sends operation 30, and Tubisoft records what it sends: the guest gets
    no round. An Objective start (game mode 1) was left
    by both players within 11 seconds, with no results.

Suggested next runs: sign-in only (watch for IRC connects and new
wait-module messages); a lobby join with two PCs (member events, pings both
ways, lobby type); a versus room through results, Play Next Game and the
host leaving; a two-player skirmish through results and the ladder screens;
friends and invitations.
