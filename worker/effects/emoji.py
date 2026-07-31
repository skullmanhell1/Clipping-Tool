"""Auto-emoji overlays synced to spoken words.

Reuses the Whisper word-level timestamps to drop relevant emoji onto the clip
at the moment the matching word is spoken. Two selection modes:

* ``keyword`` — a built-in keyword -> emoji map (fast, offline, deterministic).
* ``ai`` — ask the LLM for context-aware ``word -> emoji`` pairs for the clip
  transcript, then time them to the spoken words (falls back to keyword mode).

Intensity (Off / Subtle / Standard / Heavy) controls how many emoji appear.
Twemoji PNGs are resolved from ``settings.emoji_assets_dir`` (fetched from the
CDN and cached on first use) and composited with ffmpeg ``overlay`` filters,
optionally with an alpha "pop" as each one appears.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from config import settings

# Intensity -> minimum seconds between emoji (spacing). "off" disables overlays.
INTENSITY_SPACING: dict[str, float] = {
    "off": 0.0,
    "subtle": 10.0,
    "standard": 5.0,
    "heavy": 2.5,
}

# --------------------------------------------------------------------------- #
# The keyword map (A9)
# --------------------------------------------------------------------------- #
#
# Keys are matched case-insensitively against whole words (punctuation stripped), after the
# A10 inflection rules, so only **base forms** belong here - "win" covers "wins", "winning"
# and "won".
#
# **Why this got big.** The original map was 85 keywords over 53 glyphs, and on real speech
# that meant most clips produced no emoji at all: `standard` intensity allows one every five
# seconds, and a 60-second clip has to contain twelve mapped words spread across it to fill
# even half of them. The overlay was effectively decorative on the few clips that happened to
# say "money" or "fire".
#
# **Why it is not bigger still.** Every word here is one whose emoji is unambiguous *in
# speech*, and that rules out a large, tempting set. Homographs are the problem: a `bank` is
# 🏦 or a river's edge, `spring` is a season, a coil or a verb, `mine` is a pit or a pronoun,
# `light` is a lamp or a weight, `current` is electrical or a flow, `crash` is a car or a
# server, `mouse` is an animal or a peripheral, `date` is a day or a dinner, `wave` is water or
# a hand, `rock` is stone or music, `bug` is an insect or a defect, `note` is money, music or
# a memo. Every one of those would raise the keyword count and lower the hit *quality*, and an
# emoji illustrating the wrong sense of a word is worse than no emoji - it reads as a machine
# that did not understand the sentence. The same reasoning C14 used to refuse hue-only caption
# presets: a bigger number is not the goal.
#
# Synonyms deliberately share a glyph, which is why the keyword count is several times the
# glyph count. That keeps the vendored asset set (and so the repository) proportional to the
# number of *distinct pictures*, not to the size of the vocabulary, and A12 already refuses to
# use the same glyph twice in one clip - so synonym clusters cannot produce a repeated image.
#
# **Function words are excluded even when the picture is apt.** "Like" is 👍 in one sense and a
# filler in most others; "this", "here", "there" and "off" are grammatical far more often than
# they are deictic. A11's salience ranker already scores stopwords at zero, so such a word only
# ever wins a slot when nothing better is in the clip - which is exactly the wrong moment for a
# weak match, because that clip has no other emoji to distract from it. The rule is enforced by
# a test rather than by care, since the failure mode of adding one is a plausible-looking
# overlay on the word "just".
KEYWORD_EMOJI: dict[str, str] = {
    # --- reactions and emotion ------------------------------------------------
    "love": "❤️", "heart": "❤️", "adore": "❤️", "romance": "❤️",
    "amazing": "🤩", "incredible": "🤩", "awesome": "🤩", "stunning": "🤩",
    "brilliant": "🤩", "gorgeous": "🤩", "spectacular": "🤩",
    "wow": "😮", "shocked": "😮", "surprised": "😮", "unbelievable": "😮",
    "crazy": "🤯", "insane": "🤯", "blown": "🤯", "mental": "🤯", "wild": "🤯",
    "laugh": "😂", "funny": "😂", "haha": "😂", "hilarious": "😂", "joke": "😂",
    "comedy": "😂", "giggle": "😂",
    "happy": "😄", "joy": "😄", "glad": "😄", "cheerful": "😄", "delighted": "😄",
    "smile": "😊", "pleased": "😊", "grateful": "😊", "thankful": "😊",
    "sad": "😢", "unhappy": "😢", "upset": "😢", "disappointed": "😢",
    "cry": "😭", "sob": "😭", "tears": "😭", "heartbroken": "😭", "devastated": "😭",
    "angry": "😡", "furious": "😡", "mad": "😡", "rage": "😡", "livid": "😡",
    "annoyed": "😤", "frustrated": "😤", "fed": "😤",
    "scared": "😱", "terrified": "😱", "horror": "😱", "panic": "😱", "frightened": "😱",
    "worried": "😟", "anxious": "😟", "nervous": "😬", "awkward": "😬", "cringe": "😬",
    "tired": "😴", "exhausted": "😴", "sleep": "😴", "bored": "🥱", "yawn": "🥱",
    "sick": "🤢", "disgusting": "🤢", "gross": "🤢", "nasty": "🤢",
    "cool": "😎", "confident": "😎", "smooth": "😎", "slick": "😎",
    "think": "🤔", "wonder": "🤔", "curious": "🤔", "hmm": "🤔", "consider": "🤔",
    "confused": "😕", "unsure": "😕", "puzzled": "😕",
    "secret": "🤫", "shh": "🤫", "quiet": "🤫", "hush": "🤫", "confidential": "🤫",
    "lie": "🤥", "lying": "🤥", "dishonest": "🤥",
    "proud": "😌", "relieved": "😌", "calm": "😌", "peace": "🕊️", "peaceful": "🕊️",
    "cringing": "🫣", "embarrassed": "🫣", "hiding": "🫣",
    "beg": "🙏", "please": "🙏", "pray": "🙏", "thanks": "🙏", "gratitude": "🙏",
    "salute": "🫡", "respect": "🫡", "understood": "🫡",
    "shrug": "🤷", "whatever": "🤷", "dunno": "🤷",
    "facepalm": "🤦", "obvious": "🤦", "ridiculous": "🤦",
    "hug": "🫂", "support": "🫂", "together": "🫂", "community": "🫂",
    "kiss": "😘", "flirt": "😘", "wink": "😉", "cheeky": "😉",
    "cold": "🥶", "freezing": "🥶", "frozen": "🧊", "ice": "🧊",
    "sweat": "😅", "close": "😅", "barely": "😅", "phew": "😅",
    "dizzy": "😵", "overwhelmed": "😵", "done": "😵",
    "party": "🥳", "celebrate": "🎉", "celebration": "🎉", "congrats": "🎉",
    "congratulations": "🎉", "birthday": "🎂", "cake": "🎂", "anniversary": "🎂",

    # --- money and business --------------------------------------------------
    "money": "💰", "wealth": "💰", "fortune": "💰", "funds": "💰", "capital": "💰",
    "cash": "💵", "dollar": "💵", "salary": "💵", "wage": "💵", "payment": "💵",
    "rich": "🤑", "millionaire": "🤑", "billionaire": "🤑", "profit": "🤑", "greedy": "🤑",
    "card": "💳", "credit": "💳", "debit": "💳", "subscription": "💳",
    "bill": "🧾", "invoice": "🧾", "receipt": "🧾", "expense": "🧾", "tax": "🧾",
    "chart": "📊", "data": "📊", "statistics": "📊", "metrics": "📊", "analytics": "📊",
    "success": "📈", "growth": "📈", "increase": "📈", "rise": "📈", "gain": "📈",
    "revenue": "📈", "improve": "📈", "boost": "📈",
    "loss": "📉", "decline": "📉", "crash": "📉", "drop": "📉", "decrease": "📉",
    "recession": "📉", "collapse": "📉",
    "work": "💼", "business": "💼", "job": "💼", "career": "💼", "professional": "💼",
    "company": "🏢", "office": "🏢", "corporate": "🏢", "startup": "🏢",
    "deal": "🤝", "agreement": "🤝", "partner": "🤝", "contract": "🤝",
    "negotiate": "🤝", "handshake": "🤝",
    "team": "👥", "colleague": "👥", "staff": "👥", "employee": "👥", "crew": "👥",
    "boss": "👔", "manager": "👔", "executive": "👔", "founder": "👔",
    "customer": "🛒", "shopping": "🛒", "buy": "🛒", "purchase": "🛒", "order": "🛒",
    "sell": "🏷️", "sale": "🏷️", "discount": "🏷️", "price": "🏷️", "cheap": "🏷️",
    "expensive": "💸", "spend": "💸", "waste": "💸", "cost": "💸", "burn": "💸",
    "invest": "🏦", "savings": "🏦", "loan": "🏦", "mortgage": "🏦",
    "coin": "🪙", "crypto": "🪙", "bitcoin": "🪙", "token": "🪙",
    "diamond": "💎", "luxury": "💎", "premium": "💎", "valuable": "💎", "precious": "💎",
    "package": "📦", "delivery": "📦", "shipping": "📦", "parcel": "📦", "inventory": "📦",

    # --- winning, losing, judgement -----------------------------------------
    "win": "🏆", "winner": "🏆", "champion": "🏆", "trophy": "🏆", "victory": "🏆",
    "best": "🏆",
    "gold": "🥇", "first": "🥇", "top": "🥇", "number": "🥇",
    "silver": "🥈", "third": "🥉", "bronze": "🥉",
    "medal": "🎖️", "award": "🎖️", "honour": "🎖️", "honor": "🎖️",
    "lose": "😩", "fail": "😩", "failure": "😩", "defeat": "😩", "flop": "😩",
    "yes": "✅", "correct": "✅", "right": "✅", "check": "✅", "confirmed": "✅",
    "approved": "✅", "verified": "✅", "true": "✅", "valid": "✅", "pass": "✅",
    "no": "❌", "wrong": "❌", "incorrect": "❌", "false": "❌", "denied": "❌",
    "rejected": "❌", "banned": "❌", "cancelled": "❌", "invalid": "❌",
    "star": "⭐", "rating": "⭐", "favourite": "⭐", "favorite": "⭐", "review": "⭐",
    "perfect": "💯", "hundred": "💯", "totally": "💯", "absolutely": "💯", "exactly": "💯",
    "agree": "👍", "good": "👍", "nice": "👍", "approve": "👍",
    "bad": "👎", "disagree": "👎", "dislike": "👎", "terrible": "👎", "awful": "👎",
    "clap": "👏", "applause": "👏", "bravo": "👏", "impressive": "👏",
    "strong": "💪", "power": "💪", "muscle": "💪", "tough": "💪", "powerful": "💪",
    "brave": "🦁", "fearless": "🦁", "courage": "🦁",

    # --- ideas, learning, thinking ------------------------------------------
    "idea": "💡", "insight": "💡", "solution": "💡", "invention": "💡", "innovate": "💡",
    "realise": "💡", "realize": "💡", "figured": "💡",
    "smart": "🧠", "brain": "🧠", "mind": "🧠", "intelligent": "🧠", "genius": "🧠",
    "clever": "🧠", "memory": "🧠", "psychology": "🧠", "logic": "🧠",
    "learn": "📚", "study": "📚", "book": "📚", "read": "📚", "education": "📚",
    "knowledge": "📚", "library": "📚", "course": "📚", "lesson": "📚",
    "school": "🎓", "university": "🎓", "college": "🎓", "graduate": "🎓", "degree": "🎓",
    "teach": "🧑\u200d🏫", "teacher": "🧑\u200d🏫", "lecture": "🧑\u200d🏫", "explain": "🧑\u200d🏫",
    "write": "✍️", "note": "✍️", "notes": "✍️", "journal": "✍️", "draft": "✍️",
    "science": "🔬", "research": "🔬", "experiment": "🔬", "lab": "🔬", "biology": "🔬",
    "chemistry": "⚗️", "chemical": "⚗️", "formula": "⚗️", "reaction": "⚗️",
    "maths": "🧮", "math": "🧮", "calculate": "🧮", "arithmetic": "🧮", "count": "🧮",
    "search": "🔍", "find": "🔍", "investigate": "🔍", "detail": "🔍", "examine": "🔍",
    "discover": "🔍", "evidence": "🔍",
    "question": "❓", "why": "❓", "ask": "❓", "unknown": "❓", "mystery": "❓",
    "answer": "💬", "reply": "💬", "comment": "💬", "conversation": "💬", "chat": "💬",
    "discuss": "💬", "talk": "💬", "say": "💬",
    "important": "❗", "critical": "❗", "urgent": "❗", "attention": "❗", 
    "puzzle": "🧩", "complicated": "🧩", "piece": "🧩", "fit": "🧩",

    # --- time ----------------------------------------------------------------
    "time": "⏰", "clock": "⏰", "alarm": "⏰", "deadline": "⏰", "schedule": "⏰",
    "early": "⏰", "late": "⏰",
    "hour": "⏳", "minute": "⏳", "wait": "⏳", "patience": "⏳",
    "duration": "⏳", "countdown": "⏳", "timer": "⏳",
    "day": "📅", "week": "📅", "month": "📅", "year": "📅", "calendar": "📅",
    "date": "📅", "today": "📅", "tomorrow": "📅", "yesterday": "📅", "monday": "📅",
    "history": "📜", "ancient": "📜", "tradition": "📜", "legend": "📜", "document": "📜",
    "future": "🔮", "predict": "🔮", "forecast": "🔮", "prophecy": "🔮", "vision": "🔮",
    "old": "👴", "elderly": "👴", "grandfather": "👴", "grandmother": "👵",
    "baby": "👶", "infant": "👶", "newborn": "👶", "young": "👶",

    # --- speed, force, change -----------------------------------------------
    "fast": "⚡", "speed": "⚡", "energy": "⚡", "quick": "⚡", "instant": "⚡",
    "electric": "⚡", "electricity": "⚡", "shock": "⚡", "sudden": "⚡",
    "slow": "🐌", "sluggish": "🐌", "delay": "🐌",
    "boom": "💥", "explode": "💥", "explosion": "💥", "blast": "💥", "impact": "💥",
    "smash": "💥", "destroy": "💥", "break": "💥",
    "rocket": "🚀", "launch": "🚀", "space": "🚀", "liftoff": "🚀", "accelerate": "🚀",
    "fire": "🔥", "hot": "🔥", "burning": "🔥", "flame": "🔥", "heat": "🔥",
    "trending": "🔥", "viral": "🔥",
    "fight": "🥊", "battle": "🥊", "punch": "🥊", "boxing": "🥊", "attack": "🥊",
    "war": "⚔️", "conflict": "⚔️", "versus": "⚔️", "competition": "⚔️", "rival": "⚔️",
    "shield": "🛡️", "protect": "🛡️", "defend": "🛡️", "security": "🛡️", "safe": "🛡️",
    "lock": "🔒", "locked": "🔒", "private": "🔒", "encrypted": "🔒", "password": "🔒",
    "unlock": "🔓", "open": "🔓", "access": "🔓", "unlocked": "🔓",
    "key": "🔑", "solve": "🔑", "unlocking": "🔑", "essential": "🔑",
    "tool": "🔧", "fix": "🔧", "repair": "🔧", "maintenance": "🔧", "adjust": "🔧",
    "build": "🔨", "construct": "🔨", "hammer": "🔨", "make": "🔨",
    "cut": "✂️", "trim": "✂️", "edit": "✂️", "remove": "✂️", "delete": "🗑️",
    "trash": "🗑️", "bin": "🗑️", "discard": "🗑️", "rubbish": "🗑️",
    "clean": "🧼", "wash": "🧼", "hygiene": "🧼", "tidy": "🧹", "sweep": "🧹",
    "recycle": "♻️", "reuse": "♻️", "sustainable": "♻️", "environment": "♻️",
    "warning": "⚠️", "danger": "⚠️", "risk": "⚠️", "caution": "⚠️", "hazard": "⚠️",
    "stop": "✋", "halt": "✋", "pause": "✋", "block": "✋",
    "restart": "🔄", "repeat": "🔄", "again": "🔄", "loop": "🔄", "cycle": "🔄",
    "refresh": "🔄", "sync": "🔄", "update": "🔄", "change": "🔄", "switch": "🔄",

    # --- body, health, fitness ----------------------------------------------
    "gym": "🏋️", "workout": "🏋️", "lift": "🏋️", "training": "🏋️", "exercise": "🏋️",
    "run": "🏃", "running": "🏃", "sprint": "🏃", "marathon": "🏃", "jog": "🏃",
    "walk": "🚶", "stroll": "🚶",
    "swim": "🏊", "swimming": "🏊", "pool": "🏊",
    "yoga": "🧘", "meditate": "🧘", "mindful": "🧘", "stretch": "🧘", "breathe": "🧘",
    "health": "🩺", "doctor": "🩺", "medical": "🩺", "checkup": "🩺", "diagnosis": "🩺",
    "hospital": "🏥", "clinic": "🏥", "emergency": "🚑", "ambulance": "🚑",
    "medicine": "💊", "pill": "💊", "drug": "💊", "prescription": "💊", "treatment": "💊",
    "injury": "🩹", "hurt": "🩹", "wound": "🩹", "pain": "🩹", "bandage": "🩹",
    "eye": "👀", "look": "👀", "watch": "👀", "see": "👀", "observe": "👀",
    "ear": "👂", "listen": "👂", "hear": "👂", "sound": "👂",
    "mouth": "👄", "speak": "👄", "voice": "🗣️", "shout": "🗣️",
    "announce": "🗣️",
    "hand": "🤚", "touch": "🤚", "grab": "🤚",
    "point": "👉", "pointing": "👉", "indicate": "👉",
    "foot": "🦶", "leg": "🦵", "kick": "🦵",
    "tooth": "🦷", "teeth": "🦷", "dentist": "🦷",
    "bone": "🦴", "skeleton": "🦴",
    "blood": "🩸", "bleed": "🩸",
    "dream": "💭", "imagine": "💭", "thought": "💭", "wondering": "💭",

    # --- food and drink -----------------------------------------------------
    "food": "🍔", "burger": "🍔", "meal": "🍔", "lunch": "🍔", "dinner": "🍽️",
    "eat": "🍽️", "restaurant": "🍽️", "cuisine": "🍽️", "dish": "🍽️",
    "pizza": "🍕", "italian": "🍕", "slice": "🍕",
    "coffee": "☕", "espresso": "☕", "caffeine": "☕", "morning": "☕", "brew": "☕",
    "tea": "🍵", "beer": "🍺", "pub": "🍺", "wine": "🍷", "drink": "🥤",
    "water": "💧", "hydrate": "💧", "liquid": "💧",
    "cook": "👨\u200d🍳", "chef": "👨\u200d🍳", "recipe": "👨\u200d🍳", "kitchen": "👨\u200d🍳",
    "bake": "🍞", "bread": "🍞", "toast": "🍞",
    "fruit": "🍎", "apple": "🍎", "healthy": "🥗", "salad": "🥗", "vegetable": "🥗",
    "diet": "🥗", "nutrition": "🥗",
    "sweet": "🍬", "sugar": "🍬", "candy": "🍬", "dessert": "🍰", "chocolate": "🍫",
    "spicy": "🌶️", "pepper": "🌶️", "chilli": "🌶️", "hotter": "🌶️",
    "egg": "🥚", "breakfast": "🥚", "cheese": "🧀", "milk": "🥛",
    "hungry": "🍴", "starving": "🍴", "appetite": "🍴", "taste": "😋", "delicious": "😋",
    "yummy": "😋", "tasty": "😋",

    # --- technology ----------------------------------------------------------
    "phone": "📱", "mobile": "📱", "smartphone": "📱", "app": "📱", "text": "📱",
    "computer": "💻", "laptop": "💻", "code": "💻", "coding": "💻", "programming": "💻",
    "software": "💻", "develop": "💻", "developer": "💻", "keyboard": "⌨️", "typing": "⌨️",
    "internet": "🌐", "online": "🌐", "website": "🌐", "web": "🌐", "browser": "🌐",
    "network": "🌐", "domain": "🌐",
    "wifi": "📶", "signal": "📶", "connection": "📶", "bandwidth": "📶",
    "battery": "🔋", "charge": "🔋", "charging": "🔌", "plug": "🔌", "cable": "🔌",
    "robot": "🤖", "ai": "🤖", "automation": "🤖", "bot": "🤖", "machine": "🤖",
    "algorithm": "🤖",
    "camera": "📸", "photo": "📸", "picture": "📸", "photography": "📸", "shot": "📸",
    "video": "🎬", "film": "🎬", "movie": "🎬", "cinema": "🎬", "director": "🎬",
    "scene": "🎬", "clip": "🎬", "footage": "🎬",
    "screen": "🖥️", "monitor": "🖥️", "display": "🖥️", "desktop": "🖥️",
    "print": "🖨️", "printer": "🖨️", "scan": "🖨️",
    "save": "💾", "backup": "💾", "storage": "💾", "disk": "💾", "file": "📁",
    "folder": "📁", "archive": "🗄️", "database": "🗄️", "record": "🗄️",
    "email": "📧", "inbox": "📧", "mail": "📧", "message": "📩", "send": "📤",
    "receive": "📥", "download": "⬇️", "upload": "⬆️",
    "error": "🚨", "alert": "🚨", "incident": "🚨",
    "server": "🗄️", "cloud": "☁️", "hosting": "☁️", "infrastructure": "☁️",
    "settings": "⚙️", "config": "⚙️", "configure": "⚙️", "system": "⚙️",
    "engine": "⚙️", "process": "⚙️", "mechanism": "⚙️",
    "link": "🔗", "connect": "🔗", "url": "🔗", "reference": "🔗", "chain": "🔗",
    "game": "🎮", "gaming": "🎮", "gamer": "🎮", "console": "🎮", "player": "🎮",

    # --- travel and transport -----------------------------------------------
    "travel": "✈️", "flight": "✈️", "fly": "✈️", "plane": "✈️", "airport": "✈️",
    "holiday": "🏖️", "vacation": "🏖️", "beach": "🏖️", "resort": "🏖️", "relax": "🏖️",
    "car": "🚗", "drive": "🚗", "driving": "🚗", "vehicle": "🚗", "traffic": "🚗",
    "bike": "🚴", "cycling": "🚴", "bicycle": "🚴",
    "train": "🚆", "railway": "🚆", "station": "🚆", "commute": "🚆",
    "bus": "🚌", "boat": "⛵", "sail": "⛵", "ship": "🚢", "cruise": "🚢",
    "map": "🗺️", "location": "📍", "place": "📍", "address": "📍", "position": "📍",
    "spot": "📍", "destination": "📍",
    "road": "🛣️", "journey": "🛣️", "route": "🛣️", "path": "🛣️", "direction": "🧭",
    "compass": "🧭", "navigate": "🧭", "guide": "🧭",
    "luggage": "🧳", "suitcase": "🧳", "packing": "🧳", "trip": "🧳",
    "hotel": "🏨", "accommodation": "🏨", "booking": "🏨",
    "home": "🏠", "house": "🏠", "apartment": "🏠", "flat": "🏠", "property": "🏠",
    "rent": "🏠", "moving": "📦",
    "city": "🏙️", "urban": "🏙️", "town": "🏙️", "downtown": "🏙️", "skyline": "🏙️",
    "country": "🗺️", "region": "🗺️", "border": "🗺️",
    "world": "🌍", "global": "🌍", "planet": "🌍", "earth": "🌍", "international": "🌍",

    # --- nature and weather --------------------------------------------------
    "sun": "☀️", "sunny": "☀️", "sunshine": "☀️", "summer": "☀️", "bright": "☀️",
    "rain": "🌧️", "raining": "🌧️", "wet": "🌧️", "storm": "⛈️", "thunder": "⛈️",
    "lightning": "⛈️", "hurricane": "🌪️", "tornado": "🌪️", "chaos": "🌪️",
    "snow": "❄️", "winter": "❄️", "chilly": "❄️",
    "wind": "🌬️", "windy": "🌬️", "breeze": "🌬️",
    "moon": "🌙", "night": "🌙", "evening": "🌙", "midnight": "🌙",
    "grow": "🌱", "seed": "🌱", "plant": "🌱", "sprout": "🌱", "beginning": "🌱",
    "tree": "🌳", "forest": "🌳", "wood": "🌳", "nature": "🌳",
    "flower": "🌸", "bloom": "🌸", "blossom": "🌸", "garden": "🌷",
    "mountain": "⛰️", "hill": "⛰️", "climb": "🧗", "climbing": "🧗", "summit": "⛰️",
    "ocean": "🌊", "sea": "🌊", "tide": "🌊", "surf": "🏄",
    "volcano": "🌋", "eruption": "🌋", "lava": "🌋",
    "desert": "🏜️", "dry": "🏜️", "drought": "🏜️",
    "galaxy": "🌌", "universe": "🌌", "cosmos": "🌌", "astronomy": "🔭",
    "telescope": "🔭",
    "rainbow": "🌈", "colour": "🌈", "color": "🌈", "diversity": "🌈", "spectrum": "🌈",

    # --- animals (only where the idiom is unambiguous) -----------------------
    "dog": "🐕", "puppy": "🐕", "cat": "🐈", "kitten": "🐈",
    "bird": "🐦", "fish": "🐟", "horse": "🐴", "cow": "🐄", "sheep": "🐑",
    "pig": "🐖", "chicken": "🐔", "bee": "🐝", "butterfly": "🦋",
    "lion": "🦁", "tiger": "🐅", "bear": "🐻", "wolf": "🐺", "fox": "🦊",
    "elephant": "🐘", "monkey": "🐒", "shark": "🦈", "whale": "🐋", "dolphin": "🐬",
    "snake": "🐍", "spider": "🕷️", "dinosaur": "🦖", "dragon": "🐉",
    "penguin": "🐧", "owl": "🦉", "eagle": "🦅", "rabbit": "🐇",
    "unicorn": "🦄", "magical": "🦄", "rare": "🦄",

    # --- sport, games, competition ------------------------------------------
    "football": "⚽", "soccer": "⚽", "goal": "🎯", "target": "🎯", "focus": "🎯",
    "aim": "🎯", "objective": "🎯", "precise": "🎯", "accurate": "🎯",
    "basketball": "🏀", "tennis": "🎾", "cricket": "🏏", "golf": "⛳", "rugby": "🏉",
    "baseball": "⚾", "hockey": "🏒", "skate": "🛹", "skateboard": "🛹",
    "race": "🏁", "finish": "🏁", "start": "🏁", "lap": "🏁",
    "chess": "♟️", "strategy": "♟️", "tactic": "♟️", "move": "♟️",
    "dice": "🎲", "gamble": "🎲", "luck": "🍀", "lucky": "🍀", "chance": "🎲",
    "random": "🎲", "bet": "🎲",
    "poker": "🃏", "bluff": "🃏",
    "level": "🎮", "score": "🏅",

    # --- media, art, creativity ---------------------------------------------
    "music": "🎵", "song": "🎵", "melody": "🎵", "tune": "🎵", "audio": "🎧",
    "headphones": "🎧", "podcast": "🎙️", "microphone": "🎙️", "recording": "🎙️",
    "interview": "🎙️", "radio": "📻", "broadcast": "📻", "stream": "📡",
    "streaming": "📡", "satellite": "📡", "transmit": "📡",
    "guitar": "🎸", "piano": "🎹", "drum": "🥁", "rhythm": "🥁",
    "sing": "🎤", "singer": "🎤", "vocal": "🎤", "karaoke": "🎤",
    "dance": "💃", "dancing": "💃", "choreography": "💃",
    "art": "🎨", "paint": "🎨", "design": "🎨", "creative": "🎨", "artist": "🎨",
    "draw": "✏️", "sketch": "✏️", "pencil": "✏️", "outline": "✏️",
    "theatre": "🎭", "theater": "🎭", "drama": "🎭", "acting": "🎭", "performance": "🎭",
    "ticket": "🎟️", "event": "🎟️", "concert": "🎟️", "festival": "🎪", "circus": "🎪",
    "news": "📰", "newspaper": "📰", "press": "📰", "article": "📰", "journalism": "📰",
    "headline": "📰", "media": "📺", "television": "📺", "channel": "📺", "show": "📺",
    "episode": "📺",
    "spotlight": "🔦", "torch": "🔦", "flashlight": "🔦",
    "highlight": "🖍️", "emphasis": "🖍️",

    # --- people, relationships, society -------------------------------------
    "family": "👨\u200d👩\u200d👧", "parent": "👨\u200d👩\u200d👧", "child": "🧒", "kid": "🧒",
    "children": "🧒", "son": "🧒", "daughter": "🧒",
    "friend": "🧑\u200d🤝\u200d🧑", "friendship": "🧑\u200d🤝\u200d🧑", "buddy": "🧑\u200d🤝\u200d🧑",
    "people": "👥", "crowd": "👥", "audience": "👥", "public": "👥", "population": "👥",
    "everyone": "👥", "society": "👥",
    "wedding": "💍", "marriage": "💍", "engaged": "💍", "propose": "💍",
    "pregnant": "🤰", "birth": "👶",
    "king": "👑", "queen": "👑", "royal": "👑", "crown": "👑", "leader": "👑",
    "leadership": "👑",
    "police": "👮", "officer": "👮", "arrest": "👮", "law": "⚖️", "legal": "⚖️",
    "justice": "⚖️", "court": "⚖️", "judge": "⚖️", "lawyer": "⚖️", "fair": "⚖️",
    "balance": "⚖️", "ethics": "⚖️",
    "vote": "🗳️", "election": "🗳️", "ballot": "🗳️", "democracy": "🗳️", "politics": "🏛️",
    "government": "🏛️", "parliament": "🏛️", "institution": "🏛️",
    "prison": "⛓️", "jail": "⛓️", "chained": "⛓️", "trapped": "⛓️",
    "hero": "🦸", "saviour": "🦸", "savior": "🦸", "rescue": "🦸",
    "villain": "🦹", "criminal": "🦹", "thief": "🦹", "steal": "🦹", "fraud": "🦹",
    "scam": "🦹",
    "nurse": "🧑\u200d⚕️", "engineer": "🧑\u200d🔧", "scientist": "🧑\u200d🔬",
    "farmer": "🧑\u200d🌾", "farm": "🚜", "agriculture": "🚜", "tractor": "🚜",

    # --- direction, structure, symbols --------------------------------------
    "up": "⬆️", "higher": "⬆️", "ascend": "⬆️",
    "down": "⬇️", "lower": "⬇️", "falling": "⬇️",
    "left": "⬅️", "back": "⬅️", "previous": "⬅️", "return": "⬅️",
    "forward": "➡️", "next": "➡️", "ahead": "➡️", "continue": "➡️", "proceed": "➡️",
    "list": "📋", "checklist": "📋", "plan": "📋", "agenda": "📋", "task": "📋",
    "step": "📋",
    "pin": "📌", "remember": "📌", "reminder": "📌", "bookmark": "🔖",
    "label": "🔖", "tag": "🔖", "category": "🔖",
    "measure": "📏", "size": "📏", "length": "📏", "exact": "📏",
    "scale": "⚖️", "weight": "🏋️",
    "flag": "🚩", "milestone": "🚩",
    "bell": "🔔", "notification": "🔔", "subscribe": "🔔",
    "mute": "🔇", "silence": "🔇", "silent": "🔇",
    "loud": "🔊", "volume": "🔊", "amplify": "🔊",
    "infinity": "♾️", "endless": "♾️", "forever": "♾️", "unlimited": "♾️",
    "new": "🆕", "fresh": "🆕", "latest": "🆕", "brand": "🆕",
    "free": "🆓", "gratis": "🆓", "complimentary": "🆓",
    "gift": "🎁", "present": "🎁", "surprise": "🎁", "bonus": "🎁", "reward": "🎁",
    "magic": "✨", "sparkle": "✨", "special": "✨", "shine": "✨", "polish": "✨",
    "transform": "✨",
    "bomb": "💣", "threat": "💣", "explosive": "💣",
    "skull": "💀", "dead": "💀", "death": "💀", "die": "💀", "fatal": "💀",
    "ghost": "👻", "haunted": "👻", "spooky": "👻", "alien": "👽", "ufo": "🛸",
    "extraterrestrial": "👽",
}

#: Keys that *are* caption stopwords and are kept in the map deliberately (A9).
#:
#: These four shipped in the original 85-keyword map, and their pictures match the word's actual
#: meaning every time it is spoken: a ✅ on "yes" and an ❌ on "no" are the reference look, not a
#: coincidental match. They are stopwords for *caption highlighting*, which is a different job -
#: emphasising the word "no" in a cue is odd, showing a cross when someone says it is not.
#:
#: The list is explicit so the test that forbids every other stopword has something exact to
#: compare against. Growing it should require the same argument these four have.
STOPWORD_KEYS_KEPT: frozenset[str] = frozenset({"yes", "no", "up", "down"})

_WORD_RE = re.compile(r"[a-z']+")


@dataclass
class EmojiCue:
    """A planned emoji overlay: char + [start, end] window (clip-relative)."""

    char: str
    start: float
    end: float
    slot: int = 0  # position slot, for horizontal spread


def _norm(token: str) -> str:
    """Lowercase a token and strip surrounding punctuation."""
    m = _WORD_RE.findall(token.lower())
    return m[0] if m else ""


# --- Keyword lookup: inflected forms (A10) ---------------------------------- #
#
# The map is keyed on base forms, and lookup was exact, so speech missed constantly:
# "win" hit while "winning", "wins" and "won" did not, and "fired" missed "fire". Emoji are
# planned from spoken words, which arrive inflected far more often than not.
#
# Deliberately a small rule set plus a table of irregulars rather than a real stemmer: no
# new dependency, no surprises, and every transformation is reversible by eye. A Porter
# stemmer would also fold "business" to "busi" and stop matching the map at all.

#: Irregular forms that no suffix rule can reach, mapped to a key in ``KEYWORD_EMOJI``.
_IRREGULAR: dict[str, str] = {
    "won": "win", "winning": "win", "wins": "win",
    "lost": "lose", "loses": "lose", "losing": "lose",
    "thought": "think", "thinking": "think", "thinks": "think",
    "grew": "grow", "grown": "grow", "growing": "grow",
    "blew": "blown", "blowing": "blown",
    "ate": "food", "eating": "food", "eats": "food", "eat": "food",
    "ran": "fast", "running": "fast", "runs": "fast",
    "best": "best", "better": "best",
    "laughed": "laugh", "laughing": "laugh", "laughs": "laugh",
    "cried": "cry", "crying": "cry", "cries": "cry",
    "exploded": "explode", "exploding": "explode", "explodes": "explode",
    "launched": "launch", "launching": "launch", "launches": "launch",
    "celebrated": "celebrate", "celebrating": "celebrate", "celebrates": "celebrate",
    "focused": "focus", "focusing": "focus", "focuses": "focus",
    "stopped": "stop", "stopping": "stop", "stops": "stop",
    "looked": "look", "looking": "look", "looks": "look",
    "worked": "work", "working": "work", "works": "work",
    "powerful": "power", "empowered": "power",
    "moneys": "money", "riches": "rich", "richest": "rich",
    "fastest": "fast", "faster": "fast", "strongest": "strong", "stronger": "strong",
    "smarter": "smart", "smartest": "smart", "happiest": "happy", "happier": "happy",
    "craziest": "crazy", "crazier": "crazy", "funniest": "funny", "funnier": "funny",
    "hottest": "hot", "hotter": "hot", "biggest": "big", "bigger": "big",
    "angrier": "angry", "angriest": "angry", "saddest": "sad", "sadder": "sad",
    "ideas": "idea", "goals": "goal", "targets": "target", "secrets": "secret",
    "questions": "question", "teams": "team", "deals": "deal", "gifts": "gift",
    "stars": "star", "parties": "party", "brains": "brain", "minds": "mind",
    "eyes": "eye", "points": "point", "clocks": "clock", "phones": "phone",
    "cameras": "camera", "videos": "video", "games": "game", "rockets": "rocket",
    "warnings": "warning", "dangers": "danger", "businesses": "business",
}


def _candidate_keys(token: str) -> list[str]:
    """Lookup keys for ``token``, most specific first (A10).

    The token itself always comes first, so an exact match can never be overridden by a
    stemmed one, and the map keeps exactly the meaning it had before.
    """
    if not token:
        return []
    keys = [token]

    irregular = _IRREGULAR.get(token)
    if irregular:
        keys.append(irregular)

    # "-ies" -> "-y" ("parties" -> "party"), before the bare "-s" rule.
    if token.endswith("ies") and len(token) > 4:
        keys.append(token[:-3] + "y")
    # Plural / third person: "wins" -> "win". Not "-ss" ("business", "success").
    if token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        keys.append(token[:-1])
    # "-ed": "fired" -> "fire" (keep the e) and "worked" -> "work".
    if token.endswith("ed") and len(token) > 4:
        keys.append(token[:-1])
        keys.append(token[:-2])
    # "-ing": "firing" -> "fire" (restore the e) and "working" -> "work".
    if token.endswith("ing") and len(token) > 5:
        keys.append(token[:-3])
        keys.append(token[:-3] + "e")
        # Doubled consonant: "winning" -> "win", "stopping" -> "stop".
        stem = token[:-3]
        if len(stem) > 2 and stem[-1] == stem[-2]:
            keys.append(stem[:-1])
    # "-ly": "quickly" -> "quick".
    if token.endswith("ly") and len(token) > 4:
        keys.append(token[:-2])

    seen: set[str] = set()
    return [key for key in keys if not (key in seen or seen.add(key))]


def lookup_emoji(token: str, mapping: dict[str, str]) -> str:
    """The emoji for ``token`` under ``mapping``, or ``""`` (A10).

    Tries the token as spoken first, then its uninflected candidates.
    """
    for key in _candidate_keys(token):
        glyph = mapping.get(key)
        if glyph:
            return glyph
    return ""


def plan_emoji(
    words: list,
    duration: float,
    intensity: str = "standard",
    mode: str = "keyword",
    client=None,
    hold: float = 1.3,
    keyword_indices: Optional[set[int]] = None,
) -> list[EmojiCue]:
    """Plan emoji overlays for a clip.

    Args:
        words: clip-relative words (objects with ``.start``/``.end``/``.text``).
        duration: clip duration (s), used to bound the emoji windows.
        intensity: ``off`` | ``subtle`` | ``standard`` | ``heavy``.
        mode: ``keyword`` or ``ai``.
        client: optional LLM client for ``ai`` mode (falls back to keyword map).
        hold: how long each emoji stays on screen (s).
        keyword_indices: flat indices of the words the *captions* highlight (C19). When given,
            a mapped word in that set outranks every word outside it, so the emoji lands on the
            word the viewer is already being pointed at.

    Returns a spacing-respecting, chronologically ordered list of cues.
    """
    spacing = INTENSITY_SPACING.get(intensity, 0.0)
    if spacing <= 0 or not words:
        return []

    mapping = KEYWORD_EMOJI
    if mode == "ai" and client is not None:
        ai_map = _ai_emoji_map(words, client)
        if ai_map:
            mapping = {**KEYWORD_EMOJI, **ai_map}

    # A11: rank the candidates by salience and keep the strongest, instead of taking whichever
    # matching word happens to arrive first after the stopwatch has elapsed.
    #
    # The old rule was purely temporal: `standard` allows one emoji per five seconds, so the
    # first mapped word after each interval won regardless of whether it mattered. On
    # "so anyway the money was completely gone", "so" is not mapped but "anyway"-class filler
    # often is, and it would take the slot that "money" wanted. Salience is the same signal
    # C11 uses to choose which word to *emphasise*, so the emoji now lands on the same word the
    # caption highlights rather than on an unrelated one a second earlier.
    # C19: the highlighted words, when the caller knows them.
    #
    # A11 already ranked by the *same scorer* the caption highlighter uses, which makes the two
    # agree most of the time. Most of the time is the problem: the highlighter applies a per-cue
    # budget and a floor (the C11 follow-up), so its final selection is not a pure function of
    # salience - and where the two disagreed, the emoji landed on one word while the caption
    # emphasised another. To a viewer that reads as a bug even though each component is behaving
    # exactly as written. Taking the *actual* indices removes the second opinion.
    highlighted = keyword_indices or set()

    candidates: list[tuple[float, float, float, str]] = []
    for index, w in enumerate(words):
        key = _norm(getattr(w, "text", ""))
        glyph = lookup_emoji(key, mapping) if key else ""
        if not glyph:
            continue
        try:
            start = float(getattr(w, "start", 0.0))
        except (TypeError, ValueError):
            continue
        if start != start:      # NaN
            continue
        # A highlighted word sorts ahead of every unhighlighted one regardless of salience, which
        # is the whole point: agreement with the caption matters more than this module's own
        # opinion about which word is strongest.
        priority = 1.0 if index in highlighted else 0.0
        candidates.append((priority, _emoji_salience(w, key), start, glyph))

    if not candidates:
        return []

    # Strongest first; ties break on time so the result stays a pure function of the input -
    # the kinetic determinism properties depend on that.
    candidates.sort(key=lambda c: (-c[0], -c[1], c[2]))

    chosen: list[tuple[float, str]] = []
    used_glyphs: set[str] = set()
    cap = _emoji_cap(intensity, duration)
    for _priority, _salience, start, glyph in candidates:
        if len(chosen) >= cap:
            break
        # A12: the same glyph twice in one clip reads as a template rather than as a reaction,
        # and two identical emoji a few seconds apart is the single most obvious way an
        # automatic overlay looks automatic.
        if glyph in used_glyphs:
            continue
        # Spacing is still enforced, but now against every already-chosen cue rather than only
        # against the previous one in time order - the list is no longer in time order here.
        if any(abs(start - other) < spacing for other, _g in chosen):
            continue
        if min(duration, start + hold) <= start:
            continue
        chosen.append((start, glyph))
        used_glyphs.add(glyph)

    cues: list[EmojiCue] = []
    for slot, (start, glyph) in enumerate(sorted(chosen)):
        end = min(duration, start + hold)
        cues.append(EmojiCue(glyph, round(start, 3), round(end, 3), slot % 3))
    return cues


def _emoji_salience(word: Any, key: str) -> float:
    """How much this word deserves the emoji slot (A11).

    Deliberately reuses the caption keyword planner's own scorer rather than inventing a second
    notion of importance: two different answers to "which word matters here" would put the
    emoji on one word and the highlight on another, which looks like a bug to a viewer even
    though each component is behaving as written.

    Falls back to a length proxy if that import is unavailable, so this module keeps working
    standalone - it is imported by the overlay builder, which must not depend on caption code.
    """
    try:
        from worker.effects.caption_presets import _keyword_salience

        base = float(_keyword_salience(word))
    except Exception:
        base = 2.0 if len(key) >= 6 else 1.0
    # A longer key is a more specific match: "celebrate" carries more than "up".
    return base + min(0.9, len(key) / 20.0)


def _emoji_cap(intensity: str, duration: float) -> int:
    """The most emoji one clip may carry (A12).

    A cap is needed independently of spacing, because spacing alone scales with clip length: a
    three-minute clip at `heavy` could carry sixty emoji and still satisfy every gap. Scaled by
    duration so a 15-second clip and a 3-minute one are both proportionate, with a floor of one -
    an intensity the user switched on should produce at least one.
    """
    per_minute = {"subtle": 3, "standard": 6, "heavy": 12}.get(intensity, 6)
    minutes = max(0.25, float(duration or 0.0) / 60.0)
    return max(1, int(round(per_minute * minutes)))


def _ai_emoji_map(words: list, client) -> dict[str, str]:
    """Ask the LLM for context-aware ``word -> emoji`` pairs. Best-effort."""
    text = " ".join(getattr(w, "text", "") for w in words).strip()
    if not text:
        return {}
    prompt = (
        "For the following short-video transcript, choose up to 12 vivid single "
        "emoji to visually punctuate specific spoken words. Return a JSON object "
        'mapping the lowercase spoken word to one emoji, e.g. {"money":"💰"}. '
        "Only include words that actually appear in the transcript.\n\n"
        f"Transcript:\n{text}"
    )
    try:
        data = client.complete_json(prompt, temperature=0.4, max_tokens=400)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in data.items():
        key = _norm(str(k))
        val = str(v).strip()
        if key and val:
            out[key] = val
    return out


# --------------------------------------------------------------------------- #
# Twemoji asset resolution
# --------------------------------------------------------------------------- #
def emoji_filename(char: str) -> str:
    """Return the vendored PNG filename for an emoji string.

    Codepoints joined by ``-`` in lower-case hex, dropping the ``U+FE0F`` variation
    selector for multi-codepoint sequences — the Twemoji convention, kept as our on-disk
    naming even though the artwork now comes from Noto Emoji (A6), because it is the
    convention every emoji set agrees on modulo case and separator.
    ``scripts/fetch_emoji.py`` translates it to Noto's ``emoji_u<...>.png`` spelling.
    """
    codepoints = [ord(c) for c in char]
    if len(codepoints) > 1:
        codepoints = [cp for cp in codepoints if cp != 0xFE0F]
    return "-".join(f"{cp:x}" for cp in codepoints) + ".png"


#: Retained spelling for callers written against the Twemoji-only version.
twemoji_filename = emoji_filename


# --------------------------------------------------------------------------- #
# Emoji styles (A13)
# --------------------------------------------------------------------------- #
#
# The overlay was tied to one artwork set. Which set is a *look* decision, not a technical one -
# Noto's flat vector style, Twemoji's rounder shapes and OpenMoji's outlined drawings read very
# differently over footage, and a creator with a brand will care which one appears.
#
# Three things differ between the sets, and each of them silently produces a 404 rather than an
# error you can read: the base URL, the case of the hex in the filename (OpenMoji upper-cases it),
# and the separator plus prefix (Noto writes ``emoji_u1f9d1_200d_1f3eb.png`` where the others
# write ``1f9d1-200d-1f3eb.png``). Encoding all three per style is the whole content of this
# registry - it is what stops "switch to OpenMoji" from meaning "silently render no emoji".
#
# **Only Noto is vendored**, and that is deliberate rather than an omission. Committing three
# artwork sets for 326 glyphs would triple the 7 MB the assets already cost, to ship two sets
# that most installs never select. The other two are fetched on demand, so selecting one *does*
# require `EMOJI_ALLOW_DOWNLOAD=true` or a prior `scripts/fetch_emoji.py --style`. When a glyph is
# missing from the selected style and cannot be fetched, resolution falls back to the vendored
# Noto file rather than dropping the overlay: a mixed-style overlay is a cosmetic inconsistency,
# a missing one is a missing feature.


@dataclass(frozen=True)
class EmojiStyle:
    """One artwork set: where to fetch it and how it spells its filenames."""

    name: str
    cdn_base: str
    #: The size the artwork is published at. Recorded because it decides whether compositing is a
    #: downscale or an upscale: Twemoji's 72px is smaller than every target this tool renders, so
    #: choosing it *is* choosing a soft overlay. Stating the number is the honest way to offer it.
    nominal_px: int
    licence: str
    #: OpenMoji publishes ``1F525.png``; the others use lower case.
    upper_hex: bool = False
    #: Noto publishes ``emoji_u1f525.png`` with underscore-joined codepoints.
    remote_prefix: str = ""
    remote_separator: str = "-"

    def remote_filename(self, char: str) -> str:
        """The filename this style publishes ``char`` under."""
        local = emoji_filename(char)
        stem = local[: -len(".png")]
        if self.remote_separator != "-":
            stem = stem.replace("-", self.remote_separator)
        if self.upper_hex:
            stem = stem.upper()
        return f"{self.remote_prefix}{stem}.png"

    def remote_url(self, char: str) -> str:
        return f"{self.cdn_base.rstrip('/')}/{self.remote_filename(char)}"


#: The default style, and the only one committed to the repository.
DEFAULT_STYLE = "noto"

EMOJI_STYLES: dict[str, EmojiStyle] = {
    "noto": EmojiStyle(
        name="noto",
        cdn_base="https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/512",
        nominal_px=512,
        licence="OFL-1.1",
        remote_prefix="emoji_u",
        remote_separator="_",
    ),
    "twemoji": EmojiStyle(
        name="twemoji",
        # jdecked's fork, not twitter/twemoji: the original repository is archived, so pinning it
        # would pin a set that receives no new codepoints.
        cdn_base="https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/72x72",
        nominal_px=72,
        licence="CC-BY-4.0",
    ),
    "openmoji": EmojiStyle(
        name="openmoji",
        cdn_base="https://cdn.jsdelivr.net/gh/hfg-gmuend/openmoji@15.0.0/color/618x618",
        nominal_px=618,
        licence="CC-BY-SA-4.0",
        upper_hex=True,
    ),
}


def resolve_style(name: str | None = None) -> EmojiStyle:
    """The :class:`EmojiStyle` for ``name``, or the configured one, falling back to Noto.

    An unknown name resolves to the default rather than raising: a typo in a setting should
    produce the shipped look, not a job that fails after the transcription has been paid for.
    """
    key = str(name if name is not None else getattr(settings, "emoji_style", DEFAULT_STYLE) or "")
    return EMOJI_STYLES.get(key.strip().lower(), EMOJI_STYLES[DEFAULT_STYLE])


def style_assets_dir(style: EmojiStyle) -> Path:
    """Where ``style``'s PNGs are cached.

    The default style keeps ``emoji_assets_dir`` exactly - that is the committed directory, and
    moving it would orphan 7 MB of vendored artwork. Others get a sibling suffixed with the style
    name, so two styles never overwrite each other's copy of the same filename.
    """
    base = Path(settings.emoji_assets_dir)
    if style.name == DEFAULT_STYLE:
        return base
    return base.with_name(f"{base.name}-{style.name}")


def _default_downloader(url: str, dest: Path) -> bool:
    """Download ``url`` to ``dest``. Returns ``True`` on success."""
    if not settings.emoji_allow_download:
        return False
    try:
        import httpx

        resp = httpx.get(url, timeout=15, follow_redirects=True)
        if resp.status_code == 200 and resp.content:
            dest.write_bytes(resp.content)
            return True
    except Exception:
        return False
    return False


def _usable(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def resolve_asset(
    char: str,
    downloader: Optional[Callable[[str, Path], bool]] = None,
    *,
    style: EmojiStyle | str | None = None,
) -> Optional[Path]:
    """Return a local PNG path for ``char`` in the selected artwork set, or ``None`` (A13).

    Resolution order, and the reason for it:

    1. the selected style's cache directory - the look that was asked for;
    2. the style's CDN, if downloading is permitted;
    3. the **vendored Noto file**, when the selected style is not Noto.

    Step 3 is the interesting one. A style the operator selected but never vendored would
    otherwise drop the overlay entirely on an offline install, so switching style would silently
    switch the feature off. A glyph in the wrong artwork set is a cosmetic inconsistency that is
    visible and correctable; a missing overlay looks like the emoji feature is broken.

    ``downloader`` is injectable for tests.
    """
    chosen = style if isinstance(style, EmojiStyle) else resolve_style(style)
    filename = emoji_filename(char)

    assets = style_assets_dir(chosen)
    assets.mkdir(parents=True, exist_ok=True)
    local = assets / filename
    if _usable(local):
        return local

    fetch = downloader or _default_downloader
    # `emoji_cdn_base` still wins for the default style, so an operator who pointed it at a
    # mirror keeps that mirror.
    if chosen.name == DEFAULT_STYLE:
        url = f"{settings.emoji_cdn_base.rstrip('/')}/{filename}"
    else:
        url = chosen.remote_url(char)
    if fetch(url, local) and _usable(local):
        return local
    # A partial write from a failed fetch would otherwise be returned as a usable asset next time.
    if local.exists() and not _usable(local):
        local.unlink(missing_ok=True)

    if chosen.name != DEFAULT_STYLE:
        fallback = Path(settings.emoji_assets_dir) / filename
        if _usable(fallback):
            return fallback
    return None


# --------------------------------------------------------------------------- #
# ffmpeg overlay graph
# --------------------------------------------------------------------------- #
# Horizontal slots (fractions of frame width) for spreading emoji out.
_SLOT_X = (0.16, 0.74, 0.45)
_SLOT_Y = (0.15, 0.24, 0.15)


def _emoji_px(frame_width: int, size_frac: float) -> int:
    """Emoji width in pixels, at least 2 and always even.

    ``scale=<w>:-1`` derives the height from the aspect ratio and rounds it to an even
    number; giving it an even width keeps the two consistent, and libx264's 4:2:0 chroma
    subsampling requires even dimensions anyway.
    """
    px = int(max(2.0, float(frame_width) * float(size_frac)))
    return px - (px % 2)


#: Emoji placement modes (C19).
#:
#: ``spread`` is the shipped behaviour: three fixed slots across the frame, chosen so consecutive
#: emoji do not stack. It treats the emoji as decoration of the *frame*.
#:
#: ``caption`` treats it as decoration of the *word*, sitting the glyph just clear of the caption
#: block. That is the placement the reference look uses, and it is only sensible now that C19 puts
#: the emoji on the same word the caption highlights - an emoji next to a caption illustrating a
#: word three seconds earlier would read as a mistake rather than as a pairing.
PLACEMENTS: tuple[str, ...] = ("spread", "caption")

#: Vertical offsets, as a fraction of frame height, for the emoji band relative to the caption.
#:
#: The caption block sits inside the C12 safe area; these place the glyph *outside* it on the side
#: away from the frame edge, so the emoji never overlaps the text and never lands under platform
#: chrome. Bottom captions get an emoji above them, top captions get one below, centred captions
#: get one above - there being no "outside" for a centred block, and above reads better than below
#: because the eye arrives at the text after the glyph.
_CAPTION_ADJACENT_Y: dict[str, float] = {
    "bottom": 0.60,
    "top": 0.26,
    "center": 0.34,
}

#: Horizontal slots for caption-adjacent emoji: centre, then offset either side.
#:
#: Centre first because a single emoji paired with a caption belongs above its middle. The offsets
#: exist only so two emoji close in time do not overlap - with one emoji, this is just "centred".
_CAPTION_ADJACENT_X: tuple[float, ...] = (0.5, 0.28, 0.72)


def _caption_adjacent_slot(slot: int, caption_position: str) -> tuple[float, float]:
    """``(x_fraction, y_fraction)`` for a caption-adjacent emoji (C19)."""
    place = (caption_position or "bottom").strip().lower()
    # A nine-position caption (C13) reduces to the three vertical bands that matter here.
    if place.startswith("top"):
        key = "top"
    elif place.startswith("center"):
        key = "center"
    else:
        key = "bottom"
    return _CAPTION_ADJACENT_X[slot % 3], _CAPTION_ADJACENT_Y[key]


def build_overlay(
    cues: list[EmojiCue],
    base_label: str,
    out_label: str,
    *,
    duration: float,
    frame_width: int = 1080,
    size_frac: float = 0.14,
    animate: bool = True,
    resolver: Optional[Callable[[str], Optional[Path]]] = None,
    input_offset: int = 1,
    placement: str = "spread",
    caption_position: str = "bottom",
) -> tuple[list[str], str]:
    """Build ffmpeg inputs + a ``-filter_complex`` snippet for emoji overlays.

    Args:
        cues: planned emoji cues.
        base_label: label of the base video stream (without brackets), e.g. ``v0``.
        out_label: label to assign the final overlaid stream (without brackets).
        duration: clip duration (each looped PNG input is bounded to this).
        frame_width: width of the frame being composited onto, in pixels. ``size_frac`` is
            taken against this. It was hard-coded to 1080 (A8), so a 1:1 (1080) run was
            right by accident and a 16:9 (1920) or square-ish output got an emoji sized
            for a different frame — the overlay *placement* used the real ``W`` while the
            *scale* used a constant, so the two disagreed on what the frame was.
        size_frac: emoji width as a fraction of the frame width.
        animate: alpha "pop" fade-in as each emoji appears.
        resolver: ``char -> Path`` resolver (defaults to :func:`resolve_asset`).
        input_offset: ffmpeg input index of the first emoji PNG (after existing
            inputs such as the base video and any music).
        placement: ``spread`` (the shipped behaviour - three slots across the frame) or
            ``caption`` (C19), which sits the emoji just clear of the caption block so the glyph
            and the word it illustrates read as one element rather than two.
        caption_position: where the captions are, so ``caption`` placement knows which side of
            them to sit on. Ignored by ``spread``.

    Returns ``(input_args, filtergraph)``. When no emoji resolve, returns
    ``([], "")`` and the caller should keep using ``base_label``.
    """
    resolve = resolver or (lambda c: resolve_asset(c))

    resolved: list[tuple[EmojiCue, Path]] = []
    for cue in cues:
        path = resolve(cue.char)
        if path is not None:
            resolved.append((cue, path))
    if not resolved:
        return [], ""

    input_args: list[str] = []
    steps: list[str] = []
    current = base_label
    for i, (cue, path) in enumerate(resolved):
        idx = input_offset + i
        # Loop the still PNG for the clip's duration so its PTS tracks main time.
        input_args += ["-loop", "1", "-t", f"{max(0.1, duration):.3f}", "-i", str(path)]

        # Scale relative to the real frame width, then let overlay place it (A8).
        prep = f"[{idx}:v]scale={_emoji_px(frame_width, size_frac)}:-1,format=rgba"
        if animate:
            prep += f",fade=t=in:st={cue.start:.3f}:d=0.18:alpha=1"
        prep += f"[e{i}]"
        steps.append(prep)

        if placement == "caption":
            sx, sy = _caption_adjacent_slot(cue.slot, caption_position)
        else:
            sx, sy = _SLOT_X[cue.slot % 3], _SLOT_Y[cue.slot % 3]
        nxt = out_label if i == len(resolved) - 1 else f"ov{i}"
        steps.append(
            f"[{current}][e{i}]overlay=x='(W-w)*{sx:g}':y='H*{sy:g}':"
            f"enable='between(t,{cue.start:.3f},{cue.end:.3f})'[{nxt}]"
        )
        current = nxt

    return input_args, ";".join(steps)
