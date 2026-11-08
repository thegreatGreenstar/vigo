# Letting other people use your Vigo

This is the walkthrough for one owner: your daemon keeps running on your
machines, and other people install the same Discord app on their own accounts.
They ask you for access, you click, and then they can ask questions. Nothing
runs for them before that click.

If each person should have their own machines and their own agent, they run
the normal setup in the [README](../README.md) with their own Discord
application and their own token. That is a different thing: this page is one
agent, several askers.

## What you need

- Vigo running on at least one device, with a settings file and a bot token.
- The application's **Application ID** (`appID` in the settings).
- Permission to change the app in <https://discord.com/developers/applications>.
- The bot token stays on your machine. Never paste it into Discord, an issue,
  a commit or a shell command that another person can read.

## Who is the owner

Vigo reads the owner from the Discord application itself, with
`GET /applications/@me`, when it starts. The Discord account that owns the app
is the only account that can use `status`, `queue`, `devices`, `stop`,
`upgrade`, `goal`, `allow`, `deny` and `allowed`, and the only one that can
approve a tool. You do not copy a user id for this.

That is also why another person can run their own application called Vigo.
Their app's owner is their account, so the owner commands answer to them. The
people they allow on their Vigo stay guests, with no owner commands. If a
settings file names `owner`, that id must be the application's owner or the
daemon stops, so a typed id cannot take over someone else's application.

## 1. The Developer Portal

Open <https://discord.com/developers/applications> and choose the Vigo
application. The pages below are also printed, with your own id filled in, by
`python3 -m vigo --invite`.

### Installation

Go to **Installation** (the URL is
`https://discord.com/developers/applications/<APP_ID>/installation`).

- **Installation Contexts**: turn **User Install** on and **Guild Install**
  off.
- **Install Link**: choose **Discord Provided Link**.
- **Default Install Settings** → **User Install**: add the
  `applications.commands` scope. Do not add `bot`: a user install needs no bot
  user in any server.
- Press **Save Changes**.

Guild Install off is what keeps a public app out of other people's servers.
The bot does not need to be in a server for a user install to work.

### Bot

Go to **Bot** (`https://discord.com/developers/applications/<APP_ID>/bot`).

- Turn **Public Bot** on. Discord only gives a shareable install link to a
  public app. With it off, only your team can install it.
- Leave every **Privileged Gateway Intents** switch off. Vigo asks for one
  intent, `DIRECT_MESSAGES`, and it is not privileged.

Public Bot does not put the bot in any server. It only lets other people
authorize the app on their own account.

### General Information

Go to **General Information**
(`https://discord.com/developers/applications/<APP_ID>/information`) and check
that **Application ID** is the `appID` in the settings file.

## 2. The settings file

The app offers only User Install now, so tell Vigo to register commands for
that context, and decide whether strangers get the request button:

```jsonc
{
  "install": "user",
  "requests": true
}
```

`requests: false` keeps the refusal but removes **Ask for access**. Leave it
`true` if you want other people to be able to ask you from Discord.

Restart the daemon so the commands are registered again:

```sh
systemctl --user restart vigo                        # Linux
launchctl kickstart -k gui/$(id -u)/dev.vigo.bot     # macOS
```

The allowlist is read from the owner's DM unless `stateChannel` names a
private channel. A request card shares that DM, so a state channel is the
quieter choice:

```jsonc
{ "stateChannel": "<a private channel id the bot can read>" }
```

## 3. Check it

From the checkout, with the settings file in place:

```sh
python3 -m vigo --invite
```

It reads the application from Discord and prints:

- the user install link to share
- who Discord says owns the application
- whether User Install, Guild Install, Public Bot and the
  `applications.commands` scope are on
- the exact Installation and Bot pages for your application.

It exits 0 only when the portal and the settings agree. If it names a page,
open it, change that one setting, save, and run the check again.

## 4. Share the link

The link is:

```
https://discord.com/oauth2/authorize?client_id=<APP_ID>&integration_type=1&scope=applications.commands
```

Send it to the person. They open it, choose **Add to my apps** (Discord may
call it **Add App**), and finish. `/vigo` then appears in their DMs with the
app, in group DMs, and in the command picker of every server they are in,
including servers the bot has never joined. They do not need to invite the bot
to a server.

A changed global command can take up to an hour to appear in Discord. The
first start after a change is usually a minute.

## 5. Approve the first request

The person runs `/vigo ask prompt:...`. Until you allow them they get a short
private refusal and one button, **Ask for access**.

1. They press **Ask for access**.
2. You get a DM from Vigo: "**Name** asks for access to Vigo." with three
   buttons: **Allow 1 day**, **Allow for good**, **Deny**.
3. Press one. Only the Discord account that owns the application can answer
   the card.
4. They get a DM with the decision. If you allowed them, `/vigo ask` works.

The request itself runs nothing. A pending request is sent once, a denial is
quiet for an hour, a request that waits a day lets the person ask again, and
at most twenty wait at once.

`/vigo allow user:@someone for:7d` still works and does the same thing without
the card. `/vigo allowed` lists who may ask, and `/vigo deny user:@someone`
takes an allow back.

## What an approved person can do

An allow is permission to ask, not to drive the machine:

- Questions only. Their ask never becomes a goal and never chooses a device.
- Their question and answer are private to them, in a session of their own.
- One question in flight at a time.
- They spend the share of your plan kept for approved people, a third by
  default, with hourly and daily caps.
- Every tool the agent asks for still goes to you as a card. An approved
  person's card offers **Allow once** and **Deny**, never **Always allow**.
- `/vigo status`, `devices`, `queue`, `stop`, `upgrade`, `goal`, `allow`,
  `deny` and `allowed` remain yours.

An allow is also a read grant: a question runs the agent under your account
and can read what that agent can read on that device. Allow only people you
would trust with the machine. See ["What an allow
means"](../SECURITY.md#what-an-allow-means).

## If something does not work

| What you see | What to do |
| --- | --- |
| The daemon stops saying the file's `owner` is not the application's | Remove `owner` from the settings, or set it to the application owner's id the message names. A typed id cannot take over another person's app. |
| The daemon stops saying the token belongs to another application | `appID` and `token` came from different apps. Copy the Application ID from the app whose token is in the file. |
| `--invite` exits 1 and says `Public Bot off` | Turn Public Bot on at the Bot page it printed, and set Install Link to **Discord Provided Link**. |
| `--invite` says User Install is off | Turn it on at the Installation page, add the `applications.commands` scope for User Install, and save. |
| `--invite` says the settings and the app disagree | For a user-install-only app set `"install": "user"`. For both contexts set `"install": "both"` and turn Guild Install on. |
| The person has no `/vigo` | They have not finished **Settings → Apps**, or the command has not reached their client yet (up to an hour). |
| No **Ask for access** button | `"requests"` is false, or an older daemon is answering. Set it true and restart. |
| The request says it cannot reach the owner's list | Set `stateChannel` to a private channel the bot can read, then try again. |
| No card arrives in your DM | Open a DM with the bot yourself, or set `stateChannel`, and ask the person to press the button again. |
| The person says `Only the owner of this Discord application...` on a card | Only the account that owns the app can answer the request card. That is the design. |

## A second person who wants their own agent

This app is one agent: your token, your daemon, your machines. To give someone
an agent of their own, they create their own Discord application, use their
own token and run the normal setup. Then their app is theirs to publish, and
the two installations do not share a token, a plan or a machine.
