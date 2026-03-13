"""
Degen Scanner - Alert Formatter
Formats cluster alerts and sends them to the alert Telegram group.
Supports pagination via inline buttons for alerts with >10 wallets.

Alert format:
🚨 DEGEN CLUSTER DETECTED!
📍 Source: Coinbase Hot 1
🎯 Pattern: Same Amount
⏰ Window: 15 min (14:20 - 14:35)
👛 Wallets: 3
1. 9hqf...BYuu - 0.64 SOL
   🔗 Solscan | 📜 TX
...
💰 Total: 1.94 SOL
"""

import logging
import math
import time
import uuid
from datetime import timedelta
from telethon import Button, TelegramClient

from cluster import Cluster
from splitter import WhaleSplit
from custom_rules import CustomMatch
from config import ALERT_GROUP_ID

# UTC+7 offset for display
UTC7_OFFSET = timedelta(hours=7)

logger = logging.getLogger(__name__)

# ─── Pagination constants ───────────────────────────────────────
WALLETS_PER_PAGE = 10
CACHE_TTL_SECONDS = 3600  # 1 hour

# In-memory cache: alert_id → {header_lines, wallet_lines, footer_lines, total_wallets, created_at}
_alert_cache: dict[str, dict] = {}


def _generate_alert_id() -> str:
    return uuid.uuid4().hex[:8]


def cleanup_cache() -> int:
    """Remove cache entries older than CACHE_TTL_SECONDS. Returns count removed."""
    now = time.monotonic()
    expired = [
        aid for aid, data in _alert_cache.items()
        if now - data["created_at"] > CACHE_TTL_SECONDS
    ]
    for aid in expired:
        del _alert_cache[aid]
    return len(expired)


def _build_pagination_buttons(alert_id: str, page: int, total_pages: int) -> list[list]:
    """Build [← Trước] [2/5] [Tiếp →] inline button row."""
    if total_pages <= 1:
        return []

    buttons = []
    if page > 1:
        buttons.append(Button.inline("← Trước", f"pg:{alert_id}:{page - 1}"))

    buttons.append(Button.inline(f"{page}/{total_pages}", b"noop"))

    if page < total_pages:
        buttons.append(Button.inline("Tiếp →", f"pg:{alert_id}:{page + 1}"))

    return [buttons]


def _format_wallet_table(wallet_data: list[dict]) -> list[str]:
    """Format wallet data as individual lines with copyable wallet address."""
    lines = []
    for entry in wallet_data:
        lines.append(entry["row"])
    return lines


def _paginate(
    header_lines: list[str],
    wallet_data: list[dict],
    footer_lines: list[str],
    page: int,
    alert_id: str,
) -> tuple[str, list[list]]:
    """Assemble text + buttons for a given page."""
    total_wallets = len(wallet_data)
    total_pages = max(1, math.ceil(total_wallets / WALLETS_PER_PAGE))
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * WALLETS_PER_PAGE
    end_idx = start_idx + WALLETS_PER_PAGE
    page_wallets = wallet_data[start_idx:end_idx]

    lines = list(header_lines)
    lines.extend(_format_wallet_table(page_wallets))
    lines.extend(footer_lines)

    buttons = _build_pagination_buttons(alert_id, page, total_pages)
    return "\n".join(lines), buttons


def _cache_alert(
    alert_id: str,
    header_lines: list[str],
    wallet_data: list[dict],
    footer_lines: list[str],
    *,
    full_wallets: list[str] | None = None,
    source_wallet: str | None = None,
):
    """Store alert data in cache for pagination callbacks."""
    entry = {
        "header_lines": header_lines,
        "wallet_data": wallet_data,
        "footer_lines": footer_lines,
        "total_wallets": len(wallet_data),
        "created_at": time.monotonic(),
    }
    if full_wallets is not None:
        entry["full_wallets"] = full_wallets
    if source_wallet is not None:
        entry["source_wallet"] = source_wallet
    _alert_cache[alert_id] = entry


# ─── Helpers ────────────────────────────────────────────────────

def shorten_wallet(wallet: str) -> str:
    """Shorten a wallet address for display: 9hqf...BYuu"""
    if len(wallet) <= 12:
        return wallet
    return f"{wallet[:4]}...{wallet[-4:]}"


