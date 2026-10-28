# Security model

Akubot is a local text-generation bot. The default runtime has no hosted AI API, no browsing, no executable model tools, and no automatic training. Its local model receives the current addressed message, up to two truncated exchanges from the same server/channel/user, the owner-approved persona, and up to four owner-reviewed style examples.

## Data boundaries

- The bot uses Discord's Gateway with guild/message events but no privileged Message Content, Presence, or Member intents. Unmentioned message events may contain metadata; they are discarded before generation. DMs, bots, and webhooks are ignored. It does not fetch historical messages or referenced messages, ingest attachments, or scrape channels.
- Enabled server and channel IDs are checked in application code before inference. Conversation keys include server, channel, and user IDs. Context from different people in a channel is deliberately separate.
- Live conversations are RAM-only. A sweep removes expired sessions every minute, with a 15-minute idle TTL, a 256-session cap, and two truncated exchanges per session. Restarting removes all live memory. Pause and forget invalidate in-flight generation so it cannot repopulate cleared context.
- The SQLite file contains reviewed style examples, paused-server flags, and owner-edited dashboard settings/personality. Every loaded example and public fact must be safe for all enabled servers. Private server-specific knowledge is not supported in this version. Do not load a sensitive message and rely on a prompt to hide it.
- Source exports are mounted only into an offline tools container. Channel selection and review confirmation are required. Prepared files retain only the redacted text and source channel ID, which is rechecked when loading; the example database stores only reviewed text. Basic redaction is assistance, not a guarantee.
- NSFW source channels are blocked during preparation and loading using a private snapshot of Discord channel metadata, no older than 24 hours. Age-restricted channels, NSFW-named channels/categories, and unknown status are excluded. Export NSFW flags are also respected. The metadata helper makes read-only application/channel requests directly to Discord, without fetching messages. A current snapshot cannot recover all historical channel settings or prevent a status change after the snapshot; refresh immediately before import and review the selected sources.
- The setup defaults place credentials and data outside the repository and OneDrive. The default private folder must not be synchronized by another cloud-storage tool. Git ignore rules are an additional guard, not encryption.

## Container and model boundaries

- The bot and tools run as an unprivileged user with dropped capabilities, a read-only root filesystem, no new privileges, bounded processes, and memory limits. Only the bot has the Discord token. No Docker socket or broad host directory is mounted.
- Ollama runs as an unprivileged user on an internal network with cloud features disabled. Its model directory is read-only and its API has no published host port. Model downloading is a separate explicit container with no private conversation or credential mounts.
- The bot's model URL is restricted to local service/loopback names, redirects are rejected, and proxy environment variables are not used for model requests. There is no fallback AI provider. The bot needs internet access to Discord; this is not a host-level outbound-domain firewall against arbitrary compromised code.
- The model receives no Discord credentials, numeric author identities, or host access tools. Automatic mass mentions are disabled at the Discord API boundary. Generated links do not produce embeds.
- The model is untrusted text generation. Prompt injection can influence replies. The application controls permissions, memory scope, tools, and data availability independently of the model. An attacker might still elicit owner-approved examples or facts, so keep the entire shared corpus non-sensitive.

## Operational boundaries

- Limits are one active generation and at most four accepted pending requests. Dashboard-adjustable defaults are a two-second user cooldown and 30 accepted requests per server per minute. Requests time out. These limits are local to one bot process. Tests use the same bounded generation path; model preloads also take the generation lock.
- Application logs omit message bodies, prompts, responses, raw exception text, and tokens. Third-party Discord logging is suppressed. Ollama's runtime container logging is disabled. Operating-system administrators, debugging tools, host memory/swap, and Discord itself remain outside these guarantees.
- A cloud migration requires trusting the hosting provider with the running machine. Containers do not protect against a malicious host administrator. Encrypt backups with a key held separately and remove backups when deleting data.
- Local Compose secrets are protected host files, not an encrypted secret vault. Setup matches Linux container UID/GID to the account creating private files. Windows relies on the private user's filesystem permissions. Verify those permissions on a shared machine.
- A private GitHub repository should contain only source code, example configuration, and synthetic test fixtures. Never commit `.env`, bot tokens, data exports, reviewed personal examples, models, databases, or backups.
- Reset a leaked bot token in the Discord Developer Portal and rerun setup. Git history and synced copies require separate remediation if a credential or private message was committed elsewhere.

## Local dashboard

- The bot's HTTP dashboard binds within its container and is published as `127.0.0.1:8765:8787`. It is intended for trusted users/processes on this computer, not for access from a LAN or the internet. It has no separate password login and must not be exposed through an unauthenticated proxy or port-forward.
- All API reads and mutations require a custom local header. Foreign Host values, foreign/null Origin values, cross-site fetches, form content types, and preflight requests are rejected. No CORS access is granted. These checks address hostile browser pages and DNS rebinding; they do not stop an already trusted local process from controlling the bot.
- The UI has a restrictive Content Security Policy and uses only bundled scripts/styles. Reviewed text, word-cloud words, test replies, and settings are rendered with text nodes, never interpreted as HTML. No analytics, remote fonts, browser storage, or chat transcript collector is included. Access logs are disabled and responses use `Cache-Control: no-store`.
- Dashboard sample prompts/replies are transient, never added to the persistent example library or conversation memory. Recent activity retains at most 100 timing-only records in RAM, without author/channel IDs, message text, or model replies. Full counts are for the current process session. A preview reply is shown only in the requesting local browser.
- The database view exposes only reviewed examples with bound search parameters and bounded pagination. It is read-only, has no arbitrary SQL interface, and cannot upload examples or bypass the separate NSFW/review import workflow. Word counts are computed off the Discord event loop and cached; analysis is capped at the latest 100,000 reviewed examples.
- Runtime values are range-checked, non-finite values and cloud-model names are rejected, and model changes require an installed local model. Changes are persisted transactionally. A revision check prevents stale tabs from silently overwriting newer settings. Personality changes clear conversations and invalidate stale in-flight replies.

This is not a formal security audit. Live Discord/model behavior must be verified when starting a deployment; in-memory tests and offline UI tests do not establish live connectivity.

