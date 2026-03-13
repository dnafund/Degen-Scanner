"""
Degen Scanner - Pattern Matcher

Matches new tokens against learned patterns from /teach.
Called during autoscan Phase 3 after DevTrace completes.

Matching order (cheap → expensive):
  1. wallet_reuse — DB lookup known_dev_wallets (0 API calls)
  2. funder/collector match — compare trace clusters with DB (0 API calls)
  3. mint_authority — check mint deployer funder (1 API call)

Confidence scoring:
  wallet_reuse:  +0.8
  funder:        +0.7
  collector:     +0.6
  mint_authority: +0.9

Threshold: confidence >= 0.6 → fire alert
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from dev_tracer import TraceResult
from pump_analyzer import CEX_HOT_WALLETS
import teach_store

logger = logging.getLogger(__name__)

MATCH_THRESHOLD = 0.6

# Confidence weights per match type
WEIGHT_WALLET_REUSE = 0.8
WEIGHT_FUNDER = 0.7
WEIGHT_COLLECTOR = 0.6
WEIGHT_MINT_AUTHORITY = 0.9


@dataclass
class PatternMatch:
    """A single pattern match result."""
    match_type: str  # "wallet_reuse", "funder_wallet", "collector_wallet", "mint_authority"
    matched_wallet: str  # the wallet that matched
    case_id: int  # which teach case it matched
    case_mint: str  # original taught token mint
    confidence: float  # match confidence
    details: str = ""  # human-readable detail


@dataclass
class MatchResult:
    """Aggregated match result for a token."""
    mint: str
    matches: list[PatternMatch] = field(default_factory=list)
    total_confidence: float = 0.0

    @property
    def is_match(self) -> bool:
        return self.total_confidence >= MATCH_THRESHOLD

    @property
    def confidence_pct(self) -> int:
        return min(99, int(self.total_confidence * 100))


class PatternMatcher:
    """
    Matches new tokens against learned patterns.
    Stateless — reads from teach_store on each call.
    """

    def check_token(
        self,
        mint: str,
        trace_result: Optional[TraceResult] = None,
        early_buyers: list[str] = None,
    ) -> MatchResult:
        """
        Check a token against all learned patterns.

        Args:
            mint: Token mint address
            trace_result: Optional TraceResult from DevTracer
            early_buyers: Optional list of early buyer wallets (for quick check)

        Returns:
            MatchResult with matches and total confidence
        """
        result = MatchResult(mint=mint)

        # Collect wallets to check
        wallets_to_check = set(early_buyers or [])
        cluster_funders = set()
        cluster_collectors = set()

        if trace_result:
            for buyer in trace_result.all_buyers:
                wallets_to_check.add(buyer.wallet)
            for cluster in trace_result.clusters:
                for wt in cluster.wallets:
                    wallets_to_check.add(wt.wallet)
                if cluster.shared_funder:
                    cluster_funders.add(cluster.shared_funder)
                if cluster.shared_collector:
                    cluster_collectors.add(cluster.shared_collector)
            for trace in trace_result.traces:
                if trace.funder:
                    cluster_funders.add(trace.funder)
                if trace.collector:
                    cluster_collectors.add(trace.collector)

        # ── Match 1: wallet_reuse (0 API calls) ──────────
        if wallets_to_check:
            self._match_wallet_reuse(
                list(wallets_to_check), result
            )

        # ── Match 2: funder/collector match (0 API calls) ─
        if cluster_funders or cluster_collectors:
            self._match_funder_collector(
                cluster_funders, cluster_collectors, result
            )

        # Calculate total confidence (cap at 0.99)
        if result.matches:
            # Use max match + diminishing returns for additional matches
            sorted_conf = sorted(
                [m.confidence for m in result.matches], reverse=True
            )
            total = sorted_conf[0]
            for c in sorted_conf[1:]:
                total += c * 0.3  # diminishing returns
            result.total_confidence = min(0.99, total)

        if result.is_match:
            logger.info(
                f"Pattern match for {mint[:12]}: "
                f"{len(result.matches)} matches, "
                f"confidence={result.confidence_pct}%"
            )

        return result

    def _match_wallet_reuse(
        self,
        wallets: list[str],
        result: MatchResult,
    ) -> None:
        """Check if any wallets are in known_dev_wallets."""
        matches = teach_store.lookup_wallets(wallets)
        seen_cases = set()

        for match in matches:
            # Avoid duplicate matches from same case
            key = (match.wallet, match.case_id)
            if key in seen_cases:
                continue
            seen_cases.add(key)

            # Get case info
            case = teach_store.get_case(match.case_id)
            case_mint = case.mint if case else match.mint

            w_short = f"{match.wallet[:4]}...{match.wallet[-4:]}"
            result.matches.append(PatternMatch(
                match_type="wallet_reuse",
                matched_wallet=match.wallet,
                case_id=match.case_id,
                case_mint=case_mint,
                confidence=WEIGHT_WALLET_REUSE * match.confidence,
                details=f"Known {match.role} wallet {w_short}",
            ))

    def _match_funder_collector(
        self,
        funders: set[str],
        collectors: set[str],
        result: MatchResult,
    ) -> None:
        """Check if trace funders/collectors match learned patterns."""
        all_patterns = teach_store.get_all_patterns(min_confidence=0.1)
        seen = set()

        for pattern in all_patterns:
            if pattern.pattern_type == "funder_wallet":
                if pattern.wallet in funders:
                    key = ("funder", pattern.wallet, pattern.case_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    case = teach_store.get_case(pattern.case_id)
                    w_short = f"{pattern.wallet[:4]}...{pattern.wallet[-4:]}"
                    result.matches.append(PatternMatch(
                        match_type="funder_wallet",
                        matched_wallet=pattern.wallet,
                        case_id=pattern.case_id,
                        case_mint=case.mint if case else "",
                        confidence=WEIGHT_FUNDER * pattern.confidence,
                        details=f"Shared funder {w_short}",
                    ))

            elif pattern.pattern_type == "collector_wallet":
                if pattern.wallet in collectors:
                    key = ("collector", pattern.wallet, pattern.case_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    case = teach_store.get_case(pattern.case_id)
                    w_short = f"{pattern.wallet[:4]}...{pattern.wallet[-4:]}"
                    result.matches.append(PatternMatch(
                        match_type="collector_wallet",
                        matched_wallet=pattern.wallet,
                        case_id=pattern.case_id,
                        case_mint=case.mint if case else "",
                        confidence=WEIGHT_COLLECTOR * pattern.confidence,
                        details=f"Shared collector {w_short}",
                    ))

            elif pattern.pattern_type == "mint_authority":
                # Check if any funder matches a known mint authority
                if pattern.wallet in funders:
                    key = ("mint_auth", pattern.wallet, pattern.case_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    case = teach_store.get_case(pattern.case_id)
                    w_short = f"{pattern.wallet[:4]}...{pattern.wallet[-4:]}"
                    result.matches.append(PatternMatch(
                        match_type="mint_authority",
                        matched_wallet=pattern.wallet,
                        case_id=pattern.case_id,
                        case_mint=case.mint if case else "",
                        confidence=WEIGHT_MINT_AUTHORITY * pattern.confidence,
                        details=f"Same mint authority {w_short}",
                    ))


def format_match_alert(
    mint: str,
    token_name: str,
    token_symbol: str,
    match_result: MatchResult,
) -> str:
    """Format a pattern match alert for Telegram."""
    import html as html_lib

    name = html_lib.escape(token_name or "Unknown")
    symbol = html_lib.escape(token_symbol or "???")

    lines = [
        f"🎯 <b>PATTERN MATCH — {name} (${symbol})</b>\n",
        f"Mint: <code>{mint}</code>",
        f"🎯 Confidence: <b>{match_result.confidence_pct}%</b>\n",
    ]

    for match in match_result.matches[:5]:
        case = teach_store.get_case(match.case_id)
        case_name = ""
        if case:
            case_name = case.token_name or case.token_symbol or case.mint[:12]

        type_emoji = {
            "wallet_reuse": "👛",
            "funder_wallet": "💰",
            "collector_wallet": "📥",
            "mint_authority": "🔑",
        }.get(match.match_type, "❓")

        w_short = f"{match.matched_wallet[:4]}...{match.matched_wallet[-4:]}"
        sol_link = f"https://solscan.io/account/{match.matched_wallet}"
        lines.append(
            f"{type_emoji} <b>{match.match_type}</b>: "
            f"<code>{w_short}</code> "
            f"<a href='{sol_link}'>🔗</a>\n"
            f"   từ case <b>{html_lib.escape(case_name)}</b> "
            f"(conf: {match.confidence:.0%})"
        )

    if len(match_result.matches) > 5:
        lines.append(f"\n<i>... +{len(match_result.matches) - 5} more matches</i>")

    # Links
    dex_link = f"https://dexscreener.com/solana/{mint}"
    pump_link = f"https://pump.fun/{mint}"
    lines.append(
        f"\n<a href='{dex_link}'>DexScreener</a> | "
        f"<a href='{pump_link}'>Pump.fun</a>"
    )

    return "\n".join(lines)
