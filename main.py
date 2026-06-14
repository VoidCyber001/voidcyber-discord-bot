"""
VoidBot — VoidCyber <-> Discord account-link bot.

Commands:
  /link <code>       Link your VoidCyber account (code from site profile)
  /unlink            Unlink your account and remove VoidCyber roles
  /rank              View your rank and XP progress
  /leaderboard       Top 20 — General (XP) or CTF board

Hacking mini-game (local economy stored in game_data.json):
  /info [@user]      View a player's stats and equipped items
  /inventory         View your inventory and equipped items
  /equip <type>      Equip a firewall or attack tool from your inventory
  /unequip <type>    Unequip your current firewall or attack tool
  /sell              Sell an item from your inventory (75% of original cost)
  /hack @user        Attempt to steal bits from a member (cooldown varies by tool)
  /daily-reward      Claim daily bits; streak grows the reward (10 → 100, cycles)
  /add-bits          [Admin] Give bits to a member
  /remove-bits       [Admin] Remove bits from a member

The shop rotates every 20 minutes in #void-game, showing 2 firewalls + 2 attack
tools with limited stock. Buy via the dropdown under the shop message.

Rank roles are kept in sync automatically by a background task (every 15 minutes),
plus instantly on /rank and /link.
"""

import os
import time
import json
import random
import asyncio
from datetime import datetime, timezone, timedelta

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

# ── Configuration ─────────────────────────────────────────────────────────────
load_dotenv()
TOKEN     = os.getenv("DISCORD_TOKEN")
API_URL   = os.getenv("VOIDCYBER_API_URL", "https://voidcyber.net").rstrip("/")
BOT_SECRET = os.getenv("DISCORD_BOT_SECRET", "")
GUILD_ID  = os.getenv("GUILD_ID")

# VoidCyber rank tier (1–10) → Discord role ID
RANK_ROLE_BY_TIER = {
    1:  1485238143438557371,  # Void Operator
    2:  1485238051373318144,  # Site Guard
    3:  1485237785773211738,  # Network Infiltrator
    4:  1485236881661628567,  # Void Sentinel
    5:  1485236450671988837,  # System Breacher
    6:  1485235980440047809,  # Void Specialist
    7:  1511120936160461003,  # Elite Operative
    8:  1511121077261303899,  # Void High Custodian
    9:  1511121147717091390,  # Grand Void Director
    10: 1511121261168951426,  # The Void Entity
}
ALL_RANK_ROLE_IDS = set(RANK_ROLE_BY_TIER.values())
LINKED_ROLE_ID = 1511123827550060704  # permanent "linked" role

SYNC_INTERVAL       = 15 * 60
LINK_ATTEMPT_LIMIT  = 5
LINK_ATTEMPT_WINDOW = 60
API_HEADERS = {"X-Discord-Bot-Secret": BOT_SECRET}

_link_attempts = {}

# ── Game configuration ────────────────────────────────────────────────────────
_DIR = os.path.dirname(os.path.abspath(__file__))
GAME_DATA_FILE  = os.path.join(_DIR, "game_data.json")
SHOP_STATE_FILE = os.path.join(_DIR, "shop_state.json")

SHOP_CHANNEL_ID       = 1486393749247492116
SHOP_REFRESH_MINUTES  = 20
SHOP_SLOTS_FW         = 2
SHOP_SLOTS_ATK        = 2
STARTING_BITS         = 500
HACK_COOLDOWN_DEFAULT = 30 * 60
HACK_FAIL_PENALTY     = 50
MIN_BLOCK             = 15   # innate block even with no firewall equipped
MAX_BLOCK             = 90

DAILY_BASE  = 10
DAILY_STEP  = 5
DAILY_CYCLE = 19

GAME_RATE_LIMIT  = 5
GAME_RATE_WINDOW = 60

# ── Tier metadata ─────────────────────────────────────────────────────────────
TIER_META = {
    1: {"name": "Script Kiddie", "color": 0x808080},
    2: {"name": "Grey Hat",      "color": 0x3498db},
    3: {"name": "Black Hat",     "color": 0x9b59b6},
    4: {"name": "APT",           "color": 0xe67e22},
    5: {"name": "Zero Day",      "color": 0xf1c40f},
    6: {"name": "Phantom",       "color": 0xe74c3c},
}

# Shop rarity weights — lower tier = more common
TIER_WEIGHTS = {1: 40, 2: 30, 3: 15, 4: 8, 5: 5, 6: 2}

# Emoji per tier — used in shop and inventory displays
TIER_EMOJI = {1: "⬜", 2: "🔵", 3: "🟣", 4: "🟠", 5: "🟡", 6: "🔴"}

# Firewall HP by tier — breaks permanently when HP hits 0 (must buy a new one)
FW_HP_BY_TIER = {1: 5, 2: 8, 3: 12, 4: 18, 5: 25, 6: 35}

# NPC server targets — hackable for bits, per-player cooldowns
NPC_TARGETS = [
    {"id": "home_router",  "name": "Home Router",  "emoji": "🏠", "fw": 5,  "reward": (40,  80),   "cooldown": 1*3600, "diff": "Easy"},
    {"id": "small_corp",   "name": "Small Corp",   "emoji": "🏢", "fw": 20, "reward": (120, 200),  "cooldown": 2*3600, "diff": "Medium"},
    {"id": "bank_server",  "name": "Bank Server",  "emoji": "🏦", "fw": 40, "reward": (350, 500),  "cooldown": 3*3600, "diff": "Hard"},
    {"id": "gov_database", "name": "Gov Database", "emoji": "🏛️", "fw": 60, "reward": (600, 900),  "cooldown": 4*3600, "diff": "Expert"},
    {"id": "nsa_node",     "name": "NSA Node",     "emoji": "🔴", "fw": 80, "reward": (1200,1800), "cooldown": 6*3600, "diff": "Elite"},
]
NPC_BY_ID = {t["id"]: t for t in NPC_TARGETS}

# ── Daily missions pool ───────────────────────────────────────────────────────
# 3 missions are picked per day (seeded by date — same for everyone).
MISSION_POOL = [
    {"id": "win_hack",    "desc": "Win 1 hack",                    "type": "hack_win",    "target": 1,   "reward": 80},
    {"id": "win_3hacks",  "desc": "Win 3 hacks",                   "type": "hack_win",    "target": 3,   "reward": 250},
    {"id": "attempt_2",   "desc": "Attempt 2 hacks",               "type": "hack_attempt","target": 2,   "reward": 60},
    {"id": "block_1",     "desc": "Block an incoming hack",        "type": "block",       "target": 1,   "reward": 120},
    {"id": "claim_daily", "desc": "Claim your daily reward",       "type": "daily_claim", "target": 1,   "reward": 40},
    {"id": "buy_item",    "desc": "Buy 1 item from the shop",      "type": "buy_item",    "target": 1,   "reward": 150},
    {"id": "steal_big",   "desc": "Steal 500+ bits in one hack",   "type": "steal_big",   "target": 1,   "reward": 200},
    {"id": "equip_item",  "desc": "Equip an item",                 "type": "equip_item",  "target": 1,   "reward": 50},
]

