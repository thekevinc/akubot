# Security model

Akubot is a local text-generation bot. The default runtime has no hosted AI API, no browsing, no executable model tools, and no automatic training. Its local model receives the current addressed message, up to two truncated exchanges from the same server/channel/user, the owner-approved persona, and up to four owner-reviewed style examples.

## Data boundaries

- The bot uses Discord's Gateway with guild/message events but no privileged Message Content, Presence, or Member intents. Unmentioned message events may contain metadata; they are discarded before generation. DMs, bots, and webhooks are ignored. It does not fetch historical messages or referenced messages, ingest attachments, or scrape channels.
- Enabled server and channel IDs are checked in application code before inference. Conversation keys include server, channel, and user IDs. Context from different people in a channel is deliberately separate.
- Live conversations are RAM-only. A sweep removes expired sessions every minute, with a 15-minute idle TTL, a 256-session cap, and two truncated exchanges per session. Restarting removes all live memory. Pause and forget invalidate in-flight generation so it cannot repopulate cleared context.
- The SQLite file contains reviewed style examples and paused-server flags. Every loaded example must be safe for all enabled servers. Private server-specific knowledge is not supported in this version. Do not load a sensitive message and rely on a prompt to hide it.
- Source exports are mounted only into an offline tools container. Channel selection and review confirmation are required. Prepared files retain only the redacted text and source channel ID, which is rechecked when loading; the example database stores only reviewed text. Basic redaction is assistance, not a guarantee.
- NSFW source channels are blocked during preparation and loading using a private snapshot of Discord channel metadata, no older than 24 hours. Age-restricted channels, NSFW-named channels/categories, and unknown status are excluded. Export NSFW flags are also respected. The metadata helper makes read-only application/channel requests directly to Discord, without fetching messages. A current snapshot cannot recover all historical channel settings or prevent a status change after the snapshot; refresh immediately before import and review the selected sources.
- The setup defaults place credentials and data outside the repository and OneDrive. The default private folder must not be synchronized by another cloud-storage tool. Git ignore rules are an additional guard, not encryption.

## Container and model boundaries

- The bot and tools run as an unprivileged user with dropped capabilities, a read-only root filesystem, no new privileges, bounded processes, and memory limits. Only the bot has the Discord token. No Docker socket or broad host directory is mounted.
- Ollama runs as an unprivileged user on an internal network with cloud features disabled. Its model directory is read-only. There are no published ports. Model downloading is a separate explicit container with no private conversation or credential mounts.
- The bot's model URL is restricted to local service/loopback names, redirects are rejected, and proxy environment variables are not used for model requests. There is no fallback AI provider. The bot needs internet access to Discord; this is not a host-level outbound-domain firewall against arbitrary compromised code.
- The model receives no Discord credentials, numeric author identities, or host access tools. Automatic mass mentions are disabled at the Discord API boundary. Generated links do not produce embeds.
- The model is untrusted text generation. Prompt injection can influence replies. The application controls permissions, memory scope, tools, and data availability independently of the model. An attacker might still elicit owner-approved examples or facts, so keep the entire shared corpus non-sensitive.

## Operational boundaries

- Limits are one active generation, at most four accepted pending requests, a 15-second user cooldown, and 12 accepted requests per server per minute. Requests time out. These limits are local to one bot process.
- Application logs omit message bodies, prompts, responses, raw exception text, and tokens. Third-party Discord logging is suppressed. Ollama's runtime container logging is disabled. Operating-system administrators, debugging tools, host memory/swap, and Discord itself remain outside these guarantees.
- A cloud migration requires trusting the hosting provider with the running machine. Containers do not protect against a malicious host administrator. Encrypt backups with a key held separately and remove backups when deleting data.
- Local Compose secrets are protected host files, not an encrypted secret vault. Setup matches Linux container UID/GID to the account creating private files. Windows relies on the private user's filesystem permissions. Verify those permissions on a shared machine.
- A private GitHub repository should contain only source code, example configuration, and synthetic test fixtures. Never commit `.env`, bot tokens, data exports, reviewed personal examples, models, databases, or backups.
- Reset a leaked bot token in the Discord Developer Portal and rerun setup. Git history and synced copies require separate remediation if a credential or private message was committed elsewhere.

This version has not been tested against a live Discord application or a real model on the target computer until the owner completes local setup. There is no claim of a formal security audit.