def _build_transfer_wallet_data(sorted_transfers, start_number: int = 1) -> list[dict]:
    """Build wallet_data list from transfer objects (cluster / custom pattern)."""
    wallet_data = []
    for i, transfer in enumerate(sorted_transfers, start_number):
        short = shorten_wallet(transfer.wallet)
        solscan_url = f"https://solscan.io/account/{transfer.wallet}"

        links = [f"<a href='{solscan_url}'>🔗</a>"]
        if transfer.tx_link:
            links.append(f"<a href='{transfer.tx_link}'>📜</a>")

        row = f"{i}. <code>{short}</code> - {transfer.amount:.2f} SOL  {' | '.join(links)}"
        wallet_data.append({"row": row})
    return wallet_data


def _build_whale_wallet_data(sorted_wallets, wallet_first_sig: dict[str, str]) -> list[dict]:
    """Build wallet_data list from whale split sorted wallet tuples."""
    wallet_data = []
    for i, (wallet, amount) in enumerate(sorted_wallets, 1):
        short = shorten_wallet(wallet)
        solscan_url = f"https://solscan.io/account/{wallet}"

        links = [f"<a href='{solscan_url}'>🔗</a>"]
        sig = wallet_first_sig.get(wallet)
        if sig:
            tx_url = f"https://solscan.io/tx/{sig}"
            links.append(f"<a href='{tx_url}'>📜</a>")

        row = f"{i}. <code>{short}</code> - {amount:.2f} SOL  {' | '.join(links)}"
        wallet_data.append({"row": row})
    return wallet_data


# ─── Format functions ───────────────────────────────────────────

def format_cluster_alert(cluster: Cluster, page: int = 1) -> tuple[str, list[list], str]:
    """
    Format a cluster into a readable Telegram alert message with pagination.

    Returns:
        (text, buttons, alert_id) — buttons is empty list if ≤10 wallets
    """
    start, end = cluster.time_range
    duration = int((end - start).total_seconds() / 60)

    start_local = start + UTC7_OFFSET
    end_local = end + UTC7_OFFSET

    header_lines = [
        "🚨 <b>DEGEN CLUSTER DETECTED!</b>",
        "",
        f"📍 Source: <b>{cluster.cex}</b>",
        f"🏷 Pattern: <b>{cluster.pattern.label}</b>",
        f"⏰ Window: {duration} min ({start_local:%H:%M} - {end_local:%H:%M})",
        f"👛 Wallets: <b>{cluster.wallet_count}</b>",
        "",
    ]

    sorted_wallets = sorted(cluster.wallets, key=lambda w: w.timestamp)
    wallet_data = _build_transfer_wallet_data(sorted_wallets)

    footer_lines = [
        "",
        f"💰 Total: <b>{cluster.total_amount:.2f} SOL</b>",
    ]

    alert_id = _generate_alert_id()

    if len(wallet_data) > WALLETS_PER_PAGE:
        _cache_alert(alert_id, header_lines, wallet_data, footer_lines)

    text, buttons = _paginate(header_lines, wallet_data, footer_lines, page, alert_id)
    return text, buttons, alert_id


def format_whale_split_alert(split: WhaleSplit, page: int = 1) -> tuple[str, list[list], str]:
    """
    Format a whale split event into a readable Telegram alert message with pagination.

    Returns:
        (text, buttons, alert_id)
    """
    deposit_local = split.deposit_timestamp + UTC7_OFFSET

    short_source = shorten_wallet(split.deposit_wallet)
    solscan_source = f"https://solscan.io/account/{split.deposit_wallet}"

    header_lines = [
        "🐋 <b>WHALE SPLITTER DETECTED!</b>",
        "",
        f"📍 CEX: <b>{split.cex}</b>",
        f"💰 Deposit: <b>{split.deposit_amount:.2f} SOL</b>",
        f"⏰ Deposited: {deposit_local:%H:%M} (UTC+7)",
        f"👛 Source: <code>{short_source}</code>",
        f"   🔗 <a href='{solscan_source}'>Solscan</a>",
    ]

    if split.deposit_tx_link:
        header_lines.append(f"   📜 <a href='{split.deposit_tx_link}'>Deposit TX</a>")

    header_lines.append("")
    header_lines.append(f"🔀 <b>Split to {split.sub_wallet_count} sub-wallets:</b>")
    header_lines.append("")

    # Group transfers by destination wallet (aggregate amounts)
    wallet_amounts: dict[str, float] = {}
    wallet_first_sig: dict[str, str] = {}
    for t in split.sub_transfers:
        wallet_amounts[t.to_wallet] = wallet_amounts.get(t.to_wallet, 0) + t.amount_sol
        if t.to_wallet not in wallet_first_sig:
            wallet_first_sig[t.to_wallet] = t.signature

    sorted_wallets = sorted(wallet_amounts.items(), key=lambda x: x[1], reverse=True)
    wallet_data = _build_whale_wallet_data(sorted_wallets, wallet_first_sig)

    footer_lines = [
        "",
        f"💸 Total split: <b>{split.total_split_amount:.2f} SOL</b>",
    ]

    alert_id = _generate_alert_id()

    # Always cache whale alerts (trace button needs source wallet for BFS)
    full_wallets = [w for w, _ in sorted_wallets]
    _cache_alert(
        alert_id, header_lines, wallet_data, footer_lines,
        full_wallets=full_wallets, source_wallet=split.deposit_wallet,
    )

    text, buttons = _paginate(header_lines, wallet_data, footer_lines, page, alert_id)

    # Add trace button — embed source wallet directly (no cache dependency)
    buttons.append([Button.inline("📋 Trace dàn ví", f"tr:{split.deposit_wallet}")])

    return text, buttons, alert_id


