# 🎫 Discord Ticket Bot

A fully-featured Discord ticket system built with **discord.py (slash commands + buttons)**.

This bot allows servers to manage support tickets with:
- ticket claiming
- staff-only workflows
- automatic transcript generation
- structured logging
- optional transcript saving to disk
- scalable data storage

Designed to be **clean, reliable, and production-ready**.

---

## ✨ Features

- 🎫 Ticket panel with button-based creation
- 🔢 Automatic ticket numbering (`ticket-0001`, `ticket-0002`, etc.)
- 🧑‍💼 Staff **claim / unclaim** system
- 🔒 Claimer-only ticket closing (admin override supported)
- 📄 Automatic transcript generation on close
- 💾 Optional transcript saving to disk (config toggle)
- 📬 Transcript sent to:
  - log channel
  - ticket opener via DM
- 📝 Structured **open** and **close summary** log embeds
- 🗂 JSON-based storage (no database required)

---

## 📦 Requirements

- Python **3.10+**
- A Discord bot token
- Required Python libraries:
  ```bash
  pip install discord.py chat-exporter
