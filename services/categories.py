"""Categorias e ícones locais; sem dependências de marketplaces ou IA."""

import re
import unicodedata

CATEGORY_ICONS = {
    "mouse": "🖱️", "teclado": "⌨️", "headset": "🎧", "ssd": "💾", "ram": "💾",
    "monitor": "🖥️", "notebook": "💻", "gpu": "🎮", "cpu": "⚙️", "microfone": "🎙️",
    "webcam": "📷", "controle": "🎮", "power_bank": "🔋", "hub": "🔌",
    "carregador": "🔌", "setup": "🛠️", "other": "✨",
}
CATEGORY_ALIASES = {"SSD": "ssd", "memória RAM": "ram", "placa de vídeo": "gpu",
                    "processador": "cpu", "power bank": "power_bank", "mousepad": "setup",
                    "acessório para setup": "setup", "gadget": "setup"}
PATTERNS = (
    ("setup", r"\b(mouse\s*pad|braco articulado|suporte.*(?:monitor|notebook|headset)|light bar|stream deck|fita led)\b"),
    ("mouse", r"\bmouse\b"), ("teclado", r"\b(teclado|keyboard)\b"),
    ("headset", r"\b(headset|headphone|fone.*gamer)\b"),
    ("monitor", r"\bmonitor\b"), ("notebook", r"\b(notebook|laptop|macbook)\b"),
    ("ssd", r"\b(ssd|nvme)\b"), ("ram", r"\b(ram|ddr[345])\b"),
    ("gpu", r"\b(gpu|placa de video|geforce|radeon|rtx|gtx)\b"),
    ("cpu", r"\b(cpu|processador|ryzen|intel core)\b"),
    ("controle", r"\b(joystick|gamepad|controle.*(?:gamer|xbox|playstation|ps[45]|switch|bluetooth))\b"),
    ("microfone", r"\b(microfone|microphone)\b"), ("webcam", r"\bwebcam\b"),
    ("carregador", r"\bcarregador\b"), ("hub", r"\b(hub.*(?:usb|tipo c)|dock.*(?:usb|notebook))\b"),
    ("power_bank", r"\b(power\s*bank|bateria portatil)\b"),
    ("setup", r"\b(setup|smart plug|tomada inteligente|rastreador bluetooth|adaptador.*(?:usb|bluetooth|hdmi))\b"),
)
DOMAIN_CATEGORIES = {
    "COMPUTER_MICE": "mouse", "MICE": "mouse", "KEYBOARDS": "teclado",
    "HEADPHONES": "headset", "HEADSETS": "headset", "MOUSE_PADS": "setup",
    "MONITORS": "monitor", "NOTEBOOKS": "notebook", "SOLID_STATE_DRIVES": "ssd",
    "RAM_MEMORIES": "ram", "VIDEO_CARDS": "gpu", "PROCESSORS": "cpu",
    "MICROPHONES": "microfone", "WEBCAMS": "webcam", "GAMEPADS": "controle",
}


def fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def category_for(title: str) -> str:
    text = fold(title)
    if re.search(r"\b(teclado musical|piano|orgao musical)\b", text):
        return "other"
    return next((category for category, pattern in PATTERNS if re.search(pattern, text)), "other")


def canonical_category(category: str | None) -> str:
    category = CATEGORY_ALIASES.get(category, category)
    return category if category in CATEGORY_ICONS else "other"


def category_icon(category: str | None) -> str:
    return CATEGORY_ICONS[canonical_category(category)]
