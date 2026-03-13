"""
Degen Scanner - Message Parser
Parses CEX withdrawal messages from Ray Onyx Wallet Tracker bot.

Message structure in Telethon:
- msg.raw_text: plain text like "💸 TRANSFER\n🔹 Bybit 📙\n\n🔹Bybit 📙 transferred 3 SOL to Fsd5...BzyA*"
- msg.entities: contains URLs (Solscan account links, TX links, token links)

We extract:
- CEX name from raw_text (e.g., "Bybit 📙", "OKX 🔲", "WD3T 🐶")
- Amount from raw_text (e.g., "transferred 3 SOL")
- Wallet addresses from entity URLs (solscan.io/account/...)
- TX link from entity URLs (solscan.io/tx/...)
"""

import re
import logging
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List

logger = logging.getLogger(__name__)

# ─── Regex Patterns ───────────────────────────────────────────────

# Amount patterns — try in order (most specific → fallback)
# 1. "transferred 2.03 SOL" (Ray Onyx default)
# 2. "sent 0.7 SOL" (alternative bots)
# 3. Any number followed by SOL (last resort)
AMOUNT_PATTERNS = [
    re.compile(r'transferred\s+([\d,]+\.?\d*)\s+SOL', re.IGNORECASE),
    re.compile(r'sent\s+([\d,]+\.?\d*)\s+SOL', re.IGNORECASE),
    re.compile(r'withdraw(?:al|n)?\s+([\d,]+\.?\d*)\s+SOL', re.IGNORECASE),
    re.compile(r'([\d,]+\.?\d+)\s+SOL', re.IGNORECASE),  # fallback: any X.X SOL
]

# CEX name patterns — try multiple formats
# 1. "🔹CEX_NAME transferred ..." (Ray Onyx default)
# 2. "🔹CEX_NAME sent ..."
# 3. "🔹CEX_NAME" on its own line (just the CEX label)
CEX_NAME_PATTERNS = [
    re.compile(r'🔹\s*(.+?)\s+transferred', re.IGNORECASE),
    re.compile(r'🔹\s*(.+?)\s+sent', re.IGNORECASE),
    re.compile(r'🔹\s*(.+?)\s+withdraw', re.IGNORECASE),
    re.compile(r'🔹\s*(.+?)$', re.MULTILINE),  # fallback: 🔹CEX on own line
]

# Legacy single patterns for backward compat
AMOUNT_PATTERN = AMOUNT_PATTERNS[0]
CEX_NAME_PATTERN = CEX_NAME_PATTERNS[0]

# Solscan account URL → extract wallet address (from entities)
SOLSCAN_ACCOUNT_RE = re.compile(r'https://solscan\.io/account/([1-9A-HJ-NP-Za-km-z]{32,44})')

# Solscan TX URL (from entities)
SOLSCAN_TX_RE = re.compile(r'https://solscan\.io/tx/\S+')

# Transfer indicator
TRANSFER_INDICATOR = "💸"


@dataclass
class ParsedTransfer:
    """Structured data from a parsed CEX withdrawal message."""
    wallet: str           # Destination wallet address
    cex: str              # CEX name/label (e.g., "WD3T 🐶")
    cex_wallet: str       # CEX source wallet address
    amount: float         # SOL amount
    timestamp: datetime   # Message timestamp
    tx_link: Optional[str] = None

    def __repr__(self):
        return (
            f"Transfer({self.cex} -> {self.wallet[:8]}...{self.wallet[-4:]} | "
            f"{self.amount} SOL)"
        )


def extract_urls_from_entities(entities) -> List[str]:
    """Extract all URLs from Telegram message entities."""
    urls = []
    if not entities:
        return urls
    for entity in entities:
        if hasattr(entity, 'url') and entity.url:
            urls.append(entity.url)
    return urls


def parse_message_with_entities(raw_text: str, entities, timestamp: datetime) -> Optional[ParsedTransfer]:
    """
    Parse a CEX withdrawal message using raw_text + entities.

    Args:
        raw_text: msg.raw_text from Telethon (plain text, no markdown)
        entities: msg.entities from Telethon (contains URLs)
        timestamp: Message timestamp

    Returns:
        ParsedTransfer object if parsing succeeds, None otherwise
    """
    if not raw_text or TRANSFER_INDICATOR not in raw_text:
        return None

    try:
        # ─── Extract URLs from entities ───────────────────────
        urls = extract_urls_from_entities(entities)

        # Get all Solscan account addresses from URLs
        all_wallets = []
        tx_link = None

        for url in urls:
            # Check for account URL
            account_match = SOLSCAN_ACCOUNT_RE.search(url)
            if account_match:
                wallet = account_match.group(1)
                # Skip SOL token address
                if not wallet.startswith('So1111'):
                    all_wallets.append(wallet)
                continue

            # Check for TX URL
            if 'solscan.io/tx/' in url and not tx_link:
                tx_link = url

        if len(all_wallets) < 2:
            logger.debug(f"Not enough wallets ({len(all_wallets)}): {raw_text[:80]}...")
            return None

        # CEX wallet = first address, Destination = last different address
        cex_wallet = all_wallets[0]
        dest_wallet = None
        for w in reversed(all_wallets):
            if w != cex_wallet:
                dest_wallet = w
                break

        if not dest_wallet:
            logger.debug(f"No destination wallet found: {raw_text[:80]}...")
            return None

        # ─── Extract CEX name from raw_text ───────────────────
        cex = "Unknown"
        for cex_pattern in CEX_NAME_PATTERNS:
            cex_match = cex_pattern.search(raw_text)
            if cex_match:
                cex = cex_match.group(1).strip()
                # Clean any remaining markdown artifacts
                cex = re.sub(r'\*\*', '', cex)
                cex = re.sub(r'\[|\]', '', cex)
                cex = re.sub(r'^\*|\*$', '', cex)
                cex = cex.strip()
                if cex:
                    break

        # ─── Extract amount from raw_text ─────────────────────
        amount = 0.0
        for amt_pattern in AMOUNT_PATTERNS:
            amount_match = amt_pattern.search(raw_text)
            if amount_match:
                amount_str = amount_match.group(1).replace(',', '')
                try:
                    amount = float(amount_str)
                    if amount > 0:
                        break
                except ValueError:
                    continue

        if amount == 0.0:
            logger.warning(f"⚠️ Amount=0 for message: {raw_text[:200]}")

        parsed = ParsedTransfer(
            wallet=dest_wallet,
            cex=cex,
            cex_wallet=cex_wallet,
            amount=amount,
            timestamp=timestamp,
            tx_link=tx_link,
        )

        logger.info(f"Parsed: {parsed}")
        return parsed

    except Exception as e:
        logger.error(f"Error parsing message: {e} | Text: {raw_text[:100]}...")
        return None


# Keep backward compatibility
def parse_message(text: str, timestamp: datetime) -> Optional[ParsedTransfer]:
    """Legacy parser using msg.text (with markdown). Use parse_message_with_entities for better results."""
    return parse_message_with_entities(text, None, timestamp)


def is_transfer_message(text: str) -> bool:
    """Quick check if a message is a CEX withdrawal (TRANSFER only, not swap/buy/sell)."""
    if not text or TRANSFER_INDICATOR not in text:
        return False
    # Only match "TRANSFER" type — skip SWAP, BUY, SELL, etc.
    first_line = text.split('\n', 1)[0].upper()
    return "TRANSFER" in first_line
