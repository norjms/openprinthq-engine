# Chat agent: ask about and act on the farm from Slack or Discord

The engine can run a chat bot on Slack, Discord or both. People ask what is printing,
what failed, what is waiting and which printers have alarms, and, if their engine account
allows it, queue a library file, pause, resume or cancel a print, and take a printer out
of service or put it back.

The bot is part of the engine (`backend/app/services/chat_agent/`), not an outside
service. It is off until it is configured.

## How it decides what to do

- **Every chat account is linked to an engine user.** The bot acts as that user, with
  that user's permissions. A chat user who is not linked gets one reply only: how to link.
- **Every read and every action goes through the engine API as that user.** The bot
  calls the engine's own HTTP routes in-process (no network) with a two-minute token
  minted for the linked user, so the route permission checks, ownership filters
  (`*_own` / `*_all`) and the audit middleware apply exactly as they do for the web UI.
  Before calling, the bot also checks the user's group permissions itself, which gives a
  clear refusal and also holds when the engine runs with sign-in off (where the API
  itself checks nothing).
- **Only configured channels.** The bot answers in the channel IDs you list (and only in
  the listed Slack workspaces / Discord servers, when you list any). Direct messages are
  answered only when "Answer direct messages" is on. Anything else is ignored silently.
- **Cancel and out-of-service ask first.** The bot replies with a summary and Confirm /
  Cancel buttons (typing `yes` or `no` works too). Only the person who asked can confirm,
  and only within the confirmation window (default 120 seconds, 30 to 900). Permissions
  are checked again when the confirmation arrives. Queue, pause, resume and back in
  service run at once.
- **Everything is audit-logged.** See [Audit log](#audit-log).

## What it understands

| Say | Does | Needs |
|---|---|---|
| `status`, `what's printing?`, `status of X1C-1`, `how long until the voron is done` | State, job, progress, layer and time left for every printer you can see, or one | `printers:read` |
| `queue`, `what is waiting in the queue`, `what's waiting on X1C-1` | Pending queue items (yours only without `queue:read_all`) | `queue:read_all` or `queue:read_own` |
| `failures`, `what failed recently and why?` | The last five failed prints with the recorded reason | `archives:read_all` or `archives:read_own` |
| `alarms`, `any alarms on X1C-1?` | Active alarms from the alarm feed | `printers:read` |
| `queue benchy on X1C-1`, `print benchy on any P1S` | Adds the library file to the queue for that printer, or for any printer of that model | `queue:create`, library read, `printers:read` |
| `pause X1C-1`, `resume X1C-1` | Pauses / resumes the current print | `printers:control` |
| `cancel X1C-1` | Asks to confirm, then stops the current print | `printers:control` |
| `take X1C-1 out of service because nozzle clog` | Asks to confirm, then stops new jobs going to it (a running print continues) | `printers:update` |
| `put X1C-1 back in service` | Lets it take jobs again | `printers:update` |
| `link ABCD-EFGH` | Links this chat account (see below) | a code from the engine |
| `unlink` | Removes this chat account's link | linked |
| `help` | Lists the above | linked |

In channels, mention the bot (`@printfarm status`). In direct messages, just type.

Printer and file names are matched loosely: case, spaces and punctuation do not matter,
`#3` means printer id 3, a name that fits several printers gets a "which one?" reply, and
a small typo still finds the one close name. A printer named anywhere in a question
(`how is the X1C-1 doing`) narrows the answer to it.

Replies are in English.

### Language understanding: parser first, optional model

Messages are understood by a deterministic parser (`intents.py`): keyword and pattern
rules for the fixed set of intents above. Nothing leaves the engine and the behaviour is
testable.

Optionally, an OpenAI-compatible model (for example a local Ollama:
base URL `http://<ollama-host>:11434/v1`, a small instruct model) handles free-form
phrasing the parser does not recognise. It is asked only to choose one of the same
intents and to copy names out of the message, as JSON. It never calls the engine:
its answer goes through the same name matching, permission checks and confirmation
step as a parsed message, and anything outside the fixed set (including link, unlink and
confirm, which are never left to a model) is treated as "not understood". Messages the
parser does recognise never reach the model.

## Linking chat accounts

1. In the engine web UI open **Settings > Chat Agent** and press **Link a chat account**.
   It shows a code such as `link ABCD-EFGH`, valid for 10 minutes and usable once.
2. Send that to the bot (in an allowed channel, mentioning it, or in a direct message
   when those are allowed).
3. The bot answers "Linked. I will act as <user> for you."

Users with `users:update` can make a code for another engine user (the "Code for" list),
and with `users:read` see and remove everyone's links. Everybody can see and remove
their own. When the engine runs with sign-in off, pick the engine user in the list:
the bot still acts with that user's group permissions.

Only a SHA-256 of each code is stored. After five wrong codes in 15 minutes a chat
account must wait. Disabling an engine user stops the bot acting for every chat account
linked to it; deleting the user deletes the links.

## Setting up Slack

The engine uses **Socket Mode**: it connects out to Slack, so it needs no public URL.

1. Go to <https://api.slack.com/apps>, **Create New App > From an app manifest**, pick
   the workspace and paste:

   ```yaml
   display_information:
     name: Print Farm
   features:
     bot_user:
       display_name: printfarm
       always_online: true
     app_home:
       messages_tab_enabled: true
       messages_tab_read_only_enabled: false
   oauth_config:
     scopes:
       bot:
         - app_mentions:read
         - chat:write
         - im:history
         - im:read
         - im:write
   settings:
     event_subscriptions:
       bot_events:
         - app_mention
         - message.im
     interactivity:
       is_enabled: true
     socket_mode_enabled: true
   ```

   (By hand: enable Socket Mode; Event Subscriptions on with bot events `app_mention` and
   `message.im`; Interactivity on; bot scopes as listed.)
2. **Basic Information > App-Level Tokens > Generate Token and Scopes**: add the scope
   `connections:write`. Copy the `xapp-...` token. This is the **App-level token**.
3. **Install App > Install to Workspace**. Copy the **Bot User OAuth Token** (`xoxb-...`).
   This is the **Bot token**.
4. In Slack, invite the bot to each channel it should answer in: `/invite @printfarm`.
   The channel ID is at the bottom of the channel's details (starts with `C`, or `G` for
   older private channels).
