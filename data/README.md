# Data directory

Runtime data is stored here. Most files are gitignored.

- **agno_sessions.db** — Agno session history and Learning data (User Profile + User Memory). What the agent knows about you is stored here and injected automatically.
- **assistant_background.txt** — Optional. Multiline assistant instructions. Set `assistant.background_path` to `data/assistant_background.txt` in config to use it; edit this file for long or multiline persona text.
