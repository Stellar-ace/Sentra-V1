# Sentra Discord Bot

A powerful, modular Discord community bot built with Discord.py 2.6+, SQLAlchemy, and modern async architecture.

---

## 🚀 Easy Startup Guide

### 1. Local / VPS Setup

1. **Install requirements**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure your bot**:
   - If `.env` does not exist, copy `.env.example` to `.env`:
     ```bash
     cp .env.example .env
     ```
   - Open `.env` and set your `DISCORD_TOKEN`:
     ```env
     DISCORD_TOKEN=your_bot_token_here
     ```
     *(Default database is SQLite `sentra.db` — no extra database setup required!)*

3. **Start the bot**:
   ```bash
   python main.py
   ```

---

### 2. Pterodactyl Panel Setup

Running Sentra on a Pterodactyl game / bot hosting server is plug-and-play:

1. **Upload Files**:
   - Upload the project files to your Pterodactyl server file manager (or clone via Git).
2. **Pterodactyl Configuration**:
   - Under the **Startup** tab:
     - **Main File**: `main.py`
     - **Startup Command**: `python main.py` (or `python -u main.py`)
     - **Requirements File**: `requirements.txt`
3. **Environment Variables**:
   - Open `.env` in the file manager and set:
     ```env
     DISCORD_TOKEN=your_bot_token_here
     ```
     *(Alternatively, set `DISCORD_TOKEN` in Pterodactyl's Environment Variables if supported by your egg).*
4. **Start the Server**:
   - Click **Start** in the Pterodactyl console.

---

### 3. Windows (One-Click Setup)

- **First time setup**: Double-click `install.bat` (automatically installs dependencies).
- **Run the bot**: Double-click `start.bat` or run `python main.py` in PowerShell / Command Prompt.

---

### 4. Docker / Docker Compose

```bash
# Build and run with docker compose
docker compose up -d
```
