# Data directory

Runtime data is stored here. Most files are gitignored.

- **chat_sessions.db** — LangChain chat message history (session history). Conversation context is stored here and loaded for the agent.
- **assistant_background.txt** — Optional. Multiline assistant instructions. Set `assistant.background_path` to `data/assistant_background.txt` in config to use it; edit this file for long or multiline persona text.
