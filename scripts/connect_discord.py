"""Read a bot token privately and discover the non-secret Discord settings."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
from pathlib import Path
import sys
import urllib.error
import urllib.request
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from akubot.channel_policy import ChannelPolicy, snapshot
from setup import default_private_dir, initialize, validate_private_dir


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def discord_get(path: str, token: str):
    if path != "/oauth2/applications/@me" and not re.fullmatch(r"/guilds/[0-9]+/channels", path):
        raise ValueError("Invalid Discord metadata request.")
    request = urllib.request.Request("https://discord.com/api/v10" + path, headers={
        "Authorization": "Bot " + token,
        "User-Agent": "akubotSetup/0.1 (local private bot)",
    })
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=20) as response:
            data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise ValueError("Discord metadata response exceeded the setup limit.")
            return json.loads(data)
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise ValueError("Discord rejected the bot token. Use the bot token from the Developer Portal.") from None
        if error.code in {403, 404}:
            raise ValueError("Install the bot in the selected server using the invitation link, then rerun with --refresh.") from None
        raise ValueError("Discord metadata is temporarily unavailable. Try again later.") from None
    except (urllib.error.URLError, TimeoutError):
        raise ValueError("Could not connect directly to Discord. Check the network and try again.") from None


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure akubot using a hidden token prompt and Discord metadata only")
    parser.add_argument("--guild-id", required=True)
    parser.add_argument("--owner-id", help="Only needed if Discord does not return a personal application owner")
    parser.add_argument("--private-dir", type=Path, default=default_private_dir())
    parser.add_argument("--refresh", action="store_true", help="Reuse the saved token and refresh the channel policy")
    args = parser.parse_args()
    try:
        os.umask(0o077)
        if not args.guild_id.isdecimal() or int(args.guild_id) <= 0:
            raise ValueError("Provide the numeric server ID.")
        private = validate_private_dir(args.private_dir)
        token_file = private / "secrets" / "discord_token.txt"
        if args.refresh:
            token = token_file.read_text(encoding="utf-8").strip()
        else:
            token = getpass.getpass("Bot token (hidden; never sent to chat): ").strip()
        if not 30 <= len(token) <= 512 or any(char.isspace() for char in token):
            raise ValueError("The bot token format does not look right.")
        application = discord_get("/oauth2/applications/@me", token)
        if not isinstance(application, dict):
            raise ValueError("Discord returned invalid application metadata.")
        owner = application.get("owner")
        owner_id = args.owner_id or (owner.get("id") if isinstance(owner, dict) else None)
        if not isinstance(owner_id, str) or not owner_id.isdecimal() or int(owner_id) <= 0:
            raise ValueError("Could not identify the application owner. Rerun with --owner-id YOUR_USER_ID.")
        application_id = str(application.get("id", ""))
        if not application_id.isdecimal():
            raise ValueError("Discord did not return an application ID.")
        config_file = Path(__file__).resolve().parent.parent / ".env"
        saved = {}
        if config_file.is_file():
            for line in config_file.read_text(encoding="utf-8").splitlines():
                key, separator, value = line.partition("=")
                if separator:
                    saved[key] = value.strip().strip("'\"")
        if args.refresh:
            if args.guild_id not in saved.get("ALLOWED_GUILD_IDS", "").split(","):
                raise ValueError("The selected server is not enabled in .env. Configure it before refreshing channel metadata.")
        else:
            initialize(private, owner_id, args.guild_id, saved.get("ALLOWED_CHANNEL_IDS", ""), token)
        link = "https://discord.com/oauth2/authorize?" + urlencode({
            "client_id": application_id, "scope": "bot applications.commands", "permissions": "274877975552",
            "guild_id": args.guild_id, "disable_guild_select": "true",
        })
        print("Bot settings saved. If it is not installed yet, open this link:")
        print(link)
        channels = discord_get(f"/guilds/{args.guild_id}/channels", token)
        if not isinstance(channels, list):
            raise ValueError("Discord returned invalid channel metadata.")
        policy_file = private / "config" / "channel-policy.json"
        # Local server exclusions survive refreshing Discord's NSFW metadata.
        # Read them without applying the old snapshot's 24-hour expiry.
        exclusions = json.loads(policy_file.read_text(encoding="utf-8")).get("excluded_guild_ids", []) if policy_file.is_file() else []
        policy = snapshot(args.guild_id, channels, excluded_guild_ids=exclusions)
        verified_policy = ChannelPolicy(policy)
        policy_file.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
        policy_file.chmod(0o600)
        allowed = sum(verified_policy.allows(channel_id) for channel_id in policy["channels"])
        print(f"Verified channel metadata: {allowed} channels eligible for reviewed examples. NSFW, unknown, and excluded servers are blocked.")
        print("No messages were fetched. Refresh this policy before importing message examples; it expires after 24 hours.")
        return 0
    except ValueError as error:
        print(str(error))
        return 1
    except OSError:
        print("Setup could not access the private token or configuration files. Check their paths and permissions.")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