# ── Item catalogue (24 items: 12 firewalls + 12 attack tools) ─────────────────
ITEMS = {
    # ── FIREWALLS ──────────────────────────────────────────────────────────────
    "sk_fw_n":  {
        "name": "Packet Filter", "type": "firewall", "tier": 1, "stat": 15, "cost": 150,
        "desc": "A basic layer-3 filter. Better than nothing.",
        "special": None,
    },
    "sk_fw_s":  {
        "name": "Port Shield", "type": "firewall", "tier": 1, "stat": 10, "cost": 200,
        "desc": "Monitors traffic and logs attacker signatures.",
        "special": {"key": "reveal_attacker", "desc": "Reveals attacker identity when you block them."},
    },
    "gh_fw_n":  {
        "name": "Stateful Guard", "type": "firewall", "tier": 2, "stat": 28, "cost": 500,
        "desc": "Tracks connection states to filter malicious packets.",
        "special": None,
    },
    "gh_fw_s":  {
        "name": "IDS Lite", "type": "firewall", "tier": 2, "stat": 22, "cost": 650,
        "desc": "Intrusion detection with automated burn response.",
        "special": {"key": "extra_penalty", "desc": "Attacker loses 60 bits (instead of 50) on a failed hack."},
    },
    "bh_fw_n":  {
        "name": "Deep Inspector", "type": "firewall", "tier": 3, "stat": 42, "cost": 1100,
        "desc": "Full packet inspection at every OSI layer.",
        "special": None,
    },
    "bh_fw_s":  {
        "name": "Honeypot Grid", "type": "firewall", "tier": 3, "stat": 35, "cost": 1400,
        "desc": "Lures attackers into a trap and occasionally fires back.",
        "special": {"key": "counterattack", "desc": "10% chance to auto-counterattack when you block."},
    },
    "apt_fw_n": {
        "name": "Neural Filter", "type": "firewall", "tier": 4, "stat": 57, "cost": 2500,
        "desc": "ML-driven threat analysis. Learns and adapts in real time.",
        "special": None,
    },
    "apt_fw_s": {
        "name": "Adaptive Core", "type": "firewall", "tier": 4, "stat": 50, "cost": 3200,
        "desc": "Converts blocked attack energy into profit.",
        "special": {"key": "steal_on_defend", "desc": "Steal 5% of the attacker's bits when you block them."},
    },
    "zd_fw_n":  {
        "name": "Quantum Barrier", "type": "firewall", "tier": 5, "stat": 72, "cost": 5000,
        "desc": "Quantum-encrypted perimeter. Near-impenetrable.",
        "special": None,
    },
    "zd_fw_s":  {
        "name": "Polymorphic Wall", "type": "firewall", "tier": 5, "stat": 65, "cost": 6500,
        "desc": "Constantly shifts signature, extending attacker recovery.",
        "special": {"key": "extend_cooldown", "desc": "Attacker's cooldown becomes 45 minutes if they fail."},
    },
    "ph_fw_n":  {
        "name": "Ghost Wall", "type": "firewall", "tier": 6, "stat": 87, "cost": 10000,
        "desc": "A fortress no one can see coming.",
        "special": None,
    },
    "ph_fw_s":  {
        "name": "Phantom Veil", "type": "firewall", "tier": 6, "stat": 80, "cost": 13000,
        "desc": "Cloaks your defenses entirely. Ghost mode activated.",
        "special": {"key": "anonymous_defense", "desc": "Your firewall shows as ??? in /info. Total anonymity."},
    },
    # ── ATTACK TOOLS ──────────────────────────────────────────────────────────
    "sk_atk_n": {
        "name": "Port Scanner", "type": "attack", "tier": 1, "stat": 5, "cost": 150,
        "desc": "Scans for open ports. The classic script kiddie opener.",
        "special": None,
    },
    "sk_atk_s": {
        "name": "OSINT Probe", "type": "attack", "tier": 1, "stat": 5, "cost": 200,
        "desc": "Gathers intel before the strike. Know your target's wallet.",
        "special": {"key": "reveal_balance", "desc": "Shows target's current bits before the attack."},
    },
    "gh_atk_n": {
        "name": "SQL Injector", "type": "attack", "tier": 2, "stat": 18, "cost": 500,
        "desc": "Classic injection vector. Reliable and effective.",
        "special": None,
    },
    "gh_atk_s": {
        "name": "Payload Crafter", "type": "attack", "tier": 2, "stat": 14, "cost": 650,
        "desc": "Custom-built payloads that extract more than expected.",
        "special": {"key": "better_steal", "desc": "Steal 15-25% instead of the standard 10-20%."},
    },
    "bh_atk_n": {
        "name": "Buffer Overflow", "type": "attack", "tier": 3, "stat": 32, "cost": 1100,
        "desc": "Overwrites memory to seize control. Brutal and effective.",
        "special": None,
    },
    "bh_atk_s": {
        "name": "Zero Click", "type": "attack", "tier": 3, "stat": 26, "cost": 1400,
        "desc": "No user interaction needed. See their defenses before striking.",
        "special": {"key": "reveal_firewall", "desc": "Reveals target's equipped firewall before the attack."},
    },
    "apt_atk_n": {
        "name": "RAT Deployer", "type": "attack", "tier": 4, "stat": 45, "cost": 2500,
        "desc": "Installs a Remote Access Trojan. Persistent and silent.",
        "special": None,
    },
    "apt_atk_s": {
        "name": "Lateral Mover", "type": "attack", "tier": 4, "stat": 38, "cost": 3200,
        "desc": "Pivots through the network. Always moving, always striking.",
        "special": {"key": "reduced_cooldown_20", "desc": "Your /hack cooldown is reduced to 20 minutes."},
    },
    "zd_atk_n": {
        "name": "Kernel Exploit", "type": "attack", "tier": 5, "stat": 60, "cost": 5000,
        "desc": "Root-level access. The system is yours.",
        "special": None,
    },
    "zd_atk_s": {
        "name": "Shadow Protocol", "type": "attack", "tier": 5, "stat": 52, "cost": 6500,
        "desc": "A ghost operation. The target never knows who hit them.",
        "special": {"key": "anonymous_attack", "desc": "Target is not notified of who attacked them."},
    },
    "ph_atk_n": {
        "name": "Dark Toolkit", "type": "attack", "tier": 6, "stat": 75, "cost": 10000,
        "desc": "The complete arsenal. Nothing stands in your way.",
        "special": None,
    },
    "ph_atk_s": {
        "name": "Void Strike", "type": "attack", "tier": 6, "stat": 67, "cost": 13000,
        "desc": "Strikes from the void. Cooldown melts. Loot multiplies.",
        "special": {"key": "void_strike", "desc": "Cooldown 15 min. Steal 20-30% on success."},
    },
}

# ── Rate limiting ─────────────────────────────────────────────────────────────
_rate_buckets = {}


# ── Bot client ────────────────────────────────────────────────────────────────
class VoidBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.http_session = None

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession(headers=API_HEADERS)
        synced = await self.tree.sync()
        print(f"✅ Slash commands synced ({len(synced)} commands)")

        # Re-register the persistent BuyView so interactions on the old shop
        # message still work after a restart.
        state = _load_shop_state()
        if state.get("slots") and state.get("message_id"):
            try:
                self.add_view(BuyView(state["slots"]), message_id=state["message_id"])
            except Exception as e:
                print(f"⚠️ Could not re-register BuyView: {e}")

        asyncio.create_task(role_sync_loop())
        asyncio.create_task(shop_refresh_loop())

    async def close(self):
        if self.http_session:
            await self.http_session.close()
        await super().close()

    async def api_get(self, path, params=None):
        try:
            async with self.http_session.get(f"{API_URL}{path}", params=params) as r:
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    data = {}
                return r.status, (data or {})
        except Exception as e:
            print(f"❌ API GET {path}: {e}")
            return 0, {}

    async def api_post(self, path, payload):
        try:
            async with self.http_session.post(f"{API_URL}{path}", json=payload) as r:
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    data = {}
                return r.status, (data or {})
        except Exception as e:
            print(f"❌ API POST {path}: {e}")
            return 0, {}


bot = VoidBot()


# ── General helpers ───────────────────────────────────────────────────────────
def link_rate_limited(user_id):
    now = time.time()
    times = [t for t in _link_attempts.get(user_id, []) if now - t < LINK_ATTEMPT_WINDOW]
    if len(times) >= LINK_ATTEMPT_LIMIT:
        _link_attempts[user_id] = times
        return True
    times.append(now)
    _link_attempts[user_id] = times
    return False


def progress_bar(pct, length=14):
    pct = max(0, min(100, int(pct)))
    filled = round(length * pct / 100)
    return "█" * filled + "░" * (length - filled)


def hex_to_int(color):
    try:
        return int(str(color).lstrip("#"), 16)
    except Exception:
        return 0x5865F2


def build_rank_embed(member, data):
    rank = data["rank"]
    prog = data["progress"]
    nxt  = data.get("next")
    bar  = progress_bar(prog["pct"])

    embed = discord.Embed(title=rank["label"], color=hex_to_int(rank["color"]))
    embed.set_author(name=data.get("name") or member.display_name)
    embed.set_thumbnail(url=data.get("avatar") or member.display_avatar.url)

    embed.add_field(name="✨ XP", value=f"**{data['xp']:,}**", inline=True)
    pos = data.get("position")
    embed.add_field(name="🏆 Position", value=(f"**#{pos}**" if pos else "—"), inline=True)
    embed.add_field(name="🎖️ Tier", value=f"**{rank['tier']}/10**", inline=True)

    if nxt:
        embed.add_field(
            name=f"📈 To {nxt['label']}",
            value=f"`{bar}` **{prog['pct']}%**\n**{prog['needed']:,} XP** to the next rank",
            inline=False,
        )
    else:
        embed.add_field(
            name="📈 Progress",
            value=f"`{bar}` **MAX**\nYou've reached the highest rank. 🖤",
            inline=False,
        )
    embed.set_footer(text="VoidCyber")
    return embed


async def assign_rank_roles(member, tier):
    guild = member.guild
    target_role_id = RANK_ROLE_BY_TIER.get(tier)
    member_role_ids = {r.id for r in member.roles}
    to_add, to_remove = [], []

    linked_role = guild.get_role(LINKED_ROLE_ID)
    if linked_role and linked_role.id not in member_role_ids:
        to_add.append(linked_role)

    for rid in ALL_RANK_ROLE_IDS:
        if rid == target_role_id:
            if rid not in member_role_ids:
                role = guild.get_role(rid)
                if role:
                    to_add.append(role)
        elif rid in member_role_ids:
            role = guild.get_role(rid)
            if role:
                to_remove.append(role)

    try:
        if to_add:
            await member.add_roles(*to_add, reason="VoidCyber rank sync")
        if to_remove:
            await member.remove_roles(*to_remove, reason="VoidCyber rank sync")
    except discord.Forbidden:
        print("⚠️ Missing 'Manage Roles' or the bot role is too low in the hierarchy.")
    except Exception as e:
        print(f"❌ Role assignment error: {e}")


async def remove_all_void_roles(member):
    guild = member.guild
    member_role_ids = {r.id for r in member.roles}
    to_remove = []
    for rid in list(ALL_RANK_ROLE_IDS) + [LINKED_ROLE_ID]:
        if rid in member_role_ids:
            role = guild.get_role(rid)
            if role:
                to_remove.append(role)
    if to_remove:
        try:
            await member.remove_roles(*to_remove, reason="VoidCyber unlink")
        except Exception as e:
            print(f"❌ Role removal error: {e}")


def game_rate_limited(command, user_id):
    now = time.time()
    key = (command, user_id)
    times = [t for t in _rate_buckets.get(key, []) if now - t < GAME_RATE_WINDOW]
    if len(times) >= GAME_RATE_LIMIT:
        _rate_buckets[key] = times
        return True
    times.append(now)
    _rate_buckets[key] = times
    return False