PATTERN_LABELS = {
    "increasing": "📈 Increasing",
    "decreasing": "📉 Decreasing",
    "exact_amount": "🎯 Exact Amount",
    "rapid_fire": "⚡ Rapid Fire",
}


def format_custom_pattern_alert(match: CustomMatch, page: int = 1) -> tuple[str, list[list], str]:
    """
    Format a custom pattern match into a Telegram alert message with pagination.

    Returns:
        (text, buttons, alert_id)
    """
    rule = match.rule
    start, end = match.time_range
    duration = int((end - start).total_seconds() / 60)

    start_local = start + UTC7_OFFSET
    end_local = end + UTC7_OFFSET

    pattern_label = PATTERN_LABELS.get(rule.pattern_type, rule.pattern_type)

    header_lines = [
        "📐 <b>CUSTOM PATTERN MATCHED!</b>",
        "",
        f"📋 Rule: <b>{rule.name}</b>",
        f"📍 CEX: <b>{match.cex_display}</b>",
        f"🏷 Pattern: <b>{pattern_label}</b>",
        f"⏰ Window: {duration} min ({start_local:%H:%M} - {end_local:%H:%M})",
        f"👛 Wallets: <b>{match.wallet_count}</b>",
        "",
    ]

    sorted_wallets = sorted(match.transfers, key=lambda w: w.timestamp)
    wallet_data = _build_transfer_wallet_data(sorted_wallets)

    footer_lines = [
        "",
        f"💰 Total: <b>{match.total_amount:.2f} SOL</b>",
    ]

    alert_id = _generate_alert_id()

    if len(wallet_data) > WALLETS_PER_PAGE:
        _cache_alert(alert_id, header_lines, wallet_data, footer_lines)

    text, buttons = _paginate(header_lines, wallet_data, footer_lines, page, alert_id)
    return text, buttons, alert_id


def format_alert_page(alert_id: str, page: int) -> tuple[str, list[list]] | None:
    """
    Re-render a cached alert at the given page. Used by callback handler.

    Returns:
        (text, buttons) or None if alert_id not found / expired.
    """
    data = _alert_cache.get(alert_id)
    if data is None:
        return None

    text, buttons = _paginate(
        data["header_lines"],
        data["wallet_data"],
        data["footer_lines"],
        page,
        alert_id,
    )

    # Re-add trace button for whale alerts
    if "full_wallets" in data and "source_wallet" in data:
        buttons.append([Button.inline("📋 Trace dàn ví", f"tr:{data['source_wallet']}")])

    return text, buttons


def get_trace_source(alert_id: str) -> str | None:
    """Get source wallet for a whale split alert. Used by trace callback for BFS."""
    data = _alert_cache.get(alert_id)
    if data is None:
        return None
    return data.get("source_wallet")


