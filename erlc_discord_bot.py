"""
erlc_discord_bot.py
====================

This module implements a proof‑of‑concept integration between Emergency
Response: Liberty County (ERLC), Discord and Roblox.  The goal of this
project is to demonstrate how a single bot can tie the three
ecosystems together while remaining modular, easy to extend and secure.

Key design considerations
-------------------------

* **Asynchronous from the ground up.**  Both Discord and the ERLC API
  are IO bound.  Using `asyncio` primitives allows us to poll game
  data, fetch HTTP resources and interact with Discord concurrently.
* **Environment based configuration.**  All secrets (Discord bot token,
  ERLC server key, Roblox group IDs, etc.) should be supplied via
  environment variables.  This prevents accidental leaks in the code
  base and makes deployment straightforward in containerised
  environments.
* **Separation of concerns.**  API clients for ERLC and Roblox are
  isolated from the bot logic.  Similarly, persistent state (e.g.
  linked Roblox accounts and shift logs) is abstracted behind a simple
  SQLite database wrapper.

Please note that this script is provided as a starting point.  It
shows how to wire up the various APIs but does not attempt to cover
every nuance of your requirements.  For example, the web dashboard is
stubbed out and will require further development, and some
administrative commands assume you have appropriate Discord
permissions configured.

Before running this script you must install the required third party
packages:

```
pip install discord.py aiohttp aiosqlite fastapi uvicorn python‑dotenv
```

You will also need to create the following environment variables:

* **DISCORD_TOKEN:** The token for your Discord bot.
* **ERLC_SERVER_KEY:** The server key for your ERLC private server
  (see the Melonly documentation for how to obtain this key【525087916313152†L83-L114】).
* **ERLC_SERVER_ID:** The numeric identifier of your ERLC server.
* **DISCORD_GUILD_ID:** The guild (server) ID where the bot will
  operate.
* **ROLE_TEAM_MAP:** A JSON mapping from Discord role names to ERLC
  team names.  Users without the appropriate role will be prevented
  from joining that team.  Example: `{ "Police": "police", "Sheriff": "sheriff" }`.
* **ROBLOX_GROUP_ID:** The numeric Roblox group ID used for
  membership checks.

Optional variables:

* **JOIN_LOG_CHANNEL_ID**, **LEAVE_LOG_CHANNEL_ID**, **KILL_LOG_CHANNEL_ID**,
  **MOD_LOG_CHANNEL_ID** – the IDs of Discord channels where the
  corresponding ERLC events should be reported.

Once configured you can run the bot with:

```
python erlc_discord_bot.py
```

Copyright 2025 David.
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import aiosqlite
import discord
from discord import Intents, Member, Role
from discord.ext import commands, tasks
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn


###############################################################################
# Database helpers
###############################################################################

# By default we use a local SQLite database file.  You can override the
# location via the `ERLC_BOT_DB` environment variable.
DB_PATH = os.environ.get("ERLC_BOT_DB", "erlc_bot.db")


async def init_db() -> None:
    """Initialise the SQLite database.

    Creates tables for linked accounts and shift logs if they do not already
    exist.  Linked accounts map a Discord user ID to a Roblox user ID.  Shift
    logs track when a member begins or ends their shift.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS linked_accounts (
                discord_id TEXT PRIMARY KEY,
                roblox_id TEXT NOT NULL,
                roblox_username TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS shift_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id TEXT NOT NULL,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP
            )
            """
        )
        await db.commit()


async def link_account(discord_id: int, roblox_id: int, roblox_username: str) -> None:
    """Link a Discord user ID to a Roblox ID.

    If the entry already exists it will be replaced.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "REPLACE INTO linked_accounts (discord_id, roblox_id, roblox_username) VALUES (?, ?, ?)",
            (str(discord_id), str(roblox_id), roblox_username),
        )
        await db.commit()


