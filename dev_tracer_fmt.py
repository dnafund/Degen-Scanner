"""
Degen Scanner - Dev Tracer Telegram Formatter

Formats TraceResult into Telegram HTML messages.
Each chunk stays ≤ 3800 chars (Telegram limit 4096 with margin).
"""

from typing import List

from alerter import shorten_wallet
from dev_tracer import TraceResult, DevCluster

# Cluster type labels
CLUSTER_TYPE_LABELS = {
    "dev": "🔴 DEV",
    "suspicious": "🟡 SUS",
    "hunter": "🟢 HUNTER",
    "unknown": "⚪ UNKNOWN",
}


def format_trace_result(result: TraceResult) -> List[str]:
    """
    Format trace result into Telegram HTML message chunks.
    Each chunk ≤ 3800 chars.
    """
    if result.error:
        return [
            f"❌ <b>Trace failed</b>\n\n"
            f"Token: <code>{shorten_wallet(result.mint)}</code>\n"
            f"Error: {result.error}"
        ]

    chunks: List[str] = []

    # ─── Header ──────────────────────────────────────────
    name = result.token_name or "Unknown"
    symbol = result.token_symbol or "???"
    total_buyers = len(result.all_buyers)
    unique_wallets = len(set(b.wallet for b in result.all_buyers))
    traced = len(result.traces)
    with_funder = sum(1 for t in result.traces if t.funder)
    with_collector = sum(1 for t in result.traces if t.collector)

    # Total tokens captured by clusters (cap at 100% — buy volume can exceed supply)
    cluster_tokens = sum(c.total_tokens for c in result.clusters)
    cluster_pct = min(
        (cluster_tokens / result.total_supply * 100) if result.total_supply > 0 else 0,
        100.0,
    )

    dev_clusters = [c for c in result.clusters if c.cluster_type == "dev"]
    sus_clusters = [c for c in result.clusters if c.cluster_type == "suspicious"]
    dev_pct = min(sum(c.pct_supply for c in dev_clusters), 100.0)
    sus_pct = min(sum(c.pct_supply for c in sus_clusters), 100.0)

    header = (
        f"🕵️ <b>Dev Wallet Trace</b>\n\n"
        f"Token: <b>{name}</b> (${symbol})\n"
        f"Mint: <code>{result.mint}</code>\n"
        f"Bonding Curve: <code>{result.bonding_curve[:16]}...</code>\n\n"
        f"📊 <b>Scan Summary:</b>\n"
        f"  Buys parsed: <b>{total_buyers}</b> ({unique_wallets} unique)\n"
        f"  Wallets traced: <b>{traced}</b>\n"
        f"  With funder: <b>{with_funder}</b>\n"
        f"  With collector: <b>{with_collector}</b>\n"
        f"  Clusters found: <b>{len(result.clusters)}</b>\n\n"
    )

    # Risk summary
    if dev_pct > 0 or sus_pct > 0:
        header += f"⚠️ <b>Risk Exposure:</b>\n"
        if dev_pct > 0:
            header += f"  🔴 Dev clusters: <b>{dev_pct:.1f}%</b> supply ({len(dev_clusters)} clusters)\n"
        if sus_pct > 0:
            header += f"  🟡 Suspicious: <b>{sus_pct:.1f}%</b> supply ({len(sus_clusters)} clusters)\n"
        header += f"  📊 Total clustered: <b>{cluster_pct:.1f}%</b> supply\n"
    else:
        header += "✅ No dev clusters detected\n"

    chunks.append(header)

    # ─── Per-cluster details ────────────────────────────
    if result.clusters:
        cluster_text = "🔍 <b>Cluster Details</b>\n"

        for cluster in result.clusters:
            section = _format_cluster(cluster)

            if len(cluster_text) + len(section) > 3800:
                chunks.append(cluster_text)
                cluster_text = f"🔍 <b>Clusters (cont.)</b>\n{section}"
            else:
                cluster_text += section

        if cluster_text.strip():
            chunks.append(cluster_text)

    # ─── Copy-all wallet list (single monospace block) ──
    if result.clusters:
        all_wallets = []
        for cluster in result.clusters:
            for m in cluster.wallets:
                if m.wallet not in all_wallets:
                    all_wallets.append(m.wallet)

        total = len(all_wallets)
        batch_size = 80  # ~44 chars × 80 ≈ 3520 chars per block
        for batch_idx in range(0, total, batch_size):
            batch = all_wallets[batch_idx:batch_idx + batch_size]
            batch_addrs = "\n".join(batch)
            if total <= batch_size:
                header = f"📋 <b>All clustered wallets</b> ({total}) — tap to copy:\n"
            else:
                part = batch_idx // batch_size + 1
                header = f"📋 <b>Wallets</b> (part {part}) — tap to copy:\n"
            chunks.append(f"{header}<pre>{batch_addrs}</pre>")

    return chunks


def _format_cluster(cluster: DevCluster) -> str:
    """Format a single cluster section."""
    label = CLUSTER_TYPE_LABELS.get(cluster.cluster_type, cluster.cluster_type)
    members = cluster.wallets

    lines = [
        f"\n{'─' * 28}",
        f"<b>Cluster {cluster.cluster_id}</b> — {label} "
        f"(score: {cluster.confidence:.0f})",
        f"  👛 <b>{len(members)}</b> wallets | "
        f"📊 <b>{min(cluster.pct_supply, 100.0):.1f}%</b> supply "
        f"({cluster.total_tokens:,.0f} tokens)",
    ]

    # Shared funder
    if cluster.shared_funder:
        short_funder = shorten_wallet(cluster.shared_funder)
        funder_url = f"https://solscan.io/account/{cluster.shared_funder}"

        # Count how many wallets funded by this funder
        funded_count = sum(
            1 for m in members if m.funder == cluster.shared_funder
        )
        cex_tag = ""
        for m in members:
            if m.funder == cluster.shared_funder and m.funder_is_cex:
                cex_tag = f" ({m.funder_is_cex})"
                break

        lines.append(
            f"  💰 Funder: <code>{short_funder}</code>{cex_tag} "
            f"→ {funded_count}/{len(members)} wallets "
            f"<a href='{funder_url}'>🔗</a>"
        )

    # Shared collector
    if cluster.shared_collector:
        short_collector = shorten_wallet(cluster.shared_collector)
        collector_url = f"https://solscan.io/account/{cluster.shared_collector}"

        collected_count = sum(
            1 for m in members if m.collector == cluster.shared_collector
        )
        lines.append(
            f"  📥 Collector: <code>{short_collector}</code> "
            f"← {collected_count}/{len(members)} wallets "
            f"<a href='{collector_url}'>🔗</a>"
        )

    # Wallet list (max 10, then "... +N more")
    lines.append("")
    for i, m in enumerate(members[:10], 1):
        short = shorten_wallet(m.wallet)
        solscan_url = f"https://solscan.io/account/{m.wallet}"
        token_display = f"{m.token_amount:,.0f}" if m.token_amount else "?"

        funder_tag = ""
        if m.funder_is_cex:
            funder_tag = f" ← {m.funder_is_cex}"
        elif m.funder:
            funder_tag = f" ← {shorten_wallet(m.funder)}"

        lines.append(
            f"  {i}. <code>{short}</code> "
            f"{token_display} tokens "
            f"({m.sol_spent:.3f} SOL)"
            f"{funder_tag} "
            f"<a href='{solscan_url}'>🔗</a>"
        )

    if len(members) > 10:
        lines.append(f"  <i>... +{len(members) - 10} more</i>")

    lines.append("")
    return "\n".join(lines)
