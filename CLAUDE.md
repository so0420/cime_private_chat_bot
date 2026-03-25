# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**cime_chat_bot** — a standalone chatbot for the ci.me (씨미) streaming platform. It connects to a streamer's chat via AWS IVS WebSocket, responds to custom commands, tracks attendance, and handles donation thank-you messages. It ships as a single-file Python app with a web-based admin dashboard, and can be packaged into a Windows `.exe` via PyInstaller.

The Korean language is used throughout the codebase (UI, comments, log messages, variable naming in some places). All user-facing text is in Korean.

## Architecture

### Single-file design (`cime_bot.py`)
Everything lives in one ~900-line file. It combines:
- **SQLite database** (via aiosqlite) — stores commands, attendance records, and settings in `bot.db`
- **WebSocket chat client** — connects to `wss://edge.ivschat.ap-northeast-2.amazonaws.com/` using a token from the ci.me API
- **aiohttp web server** (port 8000) — serves the admin dashboard and REST APIs
- **Selenium login** — automates Chrome to obtain `mauth-authorization-code` cookie from ci.me

### Key data flow
1. On startup: init DB → load saved settings → auto-login via Selenium → auto-start bot
2. Bot loop: get chat token → connect WebSocket → listen for messages → match commands from DB → reply with variable substitution
3. Web dashboard: CRUD commands, configure settings, start/stop bot, view attendance stats

### REST API endpoints (all under localhost:8000)
- `GET/POST /api/commands`, `PUT/DELETE /api/commands/{id}` — command CRUD
- `GET/PUT /api/settings` — bot settings
- `POST /api/login`, `POST /api/relogin` — Selenium-based authentication
- `GET /api/status` — bot connection status
- `POST /api/bot/start`, `POST /api/bot/stop` — bot lifecycle
- `GET /api/attendance` — attendance leaderboard

### Template variables
Commands support these placeholders: `<보낸사람>`, `<출석횟수>`, `<업타임>`, `<방제>`, `<카테고리>`, `<팔로우>`, `<받은금액>`

### Global state
A single `state` dict holds runtime state (running, connected, ws, cookie, channel_info, etc.). This is intentional for the single-process design.

## Parent directory context

The parent `cime/` directory contains earlier prototype scripts:
- `chat_receiver.py` — single-channel chat receiver/sender (interactive CLI, uses `requests` + `websockets`)
- `chat_bot.py` — multi-channel bot that polls all live channels and responds to commands from `commands.json`
- `chat_all.py` — multi-channel chat logger with HTTP API for log retrieval
- `login.py` — standalone mauth login script via marpple API

`cime_chat_bot/` is the current production version that supersedes these prototypes.

## Commands

### Install dependencies
```
pip install -r requirements.txt
```
Dependencies: `aiohttp`, `websockets`, `aiosqlite`, `selenium`

### Run the bot
```
python cime_bot.py
```
Opens the admin dashboard at http://localhost:8000 automatically.

### Build Windows executable
```
pyinstaller --noconfirm --onefile --icon "favicon.ico" --add-data "web;web" --collect-all selenium cime_bot.py
```
The `web/` directory is bundled as data. At runtime, `sys._MEIPASS` resolves bundled resources while `bot.db` is stored next to the `.exe`.

## Key Conventions

- ci.me API base: `https://ci.me/api/app`
- Auth cookie name: `mauth-authorization-code`
- Attendance resets at 5:00 AM KST daily
- Cookie lifetime: 24 hours (triggers re-login)
- Channel info refreshes every 60 seconds
- WebSocket ping every 30 seconds to keep connection alive
