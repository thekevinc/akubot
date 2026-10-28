# Akubot

A Discord AI double that responds when someone **mentions the bot**, for example `@aku what do you think?`. The model runs on your machine. Your computer must stay awake while the bot is available.

This is an initial working implementation. Until you add your writing samples, replies use a generic casual persona. Model quality and speed need to be evaluated on your hardware.

## What stays private

- Live messages go from Discord to your bot and your local Ollama model. There is no external AI provider, analytics service, or hosted vector database.
- Ordinary channel history is not fetched. The bot responds only to explicit mentions in approved servers and optional approved channels. It ignores DMs, webhooks, and other bots.
- Short conversation memory is separate for each server, channel, and person. It lives in RAM, expires after 15 minutes of inactivity, and is swept every minute. At most two truncated exchanges per conversation and 256 conversations are retained.
- Only examples you explicitly review and load are stored permanently. Those examples must be suitable for everyone who can invoke the bot, because model instructions cannot guarantee that examples stay secret.
- Secrets, models, exports, reviewed examples, and the database are stored outside the checkout, outside OneDrive by default. Only code belongs in GitHub.

Read [SECURITY.md](SECURITY.md) for the security boundaries.

## 1. Create a dedicated Discord bot

1. Visit the [Discord Developer Portal](https://discord.com/developers/applications) and create a new application named **Akubot**.
2. Open **Bot**, configure its name/avatar, and obtain its **bot token**. A regular Discord account cannot be repurposed as a bot. Keep the token out of chat, GitHub, and screenshots.
3. Leave **Message Content**, **Server Members**, and **Presence** privileged intents disabled. Discord supplies the content of messages mentioning a bot without broad Message Content access.
4. Disable **Public Bot** if available, so other people cannot install it arbitrarily. Runtime server allowlists apply regardless.
5. In Discord, enable **User Settings > Advanced > Developer Mode**. Copy your personal **User ID** and each approved **Server ID**. Copy specific **Channel IDs** too if you want a channel allowlist.

The setup script generates an invitation link with View Channels, Send Messages, Read Message History (needed for Discord replies), and Send Messages in Threads. It does not request Administrator. The bot never uses the history permission to fetch surrounding messages. Nickname it **aku** in your server if desired.

For setup with fewer manual IDs, after creating the application run `python3 scripts/connect_discord.py --guild-id YOUR_SERVER_ID` from WSL. It prompts for the token with hidden input, discovers the application and its personal owner, and generates an invitation for that server. Open the invitation yourself. If the bot is not installed yet, rerun the same command with `--refresh` after installing it. On Windows use `py -3` instead of `python3`. This helper reads application and channel metadata only, never messages, and preserves a model selection already configured locally.

If you have an old bot application later, it can be reused with a fresh bot token. Stop any previous process using that application before starting this one.

## 2. Prepare your computer

Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and [Python](https://www.python.org/downloads/) if needed. On Windows, use Docker's WSL 2 backend. A reasonable starting point is at least 8 GB of system RAM, with enough memory assigned to Docker for the 6 GB model-service limit plus the bot and operating system. No GPU is required for the initial CPU setup; speed varies.

Keep the checkout outside OneDrive, for example `~/workspace/akubot` in WSL. Open a terminal in that folder and use the token helper above, or the manual-ID setup:

```powershell
py -3 scripts/setup.py
```

On Linux or macOS, use `python3 scripts/setup.py` instead. The script asks for application/user/server IDs and reads the bot token with hidden input. The token is written to a separate private file, never to `.env` or the image.

Default private storage:

| System | Location |
| --- | --- |
| Windows | `%LOCALAPPDATA%\Akubot` |
| Linux/WSL | `${XDG_DATA_HOME:-~/.local/share}/akubot` |
| macOS | `~/.local/share/akubot` |

An alternative location can be selected with `--private-dir`. Choose one that is not synchronized by a cloud-storage application. On Windows, keep the default folder's inherited private user permissions. Run setup and Compose consistently from the same Windows or WSL environment so the paths and user permissions match.

Use the generated invitation link to install the bot in each selected server.

## 3. Download the model, then start

```sh
docker compose run --rm model-download
docker compose up -d --build
docker compose run --rm --no-deps tools check
```

The first command downloads `llama3.2:3b`, roughly 2 GB. It is an explicit one-time download operation with internet access, no Discord token, and no message data mounted. The running model has a read-only model directory and an internal network with no external connection. Neither the bot nor model publishes a host port.

The check confirms the model is installed and reports how many reviewed style examples are loaded. Mention the bot in an approved channel to test it. Each follow-up should contain an explicit bot mention too. Simply typing the letters `@aku` without selecting the bot in Discord does not create a real mention.

Useful controls:

| Command | Access | Effect |
| --- | --- | --- |
| `/aku_forget` | Anyone in an approved channel | Clears that person's temporary conversation in that channel and invalidates in-flight replies |
| `/aku_pause` | Configured owner | Pauses the server, clears its temporary memory, and invalidates in-flight replies |
| `/aku_resume` | Configured owner | Resumes mention replies in that server |
| `/aku_status` | Configured owner | Shows the configured model, pause state, and example count |

Controls reply privately to the person invoking them. Owner authorization uses your numeric user ID, not your display name. Pause state survives restarts.

```sh
docker compose logs --tail=50 bot
docker compose down
```

The first command shows content-free operational messages. The second stops this project's containers without deleting private data. Change server/channel allowlists in `.env` and recreate this project's bot container to apply them. Unlisted servers are ignored even if someone installs the bot there.

### Optional NVIDIA acceleration

After validating [Docker's GPU support](https://docs.docker.com/desktop/features/gpu/), start with:

```sh
docker compose -f compose.yaml -f compose.gpu.yaml up -d --build
```

Use the same two `-f` arguments when stopping this configuration. The CPU version is the default because your available hardware has not been confirmed. The context window is 4,096 tokens and output is limited to 192 tokens; replace the model only after measuring style and latency.

## 4. Teach it your style

You can initially edit `config/persona.json` **inside your private storage folder**, not the template in the checkout. The model reads the private copy at startup. Add only public facts you want it to share. Restart this project's bot after editing that file.

For message examples, request your [official Discord data package](https://support.discord.com/hc/en-us/articles/360004957991-Your-Discord-Data-Package). Unzip it locally into `imports/discord-export` inside private storage. The importer supports the official outgoing-message JSON and older CSV layout. It does not scrape Discord, fetch attachments, or reconstruct missing conversation partners' messages.

**NSFW channels are excluded.** Before each import session, refresh the channel metadata using `python3 scripts/connect_discord.py --guild-id YOUR_SERVER_ID --refresh`. The resulting private channel policy expires after 24 hours. The importer rejects age-restricted channels, channels or parent categories named NSFW, and channels whose age-restriction status cannot be verified. Deleted channels, DMs, and unverified threads are excluded. An NSFW flag in the export also blocks import. Current Discord metadata cannot establish every historical channel setting, so review remains necessary.

List available channel IDs without displaying message content:

```sh
docker compose run --rm --no-deps tools inspect-export /imports/discord-export
```

Use the export's local index to identify channels. Select each source channel explicitly. Replace these example IDs with your own:

```sh
docker compose run --rm --no-deps tools prepare-export /imports/discord-export --owner-id 111111111111111111 --channel-id 222222222222222222 --output /review/candidates.jsonl
```

The script replaces some obvious emails, links, tokens, mentions, and number strings. **Automatic redaction cannot identify every secret or private story.** Open `review/candidates.jsonl` in private storage and manually remove names, confidential facts, quotes from friends, and anything unsuitable for every enabled server. Keep only the lines you approve in `review/approved.jsonl`, retaining each line's `text` and `source_channel_id` fields. Loading checks the channel policy again, so a newly excluded channel cannot enter the example database through an earlier candidate file. A few hundred varied, representative messages are a useful first evaluation set.

```sh
docker compose run --rm --no-deps tools load-examples /review/approved.jsonl --reviewed
docker compose run --rm --no-deps tools check
```

The running bot picks up loaded examples without restarting. The importer runs in a separate container without the Discord token or an internet connection. Live messages never become new examples automatically. To erase all examples:

```sh
docker compose run --rm --no-deps tools clear-examples
```

Remove the original export, review files, and any backups separately when no longer needed. This implementation uses retrieval of reviewed examples, with no fine-tuning or automatic training. Discord's [developer policy](https://support-dev.discord.com/hc/en-us/articles/8563934450327-Discord-Developer-Policy) restricts training on API-obtained message content without express permission.

## Tests and development

Python 3.10 or newer is supported for development. The container uses Python 3.12.

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

An optional GitHub Actions workflow is provided at `ci/github-actions.yml`. To enable it later, copy it to `.github/workflows/checks.yml` using a GitHub connection authorized to manage workflows. It runs the tests, validates Compose, builds the image, and checks its command entrypoint without starting a bot.

Tests use temporary files and fake model responses. They do not connect to Discord, download a model, or start a server. Python dependencies are pinned. Both container images are pinned by verified image digest, and build actions by commit. Check current security releases before updating these pins.

## Moving to remote hosting

Use the same Compose project on a Linux host. Rerun setup there to generate matching local permissions, securely transfer only the approved persona/examples if desired, and download the model on that host. Keep private storage outside the checkout, restrict SSH access, and never publish Ollama's port. Stop the old bot instance before switching. No hosting subscription or remote machine is provisioned by this repository.