async def get_linked_account(discord_id: int) -> Optional[Tuple[str, str]]:
    """Retrieve the Roblox ID and username for a given Discord user.

    Returns a tuple `(roblox_id, roblox_username)` or None if the user is not
    linked.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT roblox_id, roblox_username FROM linked_accounts WHERE discord_id = ?",
            (str(discord_id),),
        ) as cursor:
            row = await cursor.fetchone()
            return (row[0], row[1]) if row else None


async def start_shift(discord_id: int) -> None:
    """Record the start of a shift for a Discord user."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO shift_logs (discord_id, start_time, end_time) VALUES (?, ?, NULL)",
            (str(discord_id), datetime.utcnow()),
        )
        await db.commit()


async def end_shift(discord_id: int) -> None:
    """Record the end of a shift for a Discord user.

    This will update the most recent open shift for the user.  If no open shift
    exists then a new one is created with both start and end times equal.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM shift_logs WHERE discord_id = ? AND end_time IS NULL ORDER BY start_time DESC LIMIT 1",
            (str(discord_id),),
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            shift_id = row[0]
            await db.execute(
                "UPDATE shift_logs SET end_time = ? WHERE id = ?",
                (datetime.utcnow(), shift_id),
            )
        else:
            # Create a zero‑length shift as a fallback
            await db.execute(
                "INSERT INTO shift_logs (discord_id, start_time, end_time) VALUES (?, ?, ?)",
                (str(discord_id), datetime.utcnow(), datetime.utcnow()),
            )
        await db.commit()


###############################################################################
# ERLC API client
###############################################################################

class ERLCClient:
    """Simple wrapper around the ERLC API.

    According to the ER:LC API Pack documentation, endpoints such as
    `/server/players`, `/server/vehicles`, `/server/joinlogs` and
    `/server/killlogs` are available【57767659559315†L221-L258】.  This client exposes
    methods for those endpoints and handles authentication headers.
    """

    BASE_URL = "https://api.policeroleplay.community"

    def __init__(self, server_id: str, server_key: str, session: aiohttp.ClientSession) -> None:
        self.server_id = server_id
        self.server_key = server_key
        self.session = session

    def _headers(self) -> Dict[str, str]:
        return {
            "Server-Key": self.server_key,
            "Accept": "application/json",
        }

    async def _get(self, endpoint: str) -> Any:
        url = f"{self.BASE_URL}/servers/{self.server_id}{endpoint}"
        async with self.session.get(url, headers=self._headers()) as resp:
            if resp.status != 200:
                logging.error("ERLC API error on %s: HTTP %s", endpoint, resp.status)
                return None
            try:
                return await resp.json()
            except Exception:
                logging.exception("Failed to decode JSON from ERLC API")
                return None

    async def server_info(self) -> Optional[Dict[str, Any]]:
        return await self._get("")

    async def players(self) -> Optional[List[Dict[str, Any]]]:
        data = await self._get("/players")
        return data.get("players") if data else None

    async def vehicles(self) -> Optional[List[Dict[str, Any]]]:
        data = await self._get("/vehicles")
        return data.get("vehicles") if data else None

    async def join_logs(self) -> Optional[List[Dict[str, Any]]]:
        data = await self._get("/joinlogs")
        return data.get("logs") if data else None

    async def kill_logs(self) -> Optional[List[Dict[str, Any]]]:
        data = await self._get("/killlogs")
        return data.get("logs") if data else None

    async def run_command(self, command: str) -> bool:
        """Execute a command on the ERLC server.

        The `/server/command` endpoint accepts a POST with a JSON body
        containing the command.  According to the API pack documentation this
        endpoint will return success or failure【57767659559315†L292-L299】.
        """
        url = f"{self.BASE_URL}/servers/{self.server_id}/command"
        payload = {"command": command}
        async with self.session.post(url, json=payload, headers=self._headers()) as resp:
            if resp.status != 200:
                logging.error("Failed to run command '%s': HTTP %s", command, resp.status)
                return False


###############################################################################
# Roblox API client
###############################################################################

class RobloxClient:
    """Utility for interacting with Roblox public APIs.

    The developer forum notes that `Player:GetRankInGroup` and
    `Player:GetRoleInGroup` can be used in game scripts to query group
    membership【695876452586593†L84-L100】.  When interacting externally we can
    leverage the publicly accessible web API to fetch user group roles.

    The endpoint used here follows the pattern:

        https://groups.roblox.com/v1/users/{user_id}/groups/roles

    which returns a list of the user's groups and roles.  Note that this
    endpoint does not require authentication but is rate limited.  If you
    receive HTTP 429 responses you may need to back off.
    """

    GROUP_ROLES_URL = "https://groups.roblox.com/v1/users/{user_id}/groups/roles"
    USER_BY_USERNAME_URL = "https://api.roblox.com/users/get-by-username?username={username}"

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    async def get_user_id(self, username: str) -> Optional[int]:
        """Resolve a Roblox username to a user ID.

        Returns None if the user does not exist.
        """
        url = self.USER_BY_USERNAME_URL.format(username=username)
        async with self.session.get(url) as resp:
            if resp.status != 200:
                logging.error("Roblox username lookup failed: HTTP %s", resp.status)
                return None
            try:
                data = await resp.json()
            except Exception:
                logging.exception("Failed to parse Roblox username response")
                return None
            return data.get("Id")

    async def get_group_role(self, user_id: int, group_id: int) -> Optional[Dict[str, Any]]:
        """Return the user's role information within the specified group.

        If the user is not in the group, returns None.
        """
        url = self.GROUP_ROLES_URL.format(user_id=user_id)
        async with self.session.get(url) as resp:
            if resp.status != 200:
                logging.error("Roblox group roles lookup failed: HTTP %s", resp.status)
                return None
            try:
                data = await resp.json()
            except Exception:
                logging.exception("Failed to parse Roblox group roles response")
                return None
            groups = data.get("data", [])
            for group in groups:
                if group.get("group", {}).get("id") == group_id:
                    return group
            return None


###############################################################################
# Discord bot
###############################################################################

class ERLCDiscordBot(commands.Bot):
    """Main bot class tying together ERLC, Discord and Roblox integrations."""

    def __init__(self) -> None:
        # Configure intents: we need access to member information and message content
        intents = Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

        # Read mandatory environment variables
        try:
            self.guild_id = int(os.environ["DISCORD_GUILD_ID"])
            self.erlc_server_id = os.environ["ERLC_SERVER_ID"]
            self.erlc_server_key = os.environ["ERLC_SERVER_KEY"]
        except KeyError as exc:
            raise RuntimeError(f"Missing required environment variable: {exc}")
        # Optional configuration
        self.roblox_group_id = int(os.environ.get("ROBLOX_GROUP_ID", "0"))
        self.role_team_map: Dict[str, str] = json.loads(os.environ.get("ROLE_TEAM_MAP", "{}"))
        self.join_channel_id = int(os.environ.get("JOIN_LOG_CHANNEL_ID", 0))
        self.leave_channel_id = int(os.environ.get("LEAVE_LOG_CHANNEL_ID", 0))
        self.kill_channel_id = int(os.environ.get("KILL_LOG_CHANNEL_ID", 0))
        self.mod_channel_id = int(os.environ.get("MOD_LOG_CHANNEL_ID", 0))

        # HTTP session used by both API clients
        self.http_session: aiohttp.ClientSession = aiohttp.ClientSession()
        self.erlc_client = ERLCClient(
            server_id=self.erlc_server_id,
            server_key=self.erlc_server_key,
            session=self.http_session,
        )
        self.roblox_client = RobloxClient(session=self.http_session)

        # Maintain last processed timestamps
        self.last_join_log_time: Optional[datetime] = None
        self.last_kill_log_time: Optional[datetime] = None

    async def setup_hook(self) -> None:
        """Called automatically by discord.py when the bot is ready to set up."""
        # Initialise persistent storage
        await init_db()
        # Sync slash commands to the guild to avoid global propagation delay
        guild_obj = discord.Object(id=self.guild_id)
        await self.tree.sync(guild=guild_obj)
        # Start background loop for polling ERLC logs
        self.poll_erlc_logs.start()

    async def close(self) -> None:
        """Clean up tasks and resources on shutdown."""
        self.poll_erlc_logs.cancel()
        await self.http_session.close()
        await super().close()

    ###########################################################################
    # Slash commands
    ###########################################################################

    @discord.app_commands.command(name="link", description="Link your Roblox account to your Discord account")
    async def link(self, interaction: discord.Interaction, username: str) -> None:
        """Link the invoking Discord user to a Roblox username.

        Resolves the username to a Roblox ID, stores it in the database, and
        optionally reports the user's rank within the configured Roblox group.
        """
        await interaction.response.defer(ephemeral=True)
        roblox_id = await self.roblox_client.get_user_id(username)
        if not roblox_id:
            await interaction.followup.send(
                f"Roblox user '{username}' not found.",
                ephemeral=True,
            )
            return
        # Persist the link
        await link_account(interaction.user.id, roblox_id, username)
        # Optionally check the user's role within the Roblox group
        msg = f"Successfully linked your Discord account to Roblox user **{username}** (ID: {roblox_id})."
        if self.roblox_group_id:
            role_info = await self.roblox_client.get_group_role(roblox_id, self.roblox_group_id)
            if role_info:
                group_name = role_info.get("group", {}).get("name", "Unknown Group")
                role_name = role_info.get("role", {}).get("name", "Member")
                msg += f" You hold the role **{role_name}** in the Roblox group **{group_name}**."
        await interaction.followup.send(msg, ephemeral=True)

    @discord.app_commands.command(name="shift_start", description="Start your shift and notify the bot")
    async def shift_start(self, interaction: discord.Interaction) -> None:
        """Record the start of a shift for the invoking user."""
        await interaction.response.defer(ephemeral=True)
        await start_shift(interaction.user.id)
        await interaction.followup.send("Your shift has been started.",
                                        ephemeral=True)

    @discord.app_commands.command(name="shift_end", description="End your shift and notify the bot")
    async def shift_end(self, interaction: discord.Interaction) -> None:
        """Record the end of a shift for the invoking user."""
        await interaction.response.defer(ephemeral=True)
        await end_shift(interaction.user.id)
        await interaction.followup.send("Your shift has been ended.",
                                        ephemeral=True)

    ###########################################################################
    # Background tasks
    ###########################################################################

    @tasks.loop(seconds=30)
    async def poll_erlc_logs(self) -> None:
        """Poll the ERLC server for join/leave and kill logs and dispatch them to Discord."""
        # Join logs
        try:
            join_logs = await self.erlc_client.join_logs()
            if join_logs is not None:
                await self._handle_join_logs(join_logs)
            kill_logs = await self.erlc_client.kill_logs()
            if kill_logs is not None:
                await self._handle_kill_logs(kill_logs)
        except Exception:
            logging.exception("Error while polling ERLC logs")

    @poll_erlc_logs.before_loop
    async def before_poll(self) -> None:
        await self.wait_until_ready()

    async def _handle_join_logs(self, logs: List[Dict[str, Any]]) -> None:
        """Process join and leave logs from ERLC."""
        sorted_logs = sorted(logs, key=lambda l: l.get("timestamp", 0))
        for log in sorted_logs:
            ts = datetime.fromtimestamp(log.get("timestamp", 0))
            if self.last_join_log_time and ts <= self.last_join_log_time:
                continue
            self.last_join_log_time = ts
            username = log.get("username")
            user_id = str(log.get("id"))
            event_type = log.get("type")  # "join" or "leave"
            if event_type == "join" and self.join_channel_id:
                channel = self.get_channel(self.join_channel_id)
                if channel:
                    await channel.send(
                        f"**{username}** (ID {user_id}) joined the server at {ts.isoformat()}."
                    )
                # Enforce team restrictions for the new player
                await self._enforce_team_restrictions(username, user_id)
            elif event_type == "leave" and self.leave_channel_id:
                channel = self.get_channel(self.leave_channel_id)
                if channel:
                    await channel.send(
                        f"**{username}** (ID {user_id}) left the server at {ts.isoformat()}."
                    )

    async def _handle_kill_logs(self, logs: List[Dict[str, Any]]) -> None:
        """Process kill logs from ERLC."""
        sorted_logs = sorted(logs, key=lambda l: l.get("timestamp", 0))
        for log in sorted_logs:
            ts = datetime.fromtimestamp(log.get("timestamp", 0))
            if self.last_kill_log_time and ts <= self.last_kill_log_time:
                continue
            self.last_kill_log_time = ts
            killer = log.get("killer_username")
            victim = log.get("killed_username")
            if self.kill_channel_id:
                channel = self.get_channel(self.kill_channel_id)
                if channel:
                    await channel.send(
                        f"**{killer}** eliminated **{victim}** at {ts.isoformat()}."
                    )

    async def _enforce_team_restrictions(self, username: str, roblox_id: str) -> None:
        """Ensure that a player is on an authorised team based on their Discord roles."""
        # Find the Discord member linked to this Roblox account
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT discord_id FROM linked_accounts WHERE roblox_id = ?",
                (str(roblox_id),),
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return
                discord_id = int(row[0])
        guild = self.get_guild(self.guild_id)
        if not guild:
            return
        member = guild.get_member(discord_id)
        if not member:
            return
        # Determine player's current team
        players = await self.erlc_client.players()
        current_team = None
        if players:
            for p in players:
                if str(p.get("id")) == str(roblox_id):
                    current_team = p.get("team")
                    break
        if not current_team:
            return
        # Check mapping
        for role_name, team_name in self.role_team_map.items():
            if team_name.lower() == current_team.lower():
                has_required_role = any(r.name == role_name for r in member.roles)
                if not has_required_role:
                    # Inform moderators
                    if self.mod_channel_id:
                        mod_channel = self.get_channel(self.mod_channel_id)
                        if mod_channel:
                            await mod_channel.send(
                                f"⚠️ **{member.display_name}** attempted to join team **{current_team}** "
                                f"without possessing the required Discord role **{role_name}**."
                            )
                    # Attempt to move the user back to the civilian team
                    await self.erlc_client.run_command(f"team {username} civilian")
                break  # Only apply the first matching restriction


###############################################################################
# FastAPI dashboard
###############################################################################

app = FastAPI()


@app.get("/api/shifts")
async def get_shifts() -> JSONResponse:
    """Expose shift logs via a simple JSON API for external dashboards."""
    shifts: List[Dict[str, Any]] = []
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT discord_id, start_time, end_time FROM shift_logs ORDER BY start_time DESC"
        ) as cursor:
            async for discord_id, start_time, end_time in cursor:
                shifts.append(
                    {
                        "discord_id": discord_id,
                        "start_time": start_time,
                        "end_time": end_time,
                    }
                )
    return JSONResponse(content=shifts)


def main() -> None:
    """Entry point to run both the Discord bot and the API server."""
    logging.basicConfig(level=logging.INFO)
    bot = ERLCDiscordBot()

    async def runner() -> None:
        # Run the API and the bot concurrently
        config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
        server = uvicorn.Server(config=config)
        api_task = asyncio.create_task(server.serve())
        discord_task = asyncio.create_task(bot.start(os.environ["DISCORD_TOKEN"]))
        done, pending = await asyncio.wait(
            [api_task, discord_task], return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()

    asyncio.run(runner())


if __name__ == "__main__":
    main()