def format_cex_withdrawal_alert(event: dict, label: str) -> str:
    """
    Format a real-time CEX withdrawal alert from webhook data.

    Args:
        event: Webhook event dict with native_transfers, signature, etc.
        label: CEX wallet label (e.g., "Binance Hot 1")

    Returns:
        HTML-formatted alert text
    """
    sig = event.get("signature", "")
    sig_short = f"{sig[:8]}...{sig[-8:]}" if len(sig) > 16 else sig
    fee_payer = event.get("fee_payer", "")
    timestamp = event.get("timestamp", 0)

    lines = [
        "🔴 <b>LIVE CEX WITHDRAWAL!</b>",
        "",
        f"📍 Source: <b>{label}</b>",
    ]

    # Show native SOL transfers
    total_sol = 0.0
    native_transfers = event.get("native_transfers", [])
    for i, nt in enumerate(native_transfers[:10], 1):
        from_addr = nt.get("fromUserAccount", "")
        to_addr = nt.get("toUserAccount", "")
        lamports = nt.get("amount", 0)
        amount_sol = lamports / 1_000_000_000

        if amount_sol < 0.01:
            continue

        total_sol += amount_sol
        to_short = shorten_wallet(to_addr)
        solscan_url = f"https://solscan.io/account/{to_addr}"
        lines.append(
            f"{i}. <code>{to_short}</code> - {amount_sol:.2f} SOL  "
            f"<a href='{solscan_url}'>🔗</a>"
        )

    if total_sol > 0:
        lines.append("")
        lines.append(f"💰 Total: <b>{total_sol:.2f} SOL</b>")

    tx_url = f"https://solscan.io/tx/{sig}"
    lines.append(f"📜 <a href='{tx_url}'>TX: {sig_short}</a>")

    return "\n".join(lines)


def format_token_transfer_alert(event: dict, label: str, mint: str) -> str:
    """
    Format a real-time token transfer alert from webhook data.

    Args:
        event: Webhook event dict with token_transfers, signature, etc.
        label: Token label (e.g., "BONK")
        mint: Token mint address

    Returns:
        HTML-formatted alert text
    """
    sig = event.get("signature", "")
    sig_short = f"{sig[:8]}...{sig[-8:]}" if len(sig) > 16 else sig

    lines = [
        "🪙 <b>LIVE TOKEN TRANSFER!</b>",
        "",
        f"🏷 Token: <b>{label}</b>",
        f"📍 Mint: <code>{shorten_wallet(mint)}</code>",
    ]

    token_transfers = event.get("token_transfers", [])
    relevant = [
        tt for tt in token_transfers
        if tt.get("mint", "") == mint
    ]

    for i, tt in enumerate(relevant[:10], 1):
        from_addr = tt.get("fromUserAccount", "")
        to_addr = tt.get("toUserAccount", "")
        amount = tt.get("tokenAmount", 0)

        from_short = shorten_wallet(from_addr)
        to_short = shorten_wallet(to_addr)

        from_url = f"https://solscan.io/account/{from_addr}"
        to_url = f"https://solscan.io/account/{to_addr}"

        lines.append(
            f"{i}. <a href='{from_url}'>{from_short}</a> → "
            f"<a href='{to_url}'>{to_short}</a>  "
            f"<b>{amount:,.2f}</b>"
        )

    if not relevant:
        # Fallback: show native transfers if no matching token transfers
        for i, nt in enumerate(event.get("native_transfers", [])[:5], 1):
            to_addr = nt.get("toUserAccount", "")
            lamports = nt.get("amount", 0)
            amount_sol = lamports / 1_000_000_000
            if amount_sol < 0.001:
                continue
            to_short = shorten_wallet(to_addr)
            lines.append(f"{i}. → <code>{to_short}</code> - {amount_sol:.4f} SOL")

    tx_url = f"https://solscan.io/tx/{sig}"
    lines.append("")
    lines.append(f"📜 <a href='{tx_url}'>TX: {sig_short}</a>")

    return "\n".join(lines)


async def send_alert(client: TelegramClient, cluster: Cluster) -> bool:
    """
    Send a cluster alert to the alert group.

    Args:
        client: Telethon client instance
        cluster: Detected cluster to alert about

    Returns:
        True if sent successfully, False otherwise
    """
    try:
        text, buttons, _ = format_cluster_alert(cluster)

        await client.send_message(
            ALERT_GROUP_ID,
            text,
            parse_mode='html',
            link_preview=False,
            buttons=buttons if buttons else None,
        )

        logger.info(
            f"✅ Alert sent: {cluster.cex} | {cluster.pattern.label} | "
            f"{cluster.wallet_count} wallets | {cluster.total_amount:.2f} SOL"
        )
        return True

    except Exception as e:
        logger.error(f"❌ Failed to send alert: {e}")
        return False