5. Optional: the workspace ID (starts with `T`) to restrict the bot to one workspace.
6. In the engine: **Settings > Chat Agent**, turn on the agent and Slack, paste both
   tokens, list the channel IDs (comma-separated), optionally the workspace ID, **Save**.
   The Slack line shows "Connected" when Socket Mode is up.

## Setting up Discord

Use a Discord application of its own. A bot token can hold only one gateway connection,
so a token already used by another bot must not be reused here.

1. Go to <https://discord.com/developers/applications>, **New Application**.
2. **Bot**: **Reset Token** and copy it. This is the **Bot token**. Turn **Public Bot**
   off. No privileged gateway intents are needed: the bot reads only messages that
   mention it and direct messages, whose text Discord delivers without the Message
   Content intent.
3. **OAuth2 > URL Generator**: scope `bot`; bot permissions **View Channels**,
   **Send Messages**, **Read Message History** (and **Send Messages in Threads** if it
   should answer in threads). Open the generated URL and add the bot to your server.
4. In Discord, **User Settings > Advanced > Developer Mode** on. Right-click each channel
   the bot should answer in, **Copy Channel ID**; right-click the server, **Copy Server ID**
   (optional, to restrict to that server).
5. In the engine: **Settings > Chat Agent**, turn on the agent and Discord, paste the
   token, list the channel IDs, optionally the server ID, **Save**.

## Configuration reference

Saved in the settings table under `chat_agent_*` keys (`PUT /api/v1/chat-agent/config`,
needs `settings:update`; `GET` needs `settings:read`):

| Field | Meaning |
|---|---|
| `enabled` | Master switch. |
| `slack_enabled`, `discord_enabled` | Per platform. A platform starts only with its token(s) and at least one allowed channel (or DMs on). |
| `slack_allowed_channels`, `discord_allowed_channels` | Channel IDs the bot answers in. |
| `slack_allowed_workspaces`, `discord_allowed_guilds` | Optional; when set, messages from other workspaces / servers are ignored. |
| `allow_dms` | Answer direct messages from linked users. |
| `confirm_timeout_seconds` | Confirmation window, 30 to 900, default 120. |
| `llm_enabled`, `llm_base_url`, `llm_model` | Optional model for free-form phrasing. |
| `slack_bot_token`, `slack_app_token`, `discord_bot_token`, `llm_api_key` | Write-only. A value sets it, `""` clears it, leaving it out keeps it. `GET` returns only `<name>_set` and `<name>_from_env`. |

Tokens are encrypted at rest with the engine's secret key (the same mechanism as the
two-factor and OIDC secrets), never returned by the API, never logged, and left out of
the GitHub settings backup. Each can instead come from the environment, which wins over
the stored value:

- `OPHQ_CHAT_SLACK_BOT_TOKEN`, `OPHQ_CHAT_SLACK_APP_TOKEN`
- `OPHQ_CHAT_DISCORD_BOT_TOKEN`
- `OPHQ_CHAT_LLM_API_KEY`

Saving the configuration restarts the connections. Errors show on the settings page by
error class only (for example `SlackApiError`), never with the token.

Links: `GET /api/v1/chat-agent/links` (`?all=true` with `users:read`),
`DELETE /api/v1/chat-agent/links/{id}`, `POST /api/v1/chat-agent/link-codes`
(`{"user_id": n}` for someone else, needs `users:update`).

## Audit log

Every action, refusal and link change is written to the audit log
(`GET /api/v1/audit-log`) with `actor_kind` `chat`, `method` `CHAT`, the engine user as
`actor`, `route` `chat:<intent>` and `path` `<platform>:<chat user id> <summary>`.
Status: `200` done, `202` waiting for confirmation, `403` refused (missing permission),
`408` confirmation expired, `400` a wrong link code, or the API's own status when the
engine refused. The underlying API call is also logged by the audit middleware as the
engine user, like any web UI action. Reads are not audit-logged.

## Printer visibility

All printer lists, name lookups and per-printer reads in the chat agent start from one
function, `EngineApi.visible_printers()` in `services/chat_agent/engine_api.py`, which
reads `GET /api/v1/printers/` as the linked user and passes the result through
`filter_visible_printers(user, printers)`. Per-printer scoping in the API applies
automatically; `filter_visible_printers` is the place to add the scoping helper as a
second filter. Queue items, failures and alarms on printers the user cannot see are
dropped from replies.

## Limits

- Pending confirmations are kept in memory: an engine restart forgets them (nothing
  runs; the person asks again).
- One engine runs one connection per bot token. Do not point two engines at the same
  Slack app or Discord bot.
- Queueing needs a printer or "any <model>": a file is never queued without a target.
