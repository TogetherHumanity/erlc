# ERLC Discord Bot

This repository contains a Python bot that integrates **Emergency Response: Liberty County (ER:LC)** with **Discord** and **Roblox**.  It polls the ER:LC Private Server API for real‑time events, synchronizes player roles across Discord and Roblox groups, logs join/leave and kill events, and provides slash commands for shift management.

The bot is designed to be easily deployed on cloud hosting platforms such as Replit, Railway, Render or Heroku.  All configuration is handled through environment variables, so you don’t need to modify the code to get started.

## Features

* **Account linking:** Players can link their Discord account to a Roblox username via a slash command.  The bot resolves the username to a Roblox user ID and, if configured, displays the player’s role or rank in your Roblox group.
* **Real‑time logs:** A background task polls the ER:LC API endpoints for join/leave logs and kill logs, posting them to designated Discord channels.  Timestamps are tracked to avoid duplicate announcements.
* **Role‑based team locking:** Define a mapping of Discord roles to ER:LC teams.  When a player joins a restricted team without the proper role, the bot warns staff and attempts to move the player back to the civilian team.
* **Shift management:** Users can start and end shifts through slash commands.  Shifts are stored in a SQLite database and exposed via a simple REST endpoint for dashboard integration.
* **Extensible API:** A built‑in FastAPI server runs alongside the bot, providing access to internal data (e.g., shift logs) for a front‑end dashboard.  Use this as a foundation for a CAD/MDT system or further customizations.

## Getting started

Follow these steps to deploy the bot on a cloud host without installing anything locally:

1. **Clone this repository**

   Fork it to your own GitHub account or download a ZIP and upload it to the platform of your choice.  Many hosts let you import directly from GitHub.

2. **Configure environment variables**

   Create a copy of `.env.example` named `.env` (or use the host’s environment variable settings) and fill in the values:

   * `DISCORD_TOKEN` – Your Discord bot token.  You must create a bot in the [Discord Developer Portal](https://discord.com/developers/applications) and copy its token.
   * `ERLC_SERVER_KEY` – The ER:LC API server key from your private server settings.  Keep this secret.
   * `ERLC_SERVER_ID` – Numeric ID of your ER:LC private server.
   * `DISCORD_GUILD_ID` – The ID of your Discord server.  Enable Developer Mode in Discord settings to copy it.
   * `ROLE_TEAM_MAP` – A JSON mapping of Discord roles to ER:LC teams (e.g., `{ "Police Officer": "police", "Sheriff Deputy": "sheriff" }`).
   * `ROBLOX_GROUP_ID` – (Optional) Your Roblox group ID if you want to display group roles.
   * Channel IDs for logging (`JOIN_LOG_CHANNEL_ID`, `LEAVE_LOG_CHANNEL_ID`, `KILL_LOG_CHANNEL_ID`) and moderation notifications (`MOD_LOG_CHANNEL_ID`).

   See `.env.example` for all available variables.

3. **Deploy to a hosting platform**

   Platforms like [Replit](https://replit.com), [Railway](https://railway.app), [Render](https://render.com) or [Heroku](https://www.heroku.com) can run persistent Python processes:

   * Import this repository.
   * Add the environment variables from your `.env` file into the host’s configuration UI.
   * Set the run command to:
     ```bash
     python erlc_discord_bot.py
     ```
   * Ensure port `8000` is exposed if you want to access the FastAPI endpoints.

4. **Invite the bot to your server**

   Generate an OAuth2 invite link from the Discord Developer Portal with the `bot` and `applications.commands` scopes.  Grant the bot permissions to read and send messages in the log channels, and to manage roles if you plan to enforce team restrictions.

5. **Use the bot**

   * `/link <roblox username>` – Link your Discord user to a Roblox username.  The bot stores the association and optionally reports the player’s group rank.
   * `/shift_start` – Start a shift.  Your Discord ID is logged in the database.
   * `/shift_end` – End your current shift.  The bot records the end time.
   * Watch the designated channels for join/leave and kill logs.

## Extending the bot

Feel free to extend the FastAPI server with additional endpoints or the Discord bot with more commands.  The script is modular, with separate classes for ER:LC API access, Roblox API access, and Discord bot logic.  For example, you can:

* Log vehicle spawns or chat messages by polling additional ER:LC endpoints.
* Build a front‑end dashboard (React/Vue) that consumes the `/api/shifts` endpoint and displays shift statistics.
* Integrate a full CAD/MDT system by adding database models and endpoints inspired by existing solutions like SnailyCAD.

## License

This project is provided as‑is without warranty.  You are responsible for securing your ER:LC API key and Discord bot token.