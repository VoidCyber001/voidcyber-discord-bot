# VoidBot — VoidCyber Discord Bot

Discord bot for the [VoidCyber](https://voidcyber.net) cybersecurity community.
Features a hacking mini-game with a bits economy, rotating shop, PvP, NPC servers, and Discord account linking.

## Commands

### Account Linking
| Command | Description |
|---|---|
| `/link <code>` | Link your VoidCyber account using a one-time code from your profile |
| `/unlink` | Unlink your VoidCyber account and remove your roles |
| `/rank` | View your VoidCyber rank and XP progress |
| `/leaderboard` | Top 20 players by XP or CTF points |

### Hacking Game
| Command | Description |
|---|---|
| `/daily-reward` | Claim daily bits — streak grows the reward (10 → 100 bits/day) |
| `/missions` | View and claim your 3 daily missions |
| `/targets` | View all NPC servers and your cooldowns |
| `/breach <target>` | Hack an NPC server for bits |
| `/hack @user` | Attempt to steal bits from another player |
| `/transfer @user <amount>` | Send bits to another player |
| `/balance [@user]` | Check your bits or another player's balance |
| `/coinflip <amount>` | Bet bits on a coin flip |

### Loadout
| Command | Description |
|---|---|
| `/inventory` | View your bits, equipped items, and bag |
| `/equip <type>` | Equip a firewall or attack tool |
| `/unequip <type>` | Unequip your current firewall or attack tool |
| `/sell` | Sell an item for 75% of its price |
| `/info [@user]` | View anyone's stats and loadout |

### Admin
| Command | Description |
|---|---|
| `/add-bits @user <amount>` | Give bits to a player |
| `/remove-bits @user <amount>` | Remove bits from a player |

### Other
| Command | Description |
|---|---|
| `/help` | Full game guide |

## Setup

See [SETUP.md](SETUP.md) for full setup instructions.

## Stack

- Python 3.11+
- [discord.py](https://github.com/Rapptz/discord.py)
- Game data stored locally in `game_data.json`
- Account linking via VoidCyber REST API (Cloudflare D1 + Vercel)

## License

MIT