# ── Game data layer ───────────────────────────────────────────────────────────
def _load_game_data():
    try:
        with open(GAME_DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

    # Migrate old format (had a single "firewall" string key) to new format
    for uid, rec in data.items():
        if not isinstance(rec, dict):
            continue
        if "inventory" not in rec:
            rec.pop("firewall", None)   # old key — discard, items are incompatible
            rec["inventory"]        = []
            rec["equipped_firewall"] = None
            rec["equipped_attack"]  = None
        # Ensure all fields exist
        rec.setdefault("bits",            STARTING_BITS)
        rec.setdefault("inventory",       [])
        rec.setdefault("equipped_firewall", None)
        rec.setdefault("equipped_attack", None)
        rec.setdefault("attacks_made",    0)
        rec.setdefault("attacks_won",     0)
        rec.setdefault("times_hacked",    0)
        rec.setdefault("defenses_won",    0)
        rec.setdefault("bits_stolen",     0)
        rec.setdefault("bits_lost",       0)
        rec.setdefault("last_hack",       0)
        rec.setdefault("daily_streak",    0)
        rec.setdefault("last_daily",      "")
    return data


_game_data = _load_game_data()


def save_game_data():
    try:
        with open(GAME_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(_game_data, f, indent=2)
    except Exception as e:
        print(f"❌ Failed to save game data: {e}")


def get_player(user_id):
    """Return the player record, creating a fresh one if this is a new player."""
    uid = str(user_id)
    if uid not in _game_data:
        # New players start with a free Packet Filter (tier 1) already equipped.
        # When it breaks they'll need to buy from the shop — good tutorial.
        _game_data[uid] = {
            "bits":              STARTING_BITS,
            "inventory":         [],
            "equipped_firewall": "sk_fw_n",
            "equipped_attack":   None,
            "attacks_made":      0,
            "attacks_won":       0,
            "times_hacked":      0,
            "defenses_won":      0,
            "bits_stolen":       0,
            "bits_lost":         0,
            "last_hack":         0,
            "daily_streak":      0,
            "last_daily":        "",
            "missions":          {"date": "", "active": [], "progress": {}, "claimed": []},
            "fw_hp":             {"sk_fw_n": FW_HP_BY_TIER[1]},  # full HP = 5
            "npc_cooldowns":     {},
            "created_at":        time.time(),
            "last_active":       time.time(),
        }
    else:
        _game_data[uid].setdefault("missions", {"date": "", "active": [], "progress": {}, "claimed": []})
        _game_data[uid].setdefault("fw_hp", {})
        _game_data[uid].setdefault("npc_cooldowns", {})
        _game_data[uid].setdefault("created_at", time.time())
        _game_data[uid].setdefault("last_active", time.time())
    return _game_data[uid]


NEWBIE_SHIELD_SECONDS   = 24 * 3600   # 24 hours after account creation
INACTIVE_SHIELD_SECONDS = 7  * 86400  # 7 days without any activity


def touch_active(player):
    """Update last_active to now. Call on any game action."""
    player["last_active"] = time.time()


# ── Mission helpers ───────────────────────────────────────────────────────────
def _today_missions():
    """Return 3 mission IDs for today — deterministic, same for all players."""
    today = datetime.now(timezone.utc).date().isoformat()
    seed  = int(today.replace("-", ""))
    rng   = random.Random(seed)
    return [m["id"] for m in rng.sample(MISSION_POOL, min(3, len(MISSION_POOL)))]


def _ensure_missions(player):
    """Reset missions for this player if the day has changed."""
    today = datetime.now(timezone.utc).date().isoformat()
    m = player.setdefault("missions", {"date": "", "active": [], "progress": {}, "claimed": []})
    if m.get("date") != today:
        m["date"]     = today
        m["active"]   = _today_missions()
        m["progress"] = {}
        m["claimed"]  = []
    return m


def _advance_mission(player, mission_type):
    """Increment progress by 1 on any active, unclaimed mission matching the type."""
    m = _ensure_missions(player)
    for mid in m["active"]:
        if mid in m["claimed"]:
            continue
        mission = next((x for x in MISSION_POOL if x["id"] == mid), None)
        if mission and mission["type"] == mission_type:
            m["progress"][mid] = m["progress"].get(mid, 0) + 1


# ── Firewall durability helpers ───────────────────────────────────────────────
def _fw_hp(player, item_key):
    """Current HP of a firewall. Defaults to max HP for its tier if never hit."""
    item = ITEMS.get(item_key)
    if not item:
        return 0
    max_hp = FW_HP_BY_TIER[item["tier"]]
    return player["fw_hp"].get(item_key, max_hp)


def _fw_max_hp(item_key):
    item = ITEMS.get(item_key)
    return FW_HP_BY_TIER[item["tier"]] if item else 0


def _damage_firewall(player, item_key):
    """Deal 1 damage to the equipped firewall. Destroys it if HP hits 0. Returns True if destroyed."""
    item = ITEMS.get(item_key)
    if not item:
        return False
    max_hp = FW_HP_BY_TIER[item["tier"]]
    current = player["fw_hp"].get(item_key, max_hp)
    current -= 1
    if current <= 0:
        # Firewall destroyed — remove from everywhere
        player["fw_hp"].pop(item_key, None)
        player["equipped_firewall"] = None
        if item_key in player["inventory"]:
            player["inventory"].remove(item_key)
        return True
    else:
        player["fw_hp"][item_key] = current
        return False


def _hp_bar(current, max_hp, length=8):
    pct = current / max_hp if max_hp else 0
    filled = round(length * pct)
    return "█" * filled + "░" * (length - filled)


# ── Shop state ────────────────────────────────────────────────────────────────
def _load_shop_state():
    try:
        with open(SHOP_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_shop_state(state):
    try:
        with open(SHOP_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"❌ Failed to save shop state: {e}")


def _pick_shop_items():
    """Pick SHOP_SLOTS_FW firewalls + SHOP_SLOTS_ATK attack tools, weighted by tier."""
    fw_keys  = [k for k, v in ITEMS.items() if v["type"] == "firewall"]
    atk_keys = [k for k, v in ITEMS.items() if v["type"] == "attack"]

    def weighted_sample(pool, n):
        chosen, remaining, weights = [], list(pool), [TIER_WEIGHTS[ITEMS[k]["tier"]] for k in pool]
        for _ in range(min(n, len(remaining))):
            total = sum(weights)
            r = random.uniform(0, total)
            cum = 0
            for i, w in enumerate(weights):
                cum += w
                if r <= cum:
                    chosen.append(remaining[i])
                    remaining.pop(i)
                    weights.pop(i)
                    break
        return chosen

    slots = []
    for key in weighted_sample(fw_keys, SHOP_SLOTS_FW) + weighted_sample(atk_keys, SHOP_SLOTS_ATK):
        slots.append({"item_key": key, "stock": random.randint(2, 4)})
    return slots


def _build_shop_embed(slots, next_refresh_ts):
    def fmt_item(slot):
        key   = slot["item_key"]
        item  = ITEMS[key]
        t     = item["tier"]
        emoji = TIER_EMOJI[t]
        tname = TIER_META[t]["name"]
        stat  = f"Block `{item['stat']}%`" if item["type"] == "firewall" else f"ATK `{item['stat']}%`"
        stock = f"**{slot['stock']}** left" if slot["stock"] > 0 else "~~SOLD OUT~~"
        sp    = f"\n> ⚡ *{item['special']['desc']}*" if item["special"] else ""
        return (
            f"{emoji} **{item['name']}** — {tname} — {stat}\n"
            f"> {item['desc']}{sp}\n"
            f"> 💰 **{item['cost']} bits** · 📦 {stock}"
        )

    fw_slots  = [s for s in slots if ITEMS[s["item_key"]]["type"] == "firewall"]
    atk_slots = [s for s in slots if ITEMS[s["item_key"]]["type"] == "attack"]
    sep       = "\n​\n"   # visual blank line

    desc = (
        f"**Next refresh:** <t:{next_refresh_ts}:R>\n"
        f"Stock is shared — first come, first served.\n"
        f"\n`────────────────────────────`\n"
        f"\n🛡️ **FIREWALLS**\n\n"
        + (sep + "\n").join(fmt_item(s) for s in fw_slots)
        + f"\n\n`────────────────────────────`\n"
        f"\n⚔️ **ATTACK TOOLS**\n\n"
        + (sep + "\n").join(fmt_item(s) for s in atk_slots)
    )

    embed = discord.Embed(
        title="🛒  VOID SHOP — Rotating Arsenal",
        description=desc,
        color=0xe74c3c,
    )
    legend = "  ".join(f"{TIER_EMOJI[t]} {TIER_META[t]['name']}" for t in range(1, 7))
    embed.set_footer(text=f"Rotates every 20 min  ·  {legend}")
    return embed


# ── Shop UI (persistent — survives bot restarts) ──────────────────────────────
class BuySelect(discord.ui.Select):
    def __init__(self, slots):
        options = [
            discord.SelectOption(
                label=f"{ITEMS[s['item_key']]['name']} — {ITEMS[s['item_key']]['cost']} bits",
                value=s["item_key"],
                emoji=TIER_EMOJI[ITEMS[s["item_key"]]["tier"]],
                description=(
                    f"{TIER_META[ITEMS[s['item_key']]['tier']]['name']} · "
                    f"{'Block' if ITEMS[s['item_key']]['type'] == 'firewall' else 'ATK'}: "
                    f"{ITEMS[s['item_key']]['stat']}% · Stock: {s['stock']}"
                ),
            )
            for s in slots if s["stock"] > 0
        ] or [discord.SelectOption(label="No items available", value="__none__")]

        super().__init__(
            custom_id="shop_buy_select",
            placeholder="Select an item to buy...",
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        if key == "__none__":
            await interaction.response.send_message("The shop is currently empty.", ephemeral=True)
            return

        # Always read from disk to avoid stale in-memory state
        state = _load_shop_state()
        slot  = next((s for s in state.get("slots", []) if s["item_key"] == key), None)

        if slot is None or slot["stock"] <= 0:
            await interaction.response.send_message(
                "That item just sold out. Wait for the next rotation!", ephemeral=True
            )
            return

        item   = ITEMS[key]
        player = get_player(interaction.user.id)

        # Ownership check
        owns = (
            key in player["inventory"]
            or player["equipped_firewall"] == key
            or player["equipped_attack"] == key
        )
        if owns:
            await interaction.response.send_message(
                f"You already own **{item['name']}**.", ephemeral=True
            )
            return

        if player["bits"] < item["cost"]:
            await interaction.response.send_message(
                f"Not enough bits. **{item['name']}** costs **{item['cost']}** bits, "
                f"you have **{player['bits']}**.",
                ephemeral=True,
            )
            return

        # Commit the purchase
        player["bits"]     -= item["cost"]
        player["inventory"].append(key)
        slot["stock"]      -= 1
        _advance_mission(player, "buy_item")
        save_game_data()
        _save_shop_state(state)

        # Rebuild and edit the shop message with updated stock
        next_ts  = state.get("next_refresh", int(time.time()) + SHOP_REFRESH_MINUTES * 60)
        new_embed = _build_shop_embed(state["slots"], next_ts)
        new_view  = BuyView(state["slots"])
        try:
            channel = bot.get_channel(SHOP_CHANNEL_ID)
            if channel and state.get("message_id"):
                msg = channel.get_partial_message(state["message_id"])
                await msg.edit(embed=new_embed, view=new_view)
        except Exception as e:
            print(f"⚠️ Could not update shop message after purchase: {e}")

        tier = TIER_META[item["tier"]]
        await interaction.response.send_message(
            f"Purchased **{item['name']}** (Tier {item['tier']}: {tier['name']})!\n"
            f"It's in your /inventory. Use /equip to activate it.\n"
            f"Balance: **{player['bits']} bits**.",
            ephemeral=True,
        )


class BuyView(discord.ui.View):
    def __init__(self, slots):
        super().__init__(timeout=None)   # persistent view
        self.add_item(BuySelect(slots))


# ── Equip UI ──────────────────────────────────────────────────────────────────
class EquipSelect(discord.ui.Select):
    def __init__(self, owner_id, item_type, inventory):
        self.owner_id  = owner_id
        self.item_type = item_type
        options = [
            discord.SelectOption(
                label=ITEMS[k]["name"],
                value=k,
                description=(
                    f"Tier {ITEMS[k]['tier']}: {TIER_META[ITEMS[k]['tier']]['name']} "
                    f"| Stat: {ITEMS[k]['stat']}"
                ),
            )
            for k in inventory if k in ITEMS and ITEMS[k]["type"] == item_type
        ] or [discord.SelectOption(label=f"No {item_type}s in inventory", value="__none__")]

        super().__init__(
            placeholder=f"Choose a {item_type} to equip...",
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This isn't your menu.", ephemeral=True)
            return
        key = self.values[0]
        if key == "__none__":
            await interaction.response.send_message(
                f"You have no {self.item_type}s in your inventory.", ephemeral=True
            )
            return

        item   = ITEMS[key]
        player = get_player(self.owner_id)
        slot   = "equipped_firewall" if self.item_type == "firewall" else "equipped_attack"

        # Return previously equipped item to inventory
        current = player.get(slot)
        if current and current not in player["inventory"]:
            player["inventory"].append(current)

        # Equip the new item (remove from inventory)
        if key in player["inventory"]:
            player["inventory"].remove(key)
        player[slot] = key
        _advance_mission(player, "equip_item")
        save_game_data()

        tier = TIER_META[item["tier"]]
        await interaction.response.send_message(
            f"Equipped **{item['name']}** (Tier {item['tier']}: {tier['name']}, Stat: {item['stat']}).",
            ephemeral=True,
        )


class EquipView(discord.ui.View):
    def __init__(self, owner_id, item_type, inventory):
        super().__init__(timeout=60)
        self.add_item(EquipSelect(owner_id, item_type, inventory))


# ── Sell UI ───────────────────────────────────────────────────────────────────
class SellSelect(discord.ui.Select):
    def __init__(self, owner_id, inventory):
        self.owner_id = owner_id
        options = [
            discord.SelectOption(
                label=f"{ITEMS[k]['name']} — {int(ITEMS[k]['cost'] * 0.75)} bits",
                value=k,
                description=(
                    f"Tier {ITEMS[k]['tier']}: {TIER_META[ITEMS[k]['tier']]['name']} "
                    f"| Original: {ITEMS[k]['cost']} bits"
                ),
            )
            for k in inventory if k in ITEMS
        ] or [discord.SelectOption(label="No items to sell", value="__none__")]

        super().__init__(
            placeholder="Choose an item to sell...",
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This isn't your menu.", ephemeral=True)
            return
        key = self.values[0]
        if key == "__none__":
            await interaction.response.send_message("Nothing to sell.", ephemeral=True)
            return

        item   = ITEMS[key]
        player = get_player(self.owner_id)
        if key not in player["inventory"]:
            await interaction.response.send_message(
                "That item is not in your inventory.", ephemeral=True
            )
            return

        sell_price = int(item["cost"] * 0.75)
        player["inventory"].remove(key)
        player["bits"] += sell_price
        save_game_data()
        await interaction.response.send_message(
            f"Sold **{item['name']}** for **{sell_price} bits** (75% of {item['cost']}).\n"
            f"Balance: **{player['bits']} bits**.",
            ephemeral=True,
        )


class SellView(discord.ui.View):
    def __init__(self, owner_id, inventory):
        super().__init__(timeout=60)
        self.add_item(SellSelect(owner_id, inventory))


# ── Hack helpers ──────────────────────────────────────────────────────────────
def _hack_frame(attacker, target, fw_display, atk_name, step):
    lines = [
        f'root@void:~$ ./exploit --target "{target.display_name}"',
        f"[*] Loading {atk_name}............... armed",
        f"[*] Target acquired: {target.display_name}",
        f"[*] Probing [{fw_display}]...",
    ]
    desc  = "```\n" + "\n".join(lines[: step + 1]) + "\n```"
    embed = discord.Embed(title="BREACH IN PROGRESS", description=desc, color=0x2ecc71)
    embed.set_footer(text=f"{attacker.display_name} → {target.display_name}")
    return embed


def _hack_success_embed(attacker, target, fw_display, atk_name, steal, pct, bal, intel=""):
    log = "\n".join([
        f'root@void:~$ ./exploit --target "{target.display_name}"',
        f"[*] Loading {atk_name}............... armed",
        f"[*] Target acquired: {target.display_name}",
        f"[*] Probing [{fw_display}]...",
        "[+] Firewall bypassed!",
        "[+] ACCESS GRANTED",
        f"[$] Siphoned {steal} bits ({pct}%)",
    ])
    embed = discord.Embed(
        title="HACK SUCCESSFUL",
        description=f"```\n{log}\n```",
        color=0x2ecc71,
    )
    embed.add_field(name="Bits stolen", value=f"**+{steal}**", inline=True)
    embed.add_field(name="Your balance", value=f"**{bal}**", inline=True)
    if intel:
        embed.add_field(name="Pre-hack Intel", value=intel, inline=False)
    embed.set_footer(text=f"{attacker.display_name} hacked {target.display_name}")
    return embed


def _hack_blocked_embed(attacker, target, fw_display, atk_name, penalty, bal, notes="", intel=""):
    log = "\n".join([
        f'root@void:~$ ./exploit --target "{target.display_name}"',
        f"[*] Loading {atk_name}............... armed",
        f"[*] Target acquired: {target.display_name}",
        f"[*] Probing [{fw_display}]...",
        f"[!] [{fw_display}] blocked the intrusion",
        "[x] ACCESS DENIED — trace detected",
        f"[$] Lost {penalty} bits covering your tracks",
    ])
    embed = discord.Embed(
        title="HACK BLOCKED",
        description=f"```\n{log}\n```",
        color=0xe74c3c,
    )
    embed.add_field(name="Bits lost", value=f"**-{penalty}**", inline=True)
    embed.add_field(name="Your balance", value=f"**{bal}**", inline=True)
    if notes:
        embed.add_field(name="Additional Effects", value=notes, inline=False)
    if intel:
        embed.add_field(name="Pre-hack Intel", value=intel, inline=False)
    embed.set_footer(text=f"{target.display_name}'s {fw_display} repelled {attacker.display_name}")
    return embed


# ── Commands — Account linking ─────────────────────────────────────────────────
@bot.tree.command(name="link", description="Link your VoidCyber account to Discord")
@app_commands.describe(code="The one-time code generated on your VoidCyber profile")
async def link(interaction: discord.Interaction, code: str):
    await interaction.response.defer(ephemeral=True)

    if link_rate_limited(interaction.user.id):
        await interaction.followup.send(
            "⏳ Too many attempts. Please wait a minute and try again.", ephemeral=True
        )
        return

    status, data = await bot.api_post(
        "/api/discord/link",
        {
            "code":             code.strip().upper(),
            "discord_id":       str(interaction.user.id),
            "discord_username": str(interaction.user),
        },
    )

    if status != 200 or not data.get("ok"):
        messages = {
            "invalid_code":           "❌ Invalid code. Double-check you typed it correctly.",
            "expired_code":           "❌ Code expired. Generate a new one from your profile on the site.",
            "discord_already_linked": "❌ This Discord account is already linked to another VoidCyber profile.",
            "account_already_linked": "❌ That VoidCyber profile is already linked to another Discord account.",
            "missing_fields":         "❌ Missing code.",
        }
        msg = messages.get(data.get("error"), "❌ Linking failed. Please try again later.")
        await interaction.followup.send(msg, ephemeral=True)
        return

    await assign_rank_roles(interaction.user, data["rank"]["tier"])
    embed = build_rank_embed(interaction.user, data)
    await interaction.followup.send(
        content="✅ Account linked successfully! Here's your rank:",
        embed=embed,
        ephemeral=True,
    )


@bot.tree.command(name="unlink", description="Unlink your VoidCyber account from Discord")
async def unlink(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    status, data = await bot.api_post(
        "/api/discord/unlink", {"discord_id": str(interaction.user.id)}
    )
    if status != 200:
        await interaction.followup.send(
            "❌ Couldn't reach VoidCyber. Please try again.", ephemeral=True
        )
        return

    await remove_all_void_roles(interaction.user)
    if data.get("unlinked"):
        await interaction.followup.send(
            "✅ Account unlinked. I've removed your VoidCyber roles.", ephemeral=True
        )
    else:
        await interaction.followup.send(
            "ℹ️ This account wasn't linked. I've removed any VoidCyber roles anyway.",
            ephemeral=True,
        )


@bot.tree.command(name="rank", description="View your VoidCyber rank and progress")
async def rank(interaction: discord.Interaction):
    await interaction.response.defer()
    status, data = await bot.api_get(
        "/api/discord/rank", {"discord_id": str(interaction.user.id)}
    )
    if status != 200:
        await interaction.followup.send("❌ Couldn't reach VoidCyber. Please try again.")
        return
    if not data.get("linked"):
        await interaction.followup.send(
            "🔗 You haven't linked your account yet.\n"
            "Go to your VoidCyber profile, generate a code, and run **/link CODE**."
        )
        return

    await assign_rank_roles(interaction.user, data["rank"]["tier"])
    embed = build_rank_embed(interaction.user, data)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="leaderboard", description="VoidCyber leaderboard")
@app_commands.describe(
    board="Which leaderboard to show",
    only_me="Show only your position instead of the top 20",
)
@app_commands.choices(
    board=[
        app_commands.Choice(name="General (XP)", value="general"),
        app_commands.Choice(name="CTF",          value="ctf"),
    ]
)
async def leaderboard(
    interaction: discord.Interaction,
    board: app_commands.Choice[str] = None,
    only_me: bool = False,
):
    await interaction.response.defer(ephemeral=only_me)

    board_value = board.value if board else "general"
    is_ctf      = board_value == "ctf"
    score_label = "PTS" if is_ctf else "XP"

    if only_me:
        status, data = await bot.api_get(
            "/api/discord/leaderboard",
            {"discord_id": str(interaction.user.id), "me": "1", "board": board_value},
        )
        if status != 200:
            await interaction.followup.send("❌ Couldn't reach VoidCyber.", ephemeral=True)
            return
        if not data.get("linked"):
            await interaction.followup.send(
                "🔗 Link your account first with **/link**.", ephemeral=True
            )
            return
        me         = data["me"]
        board_name = "CTF" if is_ctf else "General"
        embed = discord.Embed(
            title=f"🏆 Your position — {board_name}",
            description=f"You're **#{me['position']}** out of **{data.get('total', '?')}** operatives.",
            color=hex_to_int(me["rank"]["color"]),
        )
        embed.add_field(name="Rank",       value=me["rank"]["label"],  inline=True)
        embed.add_field(name=score_label,  value=f"{me['xp']:,}",      inline=True)
        embed.set_footer(text="VoidCyber")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    status, data = await bot.api_get(
        "/api/discord/leaderboard", {"limit": "20", "board": board_value}
    )
    if status != 200:
        await interaction.followup.send("❌ Couldn't reach VoidCyber.")
        return
    top = data.get("top", [])
    if not top:
        await interaction.followup.send("📭 No data available.")
        return

    lines = []
    for u in top:
        i     = u["position"]
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"`#{i:>2}`")
        name  = discord.utils.escape_markdown(u.get("name") or "Anonymous")
        lines.append(f"{medal} **{name}** — {u['xp']:,} {score_label} · {u['rank']['label']}")

    title = "🏆 VoidCyber — Top 20 (CTF)" if is_ctf else "🏆 VoidCyber — Top 20 (XP)"
    embed = discord.Embed(title=title, description="\n".join(lines), color=0x5865F2)
    embed.set_footer(text="Use /leaderboard only_me:True to see your position")
    await interaction.followup.send(embed=embed)


# ── Commands — Hacking mini-game ──────────────────────────────────────────────

# Mission claim button
class MissionClaimView(discord.ui.View):
    def __init__(self, owner_id):
        super().__init__(timeout=60)
        self.owner_id = owner_id

    @discord.ui.button(label="Claim all rewards", style=discord.ButtonStyle.success, emoji="🎁")
    async def claim_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This isn't your menu.", ephemeral=True)
            return
        player = get_player(self.owner_id)
        m      = _ensure_missions(player)
        total  = 0
        for mid in m["active"]:
            if mid in m["claimed"]:
                continue
            mission = next((x for x in MISSION_POOL if x["id"] == mid), None)
            if mission and m["progress"].get(mid, 0) >= mission["target"]:
                m["claimed"].append(mid)
                player["bits"] += mission["reward"]
                total += mission["reward"]
        save_game_data()
        if total > 0:
            await interaction.response.send_message(
                f"🎁 Claimed **+{total} bits**! Balance: **{player['bits']} bits**.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message("Nothing to claim yet.", ephemeral=True)
        button.disabled = True
        await interaction.message.edit(view=self)


@bot.tree.command(name="missions", description="View and claim your daily missions")
async def missions_cmd(interaction: discord.Interaction):
    if game_rate_limited("missions", interaction.user.id):
        await interaction.response.send_message("⏳ Slow down — 5 uses per minute.", ephemeral=True)
        return

    player = get_player(interaction.user.id)
    m      = _ensure_missions(player)
    save_game_data()

    lines       = []
    claimable   = 0
    total_reward = 0

    for mid in m["active"]:
        mission  = next((x for x in MISSION_POOL if x["id"] == mid), None)
        if not mission:
            continue
        progress = m["progress"].get(mid, 0)
        target   = mission["target"]
        claimed  = mid in m["claimed"]
        done     = progress >= target

        if claimed:
            status = "✅ Claimed"
        elif done:
            status = "🎁 **Ready!**"
            claimable  += 1
            total_reward += mission["reward"]
        else:
            bar = progress_bar(progress / target * 100, length=10)
            status = f"`{bar}` {progress}/{target}"

        lines.append(
            f"**{mission['desc']}** — +{mission['reward']} bits\n> {status}"
        )

    embed = discord.Embed(
        title="📋 Daily Missions",
        description="\n\n".join(lines) or "No missions today.",
        color=0x2ecc71,
    )
    if claimable:
        embed.add_field(
            name="🎁 Ready to claim",
            value=f"**{claimable}** mission(s) · **+{total_reward} bits** waiting",
            inline=False,
        )
    embed.set_footer(text="Missions reset every day at midnight UTC.")

    view = MissionClaimView(interaction.user.id) if claimable else None
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="help", description="How to play the VoidCyber hacking game")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="VoidCyber — Hacking Game Guide",
        description=(
            "Earn **bits**, buy weapons and defenses from the rotating shop, "
            "and hack other players to steal their stash.\n​"
        ),
        color=0x5865F2,
    )

    embed.add_field(
        name="💰 How to earn bits",
        value=(
            "**/daily-reward** — Claim bits every day. Streak grows the reward (10 → 100 bits/day).\n"
            "**/missions** — 3 daily missions for everyone. Complete them for bonus bits.\n"
            "**/breach <target>** — Hack an NPC server. No big penalty on fail, resets on cooldown.\n"
            "**/hack @user** — Steal 10–30% of another player's bits (30 min cooldown)."
        ),
        inline=False,
    )

    embed.add_field(
        name="🎯 NPC Servers — /targets & /breach",
        value=(
            "5 hackable servers available at all times, each with its own cooldown (1h–6h):\n"
            "🏠 Home Router · 🏢 Small Corp · 🏦 Bank Server · 🏛️ Gov Database · 🔴 NSA Node\n"
            "Better attack tool = higher success chance = more bits.\n"
            "**/targets** — See all servers, rewards, and your cooldowns."
        ),
        inline=False,
    )

    embed.add_field(
        name="🛒 Shop & Items",
        value=(
            "Shop rotates **every 20 minutes** — 2 firewalls + 2 attack tools with limited stock.\n"
            "6 tiers: ⬜ Script Kiddie · 🔵 Grey Hat · 🟣 Black Hat · 🟠 APT · 🟡 Zero Day · 🔴 Phantom\n"
            "⚡ Some items have a **special effect**. Higher tier = better stats."
        ),
        inline=False,
    )

    embed.add_field(
        name="⚔️ Combat — how /hack works",
        value=(
            "`block % = defender's firewall stat − your attack tool stat`\n"
            "Win → steal bits. Fail → lose **50 bits** + wait cooldown (15–30 min).\n"
            "No firewall = innate **15% block**. Firewalls have **HP** — they break after enough blocks!"
        ),
        inline=False,
    )

    embed.add_field(
        name="🛡️ Firewall Durability",
        value=(
            "Every time your firewall blocks an attack it loses **1 HP**.\n"
            "When HP hits 0 the firewall is **destroyed** — you need to buy a new one from the shop.\n"
            "HP visible in **/inventory** and **/info**."
        ),
        inline=False,
    )

    embed.add_field(
        name="🛡️ Player Protections",
        value=(
            "**Newbie shield** — New players can't be hacked for the first **24 hours**.\n"
            "**Inactive shield** — Players inactive for **7+ days** are protected until they return."
        ),
        inline=False,
    )

    embed.add_field(
        name="📦 Loadout commands",
        value=(
            "**/inventory** — Bits, equipped items, bag\n"
            "**/equip** — Equip a firewall or attack tool\n"
            "**/unequip** — Unequip (returns to inventory)\n"
            "**/sell** — Sell an item for 75% of its price\n"
            "**/info [@user]** — View anyone's stats and loadout"
        ),
        inline=False,
    )

    embed.add_field(
        name="💸 Economy commands",
        value=(
            "**/transfer @user <amount>** — Send bits to another player\n"
            "**/balance [@user]** — Check your bits or another player's balance"
        ),
        inline=False,
    )

    embed.add_field(
        name="🔗 VoidCyber account",
        value=(
            "**/link <code>** — Link your VoidCyber site account\n"
            "**/rank** — Your XP rank and progress\n"
            "**/leaderboard** — Top 20 by XP or CTF points"
        ),
        inline=False,
    )

    embed.set_footer(text="Bits are local to the game — separate from site XP.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="inventory", description="View your items and equipped loadout")
async def inventory(interaction: discord.Interaction):
    if game_rate_limited("inventory", interaction.user.id):
        await interaction.response.send_message(
            "⏳ Slow down — 5 uses per minute.", ephemeral=True
        )
        return

    player = get_player(interaction.user.id)
    save_game_data()

    def item_line(key, show_hp=False):
        if key is None or key not in ITEMS:
            return "None"
        it   = ITEMS[key]
        tier = TIER_META[it["tier"]]
        sp   = f" | {it['special']['desc']}" if it["special"] else ""
        base = f"**{it['name']}** (T{it['tier']}: {tier['name']}, Stat: {it['stat']}){sp}"
        if show_hp and it["type"] == "firewall":
            cur = _fw_hp(player, key)
            mx  = _fw_max_hp(key)
            base += f" · HP: `{_hp_bar(cur, mx)}` {cur}/{mx}"
        return base

    inv_keys = player["inventory"]
    if inv_keys:
        inv_lines = [f"`{i+1}.` {item_line(k)}" for i, k in enumerate(inv_keys)]
        inv_text  = "\n".join(inv_lines)
    else:
        inv_text = "Empty — buy from the rotating /shop"

    embed = discord.Embed(title=f"Inventory — {interaction.user.display_name}", color=0x5865F2)
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.add_field(name="💰 Bits",          value=f"**{player['bits']:,}**",                               inline=True)
    embed.add_field(name="🛡️ Equipped FW",   value=item_line(player["equipped_firewall"], show_hp=True),  inline=False)
    embed.add_field(name="⚔️ Equipped Tool", value=item_line(player["equipped_attack"]),                  inline=False)
    embed.add_field(name=f"📦 Bag ({len(inv_keys)} items)", value=inv_text,                        inline=False)
    embed.set_footer(text="Use /equip, /unequip, or /sell to manage your items.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="equip", description="Equip a firewall or attack tool from your inventory")
@app_commands.describe(item_type="What type of item to equip")
@app_commands.choices(item_type=[
    app_commands.Choice(name="Firewall",     value="firewall"),
    app_commands.Choice(name="Attack Tool",  value="attack"),
])
async def equip(interaction: discord.Interaction, item_type: app_commands.Choice[str]):
    if game_rate_limited("equip", interaction.user.id):
        await interaction.response.send_message(
            "⏳ Slow down — 5 uses per minute.", ephemeral=True
        )
        return

    player = get_player(interaction.user.id)
    save_game_data()
    inv    = player["inventory"]

    has_type = any(k in ITEMS and ITEMS[k]["type"] == item_type.value for k in inv)
    if not has_type:
        await interaction.response.send_message(
            f"You don't have any **{item_type.name}** in your inventory.\n"
            "Buy one from the rotating shop!",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Choose a **{item_type.name}** to equip:",
        view=EquipView(interaction.user.id, item_type.value, inv),
        ephemeral=True,
    )


@bot.tree.command(name="unequip", description="Unequip your active firewall or attack tool")
@app_commands.describe(item_type="What slot to unequip")
@app_commands.choices(item_type=[
    app_commands.Choice(name="Firewall",    value="firewall"),
    app_commands.Choice(name="Attack Tool", value="attack"),
])
async def unequip(interaction: discord.Interaction, item_type: app_commands.Choice[str]):
    if game_rate_limited("unequip", interaction.user.id):
        await interaction.response.send_message(
            "⏳ Slow down — 5 uses per minute.", ephemeral=True
        )
        return

    player = get_player(interaction.user.id)
    slot   = "equipped_firewall" if item_type.value == "firewall" else "equipped_attack"
    key    = player.get(slot)

    if key is None:
        await interaction.response.send_message(
            f"You don't have a **{item_type.name}** equipped.", ephemeral=True
        )
        return

    item = ITEMS.get(key, {})
    player[slot] = None
    if key not in player["inventory"]:
        player["inventory"].append(key)
    save_game_data()

    await interaction.response.send_message(
        f"Unequipped **{item.get('name', key)}**. It's back in your inventory.",
        ephemeral=True,
    )


@bot.tree.command(name="sell", description="Sell an item from your inventory (75% of original cost)")
async def sell(interaction: discord.Interaction):
    if game_rate_limited("sell", interaction.user.id):
        await interaction.response.send_message(
            "⏳ Slow down — 5 uses per minute.", ephemeral=True
        )
        return

    player = get_player(interaction.user.id)
    save_game_data()

    if not player["inventory"]:
        await interaction.response.send_message(
            "Your inventory is empty — nothing to sell.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        "Choose an item to sell:",
        view=SellView(interaction.user.id, player["inventory"]),
        ephemeral=True,
    )


@bot.tree.command(name="hack", description="Attempt to breach a member and steal their bits")
@app_commands.describe(user="The member you want to hack")
async def hack(interaction: discord.Interaction, user: discord.Member):
    attacker = interaction.user
    target   = user

    if target.id == attacker.id:
        await interaction.response.send_message("🪞 You can't hack yourself.", ephemeral=True)
        return
    if target.bot:
        await interaction.response.send_message("🤖 You can't hack a bot.", ephemeral=True)
        return

    ap = get_player(attacker.id)
    tp = get_player(target.id)

    # ── Protection checks ─────────────────────────────────────────────────────
    now = time.time()
    if now - tp.get("created_at", now) < NEWBIE_SHIELD_SECONDS:
        remaining = int((NEWBIE_SHIELD_SECONDS - (now - tp["created_at"])) / 3600) + 1
        await interaction.response.send_message(
            f"🛡️ **{target.display_name}** is protected by a **newbie shield** for ~{remaining}h more.",
            ephemeral=True,
        )
        return
    if now - tp.get("last_active", now) > INACTIVE_SHIELD_SECONDS:
        await interaction.response.send_message(
            f"🛡️ **{target.display_name}** hasn't been active for 7+ days and is protected.",
            ephemeral=True,
        )
        return

    # ── Resolve equipped items ────────────────────────────────────────────────
    atk_key  = ap.get("equipped_attack")
    fw_key   = tp.get("equipped_firewall")
    atk_item = ITEMS.get(atk_key) if atk_key else None
    fw_item  = ITEMS.get(fw_key)  if fw_key  else None

    atk_stat = atk_item["stat"] if atk_item else 0
    fw_stat  = fw_item["stat"]  if fw_item  else MIN_BLOCK   # innate 15

    atk_special = (atk_item.get("special") or {}) if atk_item else {}
    fw_special  = (fw_item.get("special")  or {}) if fw_item  else {}

    # ── Cooldown ──────────────────────────────────────────────────────────────
    cooldown = HACK_COOLDOWN_DEFAULT
    if atk_special.get("key") == "reduced_cooldown_20":
        cooldown = 20 * 60
    elif atk_special.get("key") == "void_strike":
        cooldown = 15 * 60

    now       = time.time()
    remaining = cooldown - (now - ap["last_hack"])
    if remaining > 0:
        mins, secs = int(remaining // 60), int(remaining % 60)
        await interaction.response.send_message(
            f"⏳ Exploit kit recharging. Ready in **{mins}m {secs}s**.",
            ephemeral=True,
        )
        return

    # ── Steal range ───────────────────────────────────────────────────────────
    steal_min, steal_max = 10, 20
    if atk_special.get("key") == "better_steal":
        steal_min, steal_max = 15, 25
    elif atk_special.get("key") == "void_strike":
        steal_min, steal_max = 20, 30

    # ── Anonymous defense check ───────────────────────────────────────────────
    anonymous_defense = fw_special.get("key") == "anonymous_defense"
    fw_display = "???" if anonymous_defense else (fw_item["name"] if fw_item else "Default FW")
    atk_name   = atk_item["name"] if atk_item else "Basic Exploit"

    # ── Pre-hack intel (reveal_balance / reveal_firewall) ────────────────────
    intel_parts = []
    if atk_special.get("key") == "reveal_balance":
        intel_parts.append(f"OSINT scan — Target balance: **{tp['bits']} bits**")
    elif atk_special.get("key") == "reveal_firewall":
        if fw_item:
            intel_parts.append(
                f"Recon — Target firewall: **{fw_item['name']}** (Stat: {fw_stat})"
            )
        else:
            intel_parts.append(f"Recon — Target has no firewall (innate block: {MIN_BLOCK})")
    intel = "\n".join(intel_parts)

    # Lock in cooldown immediately (prevents spam)
    ap["last_hack"] = now
    save_game_data()

    # ── Animation ─────────────────────────────────────────────────────────────
    await interaction.response.send_message(
        embed=_hack_frame(attacker, target, fw_display, atk_name, 0)
    )
    msg = await interaction.original_response()
    for step in (1, 2, 3):
        await asyncio.sleep(0.9)
        await msg.edit(embed=_hack_frame(attacker, target, fw_display, atk_name, step))
    await asyncio.sleep(0.9)

    # ── Combat roll ───────────────────────────────────────────────────────────
    effective_block = max(MIN_BLOCK, min(MAX_BLOCK, fw_stat - atk_stat))
    roll    = random.randint(1, 100)
    blocked = roll <= effective_block

    # ── Resolve ───────────────────────────────────────────────────────────────
    if blocked:
        penalty_amount = 60 if fw_special.get("key") == "extra_penalty" else HACK_FAIL_PENALTY
        penalty = min(penalty_amount, ap["bits"])
        ap["bits"] -= penalty
        tp["bits"] += penalty
        ap["attacks_made"] += 1
        tp["defenses_won"] += 1
        _advance_mission(ap, "hack_attempt")
        _advance_mission(tp, "block")
        touch_active(ap)
        touch_active(tp)

        extra_notes = []

        # Durability — firewall takes 1 damage per successful block
        if fw_key:
            destroyed = _damage_firewall(tp, fw_key)
            if destroyed:
                extra_notes.append(f"**{fw_item['name']}** took too many hits and was destroyed!")

        # steal_on_defend — defender takes 5% of attacker's remaining bits
        if fw_special.get("key") == "steal_on_defend" and ap["bits"] > 0:
            bonus = max(1, int(ap["bits"] * 0.05))
            bonus = min(bonus, ap["bits"])
            ap["bits"] -= bonus
            tp["bits"] += bonus
            extra_notes.append(f"Adaptive Core drained **{bonus}** extra bits from your wallet.")

        # extend_cooldown — add 15 minutes on top of base cooldown
        if fw_special.get("key") == "extend_cooldown":
            ap["last_hack"] += 15 * 60
            extra_notes.append("Polymorphic Wall scrambled your toolkit — cooldown extended to **45 min**.")

        # counterattack — 10% chance to auto-retaliate
        if fw_special.get("key") == "counterattack" and random.randint(1, 10) == 1:
            counter = max(1, int(ap["bits"] * random.randint(10, 20) / 100)) if ap["bits"] > 0 else 0
            if counter:
                ap["bits"] -= counter
                tp["bits"] += counter
                extra_notes.append(f"Honeypot Grid fired back — lost **{counter}** additional bits.")

        # reveal_attacker — note in the result (target sees who attacked)
        if fw_special.get("key") == "reveal_attacker":
            extra_notes.append(f"Port Shield logged the intrusion — **{attacker.display_name}** is exposed.")

        save_game_data()
        await msg.edit(embed=_hack_blocked_embed(
            attacker, target, fw_display, atk_name,
            penalty, ap["bits"],
            notes="\n".join(extra_notes) if extra_notes else "",
            intel=intel,
        ))

    else:
        pct   = random.randint(steal_min, steal_max)
        steal = tp["bits"] * pct // 100
        if steal == 0 and tp["bits"] > 0:
            steal = 1
        steal = min(steal, tp["bits"])

        ap["bits"]       += steal
        tp["bits"]       -= steal
        ap["attacks_made"] += 1
        ap["attacks_won"]  += 1
        ap["bits_stolen"]  += steal
        tp["times_hacked"] += 1
        tp["bits_lost"]    += steal
        _advance_mission(ap, "hack_attempt")
        _advance_mission(ap, "hack_win")
        if steal >= 500:
            _advance_mission(ap, "steal_big")
        touch_active(ap)
        touch_active(tp)
        save_game_data()
        await msg.edit(embed=_hack_success_embed(
            attacker, target, fw_display, atk_name,
            steal, pct, ap["bits"], intel=intel,
        ))


@bot.tree.command(name="info", description="View a player's hacking stats and loadout")
@app_commands.describe(user="The member to inspect (defaults to you)")
async def info(interaction: discord.Interaction, user: discord.Member = None):
    if game_rate_limited("info", interaction.user.id):
        await interaction.response.send_message(
            "⏳ Slow down — 5 uses per minute.", ephemeral=True
        )
        return

    target = user or interaction.user
    if target.bot:
        await interaction.response.send_message("🤖 Bots don't play the game.", ephemeral=True)
        return

    p = get_player(target.id)
    save_game_data()

    fw_key  = p.get("equipped_firewall")
    atk_key = p.get("equipped_attack")
    fw_item  = ITEMS.get(fw_key)  if fw_key  else None
    atk_item = ITEMS.get(atk_key) if atk_key else None

    # Respect anonymous_defense — hide firewall from others
    viewer_is_self = (interaction.user.id == target.id)
    if fw_item and not viewer_is_self:
        fw_sp = fw_item.get("special") or {}
        if fw_sp.get("key") == "anonymous_defense":
            fw_display = "???"
            fw_stat_display = "???"
        else:
            cur = _fw_hp(p, fw_key)
            mx  = _fw_max_hp(fw_key)
            fw_display      = f"{fw_item['name']} (Stat: {fw_item['stat']}) · HP {cur}/{mx}"
            fw_stat_display = str(fw_item["stat"])
    elif fw_item:
        cur = _fw_hp(p, fw_key)
        mx  = _fw_max_hp(fw_key)
        fw_display      = f"{fw_item['name']} (Stat: {fw_item['stat']}) · HP {cur}/{mx}"
        fw_stat_display = str(fw_item["stat"])
    else:
        fw_display      = f"None (innate {MIN_BLOCK})"
        fw_stat_display = str(MIN_BLOCK)

    atk_display = f"{atk_item['name']} (Stat: {atk_item['stat']})" if atk_item else "None"

    win_rate = (p["attacks_won"] / p["attacks_made"] * 100) if p["attacks_made"] else 0

    fw_tier_color = TIER_META[fw_item["tier"]]["color"] if fw_item else 0x5865F2

    embed = discord.Embed(
        title=f"{target.display_name} — Hacker Profile",
        color=fw_tier_color,
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="💰 Bits",          value=f"**{p['bits']:,}**",     inline=True)
    embed.add_field(name="🛡️ Firewall",      value=fw_display,               inline=True)
    embed.add_field(name="⚔️ Attack Tool",   value=atk_display,              inline=True)
    embed.add_field(
        name="⚔️ Offense",
        value=f"{p['attacks_won']}/{p['attacks_made']} wins ({win_rate:.0f}%)",
        inline=True,
    )
    embed.add_field(name="🧱 Blocks",        value=f"{p['defenses_won']}",   inline=True)
    embed.add_field(name="💀 Hacked",        value=f"{p['times_hacked']}",   inline=True)
    embed.add_field(name="📈 Bits stolen",   value=f"{p['bits_stolen']:,}",  inline=True)
    embed.add_field(name="📉 Bits lost",     value=f"{p['bits_lost']:,}",    inline=True)
    embed.add_field(name="📦 Inventory",     value=f"{len(p['inventory'])} item(s)", inline=True)
    embed.set_footer(text="VoidCyber — Hacking Game")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="targets", description="Show hackable NPC servers and their cooldowns")
async def targets(interaction: discord.Interaction):
    if game_rate_limited("targets", interaction.user.id):
        await interaction.response.send_message("⏳ Slow down — 5 uses per minute.", ephemeral=True)
        return

    player = get_player(interaction.user.id)
    save_game_data()
    now = time.time()

    atk_key  = player.get("equipped_attack")
    atk_item = ITEMS.get(atk_key) if atk_key else None
    atk_stat = atk_item["stat"] if atk_item else 0

    lines = []
    for t in NPC_TARGETS:
        last = player["npc_cooldowns"].get(t["id"], 0)
        remaining = t["cooldown"] - (now - last)
        if remaining > 0:
            m, s = int(remaining // 60), int(remaining % 60)
            cd_str = f"⏳ Ready in {m}m {s}s"
        else:
            cd_str = "✅ **Ready**"

        # Success chance with current tool
        chance = max(10, min(90, atk_stat - t["fw"] + 50))
        rmin, rmax = t["reward"]
        lines.append(
            f"{t['emoji']} **{t['name']}** — {t['diff']}\n"
            f"> FW: `{t['fw']}` · Reward: **{rmin}–{rmax} bits** · Your chance: `{chance}%`\n"
            f"> {cd_str}"
        )

    atk_name = atk_item["name"] if atk_item else "None (base 0 ATK)"
    embed = discord.Embed(
        title="🎯 NPC Targets",
        description="\n\n".join(lines),
        color=0x2ecc71,
    )
    embed.set_footer(text=f"Your attack tool: {atk_name} | Use /breach <target> to hack")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="breach", description="Hack an NPC server to earn bits")
@app_commands.describe(target="The server to attack")
@app_commands.choices(target=[
    app_commands.Choice(name="🏠 Home Router    (Easy)",   value="home_router"),
    app_commands.Choice(name="🏢 Small Corp     (Medium)", value="small_corp"),
    app_commands.Choice(name="🏦 Bank Server    (Hard)",   value="bank_server"),
    app_commands.Choice(name="🏛️ Gov Database   (Expert)", value="gov_database"),
    app_commands.Choice(name="🔴 NSA Node       (Elite)",  value="nsa_node"),
])
async def breach(interaction: discord.Interaction, target: app_commands.Choice[str]):
    if game_rate_limited("breach", interaction.user.id):
        await interaction.response.send_message("⏳ Slow down — 5 uses per minute.", ephemeral=True)
        return

    t      = NPC_BY_ID[target.value]
    player = get_player(interaction.user.id)
    now    = time.time()

    # Cooldown check
    last      = player["npc_cooldowns"].get(t["id"], 0)
    remaining = t["cooldown"] - (now - last)
    if remaining > 0:
        m, s = int(remaining // 60), int(remaining % 60)
        await interaction.response.send_message(
            f"⏳ **{t['name']}** is on cooldown. Try again in **{m}m {s}s**.", ephemeral=True
        )
        return

    atk_key  = player.get("equipped_attack")
    atk_item = ITEMS.get(atk_key) if atk_key else None
    atk_stat = atk_item["stat"] if atk_item else 0
    atk_name = atk_item["name"] if atk_item else "Basic Exploit"

    # Set cooldown immediately
    player["npc_cooldowns"][t["id"]] = now
    save_game_data()

    # Combat roll — no MIN_BLOCK for NPC (no innate defense)
    chance  = max(10, min(90, atk_stat - t["fw"] + 50))
    success = random.randint(1, 100) <= chance

    if success:
        earned = random.randint(*t["reward"])
        player["bits"] += earned
        _advance_mission(player, "hack_win")
        if earned >= 500:
            _advance_mission(player, "steal_big")
        save_game_data()

        log = "\n".join([
            f'root@void:~$ ./exploit --target "{t["name"]}"',
            f"[*] Loading {atk_name}............... armed",
            f"[*] Scanning {t['name']} (FW: {t['fw']})...",
            "[+] Firewall bypassed!",
            "[+] ACCESS GRANTED",
            f"[$] Extracted {earned} bits",
        ])
        embed = discord.Embed(
            title=f"✅ {t['emoji']} {t['name']} — Breached",
            description=f"```\n{log}\n```",
            color=0x2ecc71,
        )
        embed.add_field(name="💰 Earned",   value=f"**+{earned} bits**",   inline=True)
        embed.add_field(name="🏦 Balance",  value=f"**{player['bits']}**", inline=True)
        next_ts = int(now + t["cooldown"])
        embed.set_footer(text=f"Next attempt: {t['name']}")
        embed.add_field(name="⏳ Next attempt", value=f"<t:{next_ts}:R>", inline=False)
    else:
        penalty = 10
        player["bits"] = max(0, player["bits"] - penalty)
        _advance_mission(player, "hack_attempt")
        save_game_data()

        log = "\n".join([
            f'root@void:~$ ./exploit --target "{t["name"]}"',
            f"[*] Loading {atk_name}............... armed",
            f"[*] Scanning {t['name']} (FW: {t['fw']})...",
            f"[!] Firewall held — intrusion detected",
            f"[x] ACCESS DENIED",
            f"[$] Lost {penalty} bits covering tracks",
        ])
        embed = discord.Embed(
            title=f"❌ {t['emoji']} {t['name']} — Failed",
            description=f"```\n{log}\n```",
            color=0xe74c3c,
        )
        embed.add_field(name="🔻 Lost",    value=f"**-{penalty} bits**",   inline=True)
        embed.add_field(name="🏦 Balance", value=f"**{player['bits']}**",  inline=True)
        next_ts = int(now + t["cooldown"])
        embed.add_field(name="⏳ Next attempt", value=f"<t:{next_ts}:R>", inline=False)
        embed.set_footer(text=f"Upgrade your attack tool for better odds.")

    await interaction.response.send_message(embed=embed)


def _daily_reward_for(streak_day):
    return DAILY_BASE + ((streak_day - 1) % DAILY_CYCLE) * DAILY_STEP


@bot.tree.command(name="daily-reward", description="Claim your daily bits and build a streak")
async def daily_reward(interaction: discord.Interaction):
    if game_rate_limited("daily-reward", interaction.user.id):
        await interaction.response.send_message(
            "⏳ Slow down — 5 uses per minute.", ephemeral=True
        )
        return

    player = get_player(interaction.user.id)
    today  = datetime.now(timezone.utc).date()
    streak = player.get("daily_streak", 0)

    last_date = None
    if player.get("last_daily"):
        try:
            last_date = datetime.strptime(player["last_daily"], "%Y-%m-%d").date()
        except ValueError:
            last_date = None

    if last_date == today:
        await interaction.response.send_message(
            f"✅ Already claimed today.\n"
            f"🔥 Streak: **{streak} day{'s' if streak != 1 else ''}** — come back tomorrow!",
            ephemeral=True,
        )
        return

    streak = (streak + 1) if last_date == today - timedelta(days=1) else 1

    reward = _daily_reward_for(streak)
    player["bits"]         = player.get("bits", 0) + reward
    player["daily_streak"] = streak
    player["last_daily"]   = today.isoformat()
    _advance_mission(player, "daily_claim")
    touch_active(player)
    save_game_data()

    next_reward = _daily_reward_for(streak + 1)
    embed = discord.Embed(
        title="🎁 Daily Reward Claimed",
        description=f"You earned **+{reward} bits**!",
        color=0xf1c40f,
    )
    embed.set_author(
        name=interaction.user.display_name,
        icon_url=interaction.user.display_avatar.url,
    )
    embed.add_field(name="🔥 Streak", value=f"**{streak} day{'s' if streak != 1 else ''}**", inline=True)
    embed.add_field(name="💰 Balance", value=f"**{player['bits']} bits**",                   inline=True)
    embed.set_footer(text=f"Come back tomorrow for +{next_reward} bits!")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="add-bits", description="[Admin] Give bits to a member")
@app_commands.describe(user="Member to give bits to", amount="How many bits to add")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def add_bits(interaction: discord.Interaction, user: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message(
            "❌ Amount must be a positive number.", ephemeral=True
        )
        return
    if user.bot:
        await interaction.response.send_message("🤖 Bots don't have a wallet.", ephemeral=True)
        return

    p = get_player(user.id)
    p["bits"] = p.get("bits", 0) + amount
    save_game_data()
    await interaction.response.send_message(
        f"✅ Added **{amount} bits** to {user.mention}. New balance: **{p['bits']} bits**.",
        ephemeral=True,
    )


@bot.tree.command(name="remove-bits", description="[Admin] Remove bits from a member")
@app_commands.describe(user="Member to remove bits from", amount="How many bits to remove")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def remove_bits(interaction: discord.Interaction, user: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message(
            "❌ Amount must be a positive number.", ephemeral=True
        )
        return
    if user.bot:
        await interaction.response.send_message("🤖 Bots don't have a wallet.", ephemeral=True)
        return

    p       = get_player(user.id)
    removed = min(amount, p.get("bits", 0))
    p["bits"] = p.get("bits", 0) - removed
    save_game_data()
    await interaction.response.send_message(
        f"✅ Removed **{removed} bits** from {user.mention}. New balance: **{p['bits']} bits**.",
        ephemeral=True,
    )




class TransferConfirmView(discord.ui.View):
    def __init__(self, sender: discord.Member, recipient: discord.Member, amount: int):
        super().__init__(timeout=30)
        self.sender    = sender
        self.recipient = recipient
        self.amount    = amount

    @discord.ui.button(label="Yes, transfer", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.sender.id:
            await interaction.response.send_message("❌ This is not your transfer.", ephemeral=True)
            return

        sender_data    = get_player(self.sender.id)
        recipient_data = get_player(self.recipient.id)

        if sender_data["bits"] < self.amount:
            await interaction.response.send_message(
                f"❌ Not enough bits. You have **{sender_data['bits']} bits**.", ephemeral=True
            )
            self.stop()
            return

        sender_data["bits"]    -= self.amount
        recipient_data["bits"] += self.amount
        save_game_data()

        self.stop()
        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(
            content=(
                f"✅ Transferred **{self.amount} bits** to {self.recipient.mention}.\n"
                f"💰 Your balance: **{sender_data['bits']} bits**"
            ),
            view=self,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.sender.id:
            await interaction.response.send_message("❌ This is not your transfer.", ephemeral=True)
            return

        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="❌ Transfer cancelled.", view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


@bot.tree.command(name="balance", description="Check your bits or another player's balance")
@app_commands.describe(user="Player to check (leave empty for yourself)")
@app_commands.guild_only()
async def balance(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    if target.bot:
        await interaction.response.send_message("🤖 Bots don't have a wallet.", ephemeral=True)
        return

    player = get_player(target.id)
    bits   = player.get("bits", 0)

    if target.id == interaction.user.id:
        desc = f"💰 You have **{bits} bits**."
    else:
        desc = f"💰 **{target.display_name}** has **{bits} bits**."

    embed = discord.Embed(description=desc, color=0xf1c40f)
    embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="transfer", description="Transfer bits to another player")
@app_commands.describe(user="Who to send bits to", amount="How many bits to transfer")
@app_commands.guild_only()
async def transfer(interaction: discord.Interaction, user: discord.Member, amount: int):
    if user.bot:
        await interaction.response.send_message("🤖 Bots don't have a wallet.", ephemeral=True)
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ You can't transfer bits to yourself.", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be a positive number.", ephemeral=True)
        return

    sender_data = get_player(interaction.user.id)
    if sender_data["bits"] < amount:
        await interaction.response.send_message(
            f"❌ Not enough bits. You have **{sender_data['bits']} bits**.", ephemeral=True
        )
        return

    view = TransferConfirmView(interaction.user, user, amount)
    await interaction.response.send_message(
        f"💸 Transfer **{amount} bits** to {user.mention}?\n"
        f"💰 Your balance after: **{sender_data['bits'] - amount} bits**",
        view=view,
        ephemeral=True,
    )


@bot.tree.error
async def on_command_error(interaction: discord.Interaction, error):
    print(f"Command error: {error}")
    if not interaction.response.is_done():
        await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)


# ── Background: role sync ─────────────────────────────────────────────────────
async def role_sync_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await sync_all_roles()
        except Exception as e:
            print(f"❌ Sync loop error: {e}")
        await asyncio.sleep(SYNC_INTERVAL)


async def sync_all_roles():
    status, data = await bot.api_get("/api/discord/linked-users")
    if status != 200:
        print(f"⚠️ Role sync: API returned {status}")
        return

    users = data.get("users", [])
    guild = bot.get_guild(int(GUILD_ID)) if GUILD_ID else None
    if guild is None and bot.guilds:
        guild = bot.guilds[0]
    if guild is None:
        return

    updated = 0
    for u in users:
        member = guild.get_member(int(u["discord_id"]))
        if member is None:
            continue
        await assign_rank_roles(member, u["tier"])
        updated += 1
    print(f"🔄 Role sync done — {updated}/{len(users)} linked members present")


# ── Background: shop rotation ─────────────────────────────────────────────────
async def do_shop_rotation():
    """Pick new items, build embed, and edit (or post) the shop message."""
    channel = bot.get_channel(SHOP_CHANNEL_ID)
    if channel is None:
        print(f"⚠️ Shop channel {SHOP_CHANNEL_ID} not found — skipping rotation")
        return

    slots          = _pick_shop_items()
    next_refresh   = int(time.time()) + SHOP_REFRESH_MINUTES * 60
    embed          = _build_shop_embed(slots, next_refresh)
    view           = BuyView(slots)
    state          = _load_shop_state()
    message_id     = state.get("message_id")

    # Try to edit the existing pinned message
    if message_id:
        try:
            msg = channel.get_partial_message(message_id)
            await msg.edit(embed=embed, view=view)
            state["slots"]        = slots
            state["next_refresh"] = next_refresh
            _save_shop_state(state)
            bot.add_view(view, message_id=message_id)
            print(f"🛒 Shop rotated (edited msg {message_id})")
            return
        except (discord.NotFound, discord.HTTPException):
            pass   # message deleted — post a fresh one

    # Post a new message
    try:
        msg = await channel.send(embed=embed, view=view)
        new_state = {"message_id": msg.id, "slots": slots, "next_refresh": next_refresh}
        _save_shop_state(new_state)
        bot.add_view(view, message_id=msg.id)
        print(f"🛒 Shop posted — new message ID {msg.id}")
    except Exception as e:
        print(f"❌ Could not post shop message: {e}")


async def shop_refresh_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            state = _load_shop_state()
            if time.time() >= state.get("next_refresh", 0):
                await do_shop_rotation()
        except Exception as e:
            print(f"❌ Shop rotation error: {e}")
        await asyncio.sleep(60)   # check once a minute


# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print("=" * 40)
    print(f"✅ VoidBot online as {bot.user}")
    print(f"🌐 API: {API_URL}")
    print(f"📊 Servers: {len(bot.guilds)}")
    print("=" * 40)


# ── Start ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ DISCORD_TOKEN missing in .env")
    if not BOT_SECRET:
        raise SystemExit("❌ DISCORD_BOT_SECRET missing in .env")
    bot.run(TOKEN)
