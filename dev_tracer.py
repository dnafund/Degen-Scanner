"""
Degen Scanner - Dev Wallet Tracer

Convergence-based dev wallet detection for Solana tokens.

Pipeline:
  1. Token metadata + estimate total txns
  2. Get earliest buyers (fast path or deep path depending on token size)
  3. Trace 5 hops upstream (funder chain) + 5 hops downstream (collector chain)
  4. Find convergence wallets (shared ancestors/descendants)
  5. Expand from convergence: splitter -> destinations, collector -> 2-hop sources
  6. Cluster expanded wallets by shared convergence origin
  7. Filter: keep clusters with 3+ wallets, exclude CEX collectors

Fast path (< 5000 txns): get_token_transactions gives all txns directly
Deep path (> 5000 txns): getSignaturesForAddress RPC + parseTransactions API

Usage:
    tracer = DevTracer(helius=helius_client)
    result = await tracer.trace(mint_address)
"""

import asyncio
import logging
import random
import aiohttp
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from helius import HeliusClient, is_on_curve
from pump_analyzer import CEX_HOT_WALLETS, FEE_VAULTS, derive_ata, derive_bonding_curve_pda
from config import FREE_RPC_ENDPOINTS

logger = logging.getLogger(__name__)

# Limits
MAX_EARLY_WALLETS_TO_TRACE = 400  # trace this many AFTER activity filter
MAX_CONCURRENT = 20
TRACE_TIMEOUT = 3600  # seconds (60 min — deep pagination for mega-tokens can take 20+ min)
PUMP_DEFAULT_SUPPLY = 1_000_000_000
DEEP_PATH_THRESHOLD = 20000  # switch to deep path if total sigs > this
MAX_OLDEST_TXNS = 0  # parse ALL sigs (0 = all)
HOP_DEPTH = 5  # trace this many hops in each direction


# ─── Dataclasses ──────────────────────────────────────────────

@dataclass
class BuyerInfo:
    """A buyer with token amount."""
    wallet: str
    token_amount: float
    sol_spent: float
    buy_timestamp: int
    signature: str


@dataclass
class WalletTrace:
    """Trace of a buyer: funding source + profit destination."""
    wallet: str
    token_amount: float
    sol_spent: float
    funder: str
    funder_amount: float
    collector: str
    collector_amount: float
    funder_is_cex: str


@dataclass
class DevCluster:
    """A cluster of wallets linked by shared convergence."""
    cluster_id: str
    cluster_type: str       # "dev", "suspicious", "hunter", "unknown"
    wallets: list
    shared_funder: str
    shared_collector: str
    total_tokens: float
    pct_supply: float
    confidence: float


@dataclass
class TraceResult:
    """Complete trace result for a token."""
    mint: str
    token_name: str = ""
    token_symbol: str = ""
    total_supply: float = PUMP_DEFAULT_SUPPLY
    bonding_curve: str = ""
    all_buyers: List[BuyerInfo] = field(default_factory=list)
    traces: List[WalletTrace] = field(default_factory=list)
    clusters: List[DevCluster] = field(default_factory=list)
    wallet_origins: dict = field(default_factory=dict)
    error: str = ""


# ─── Union-Find ──────────────────────────────────────────────

def _find(parent: dict, x: str) -> str:
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def _union(parent: dict, a: str, b: str) -> None:
    ra, rb = _find(parent, a), _find(parent, b)
    if ra != rb:
        parent[rb] = ra


# ─── DevTracer ───────────────────────────────────────────────

class DevTracer:
    """
    Traces dev wallets using convergence expansion methodology.
    """

    def __init__(self, helius: HeliusClient):
        self.helius = helius

    async def trace(
        self,
        mint: str,
        on_progress: Optional[Callable] = None,
    ) -> TraceResult:
        """
        Main trace pipeline.

        Args:
            mint: Token mint address
            on_progress: Optional async callback(step, message) for progress updates
        """
        result = TraceResult(mint=mint)

        async def progress(step: int, msg: str):
            logger.info(f"Dev trace [{step}]: {msg}")
            if on_progress:
                try:
                    await on_progress(step, msg)
                except Exception:
                    pass

        try:
            async with asyncio.timeout(TRACE_TIMEOUT):
                await self._run_pipeline(mint, result, progress)
        except TimeoutError:
            result.error = f"Trace timed out ({TRACE_TIMEOUT}s limit)"
            logger.warning(f"Dev trace timeout for {mint[:12]}...")
        except Exception as e:
            result.error = str(e)
            logger.error(f"Dev trace error: {e}", exc_info=True)

        return result

    async def _run_pipeline(
        self,
        mint: str,
        result: TraceResult,
        progress: Callable,
    ) -> None:
        sem = asyncio.Semaphore(MAX_CONCURRENT)

        # ── Step 1: Token metadata ──────────────────────────
        await progress(1, "Fetching token metadata...")
        asset = await self.helius.get_das_asset(mint)
        if asset:
            meta = asset.get("content", {}).get("metadata", {})
            result.token_name = meta.get("name", "")
            result.token_symbol = meta.get("symbol", "")
            ti = asset.get("token_info", {})
            if ti.get("supply"):
                result.total_supply = ti["supply"] / (10 ** ti.get("decimals", 6))

        # ── Step 2: Get earliest buyers ─────────────────────
        # First pass: quick fetch to estimate size
        await progress(2, "Fetching transaction signatures...")
        all_sigs = await self._get_all_signatures(mint, max_pages=60)
        total_sigs = len(all_sigs)

        await progress(2, f"Found {total_sigs:,} signatures...")
        logger.info(f"Dev trace: {total_sigs} total sigs for {mint[:12]}...")

        # For mega-tokens (hit 60-page limit), the newest 60k sigs miss the
        # earliest buys (bonding curve phase). Use BC cursor jump: derive the
        # bonding curve PDA, use its sigs as cursor to jump directly to the
        # bonding phase in the BC token account (~30s vs 30min deep pagination).
        bonding_cursor_sig = ""  # PDA sig for bonding-era cursor jumps
        bc_token_account = ""   # BC's token account (for filtering)
        if total_sigs >= 60000:
            await progress(2, f"Large token ({total_sigs:,}+ sigs), scanning bonding curve...")
            bc_phase_sigs, bonding_cursor_sig, bc_token_account = await self._get_bonding_curve_phase_sigs(mint, sem)
            if bc_phase_sigs:
                # Merge: newest sigs (context) + bonding phase sigs (earliest buys)
                seen = {s["signature"] for s in all_sigs}
                for s in bc_phase_sigs:
                    if s["signature"] not in seen:
                        all_sigs.append(s)
                await progress(2, f"Merged {len(bc_phase_sigs):,} bonding phase sigs")

        bonding_curve = ""
        if total_sigs > 1000:
            await progress(2, f"Deep scan - parsing {len(all_sigs):,} sigs for buyers...")
            buyers, bonding_curve = await self._get_buyers_deep(mint, all_sigs, sem)
        else:
            await progress(2, f"Fast scan ({total_sigs:,} txns)...")
            buyers, bonding_curve = await self._get_buyers_fast(mint)

        # Derive bonding curve PDA if not detected from txns
        if not bonding_curve:
            try:
                bonding_curve = derive_bonding_curve_pda(mint)
            except Exception:
                pass

        result.bonding_curve = bonding_curve

        if not buyers:
            result.error = "No buy transactions found"
            return

        # Debug: log earliest 5 buyers + total count
        early_5 = sorted(buyers.items(), key=lambda x: x[1]["ts"])[:5]
        logger.info(
            f"Dev trace: {len(buyers)} buyers found. "
            f"Earliest 5: {[w[:8] for w, _ in early_5]}"
        )

        # Convert to BuyerInfo list
        for w, info in buyers.items():
            result.all_buyers.append(BuyerInfo(
                wallet=w,
                token_amount=info["tokens"],
                sol_spent=info["sol"],
                buy_timestamp=info["ts"],
                signature="",
            ))

        # (Token recipients from direct transfers will be discovered
        #  during step 3 tracing via token transfer tracking)

        # ── Step 2a: Augment with current token holders (DAS) ──
        # For mega-tokens, getSignaturesForAddress is unreliable and misses
        # dev wallets that received tokens via transfer (not direct buy).
        # DAS getTokenAccounts is deterministic — returns all current holders.
        das_holder_set = set()
        if total_sigs >= 20000:
            await progress(2, "Fetching current token holders via DAS...")
            das_holders = await self._get_token_holders_das(mint, max_pages=3)
            for w in das_holders:
                if w not in buyers and is_on_curve(w) and w != bonding_curve:
                    buyers[w] = {"tokens": 0, "sol": 0, "ts": 999999999}
                    das_holder_set.add(w)
            if das_holder_set:
                logger.info(f"DAS holders: +{len(das_holder_set)} added to buyer pool")

        # ── Step 2b: Augment with ALL token account owners (getProgramAccounts) ──
        # DAS only returns holders with balance > 0. getProgramAccounts returns
        # ALL token accounts (including 0-balance), catching wallets that bought
        # via DEX (ALT-indexed, invisible to getSignaturesForAddress) and sold.
        gpa_holder_set = set()
        if total_sigs >= 5000:
            await progress(2, "Fetching all token account owners via getProgramAccounts...")
            gpa_owners = await self._get_all_token_owners_gpa(mint)
            for w in gpa_owners:
                if w not in buyers and is_on_curve(w) and w != bonding_curve:
                    buyers[w] = {"tokens": 0, "sol": 0, "ts": 999999999}
                    gpa_holder_set.add(w)
            if gpa_holder_set:
                logger.info(f"GPA owners: +{len(gpa_holder_set)} added to buyer pool "
                            f"(total GPA: {len(gpa_owners)})")

        # Add DAS/GPA-discovered wallets to result.all_buyers
        for w, info in buyers.items():
            if not any(b.wallet == w for b in result.all_buyers):
                result.all_buyers.append(BuyerInfo(
                    wallet=w, token_amount=info["tokens"],
                    sol_spent=info["sol"], buy_timestamp=info["ts"],
                    signature="",
                ))

        buyer_set = set(buyers.keys())
        early_sorted = sorted(buyers.items(), key=lambda x: x[1]["ts"])

        # ── Step 2.5: Filter high-activity wallets ────────
        # Primary pool: first 400 by timestamp (proven to find core matches)
        primary = [w for w, _ in early_sorted[:MAX_EARLY_WALLETS_TO_TRACE]]
        # Secondary pool: remaining buyers, scan for fresh wallets
        # Increase cap when DAS/GPA holders exist (they sort to end with ts=999999999)
        sec_cap = 5000 if (das_holder_set or gpa_holder_set) else 2000
        secondary_candidates = [w for w, _ in early_sorted[MAX_EARLY_WALLETS_TO_TRACE:sec_cap]]

        # 2-step filter: quick check (1 page) first, then deep check only borderline wallets
        await progress(2, f"Quick-checking activity of {len(primary)} primary wallets...")
        sig_counts = await self._quick_sig_check(primary, sem)
        # Wallets with 1000 sigs (hit page limit) need deep check to see if >10k
        borderline = [w for w in primary if sig_counts.get(w, 0) >= 1000]
        if borderline:
            await progress(2, f"Deep-checking {len(borderline)} high-activity wallets...")
            deep_counts = await self._batch_estimate_sig_counts(borderline, sem)
            sig_counts.update(deep_counts)

        # For secondary, use 1-page quick check (enough to detect <100 sigs)
        if secondary_candidates:
            await progress(2, f"Scanning {len(secondary_candidates)} more wallets for fresh devs...")
            sec_counts = await self._quick_sig_check(secondary_candidates, sem)
            sig_counts.update(sec_counts)

        filtered_wallets = []
        flagged_5k = set()
        removed_10k = 0
        for w in primary:
            count = sig_counts.get(w, 0)
            if count >= 10000:
                removed_10k += 1
                continue
            if count >= 5000:
                flagged_5k.add(w)
            filtered_wallets.append(w)

        # From secondary pool, add only fresh wallets (<200 sigs = likely dev wallets)
        # GPA wallets with sig_count=0 (rate limit error) are included since
        # they're verified token holders.
        fresh_added = 0
        gpa_recovered = 0
        for w in secondary_candidates:
            count = sig_counts.get(w, 0)
            if 0 < count < 200:
                filtered_wallets.append(w)
                fresh_added += 1
            elif count == 0 and w in gpa_holder_set:
                filtered_wallets.append(w)
                gpa_recovered += 1

        if removed_10k or flagged_5k or fresh_added or gpa_recovered:
            logger.info(
                f"Activity filter: {removed_10k} removed (>10k sigs), "
                f"{len(flagged_5k)} flagged (5k-10k sigs), "
                f"{fresh_added} fresh wallets added from secondary pool"
                + (f", {gpa_recovered} GPA recovered" if gpa_recovered else "")
            )

        # ── High-bot-ratio expansion ──────────────────────
        # If >50% of primary pool are bots, check ALL existing buyers
        # (not just primary 400) to find non-bot wallets beyond position 400.
        # This avoids expensive re-parsing — just expands the activity check.
        bot_ratio = removed_10k / max(len(primary), 1)
        non_bot_count = len(filtered_wallets)
        if bot_ratio > 0.5 and non_bot_count < 200 and len(early_sorted) > MAX_EARLY_WALLETS_TO_TRACE:
            # Check ALL buyers beyond primary pool
            remaining = [w for w, _ in early_sorted[MAX_EARLY_WALLETS_TO_TRACE:]]
            await progress(2,
                f"High bot ratio ({bot_ratio:.0%}), checking {len(remaining)} more buyers..."
            )
            remaining_counts = await self._quick_sig_check(remaining, sem)
            sig_counts.update(remaining_counts)

            # Add non-bot wallets from remaining pool
            for w in remaining:
                count = remaining_counts.get(w, 0)
                if count >= 10000:
                    continue
                if count >= 5000:
                    flagged_5k.add(w)
                filtered_wallets.append(w)

            logger.info(
                f"High-bot expansion: {len(early_sorted)} total buyers, "
                f"{len(filtered_wallets)} non-bot wallets now"
            )

        # Cap total wallets to trace — expand for high-bot tokens or GPA augmentation
        max_to_trace = MAX_EARLY_WALLETS_TO_TRACE
        if bot_ratio > 0.5:
            max_to_trace = min(len(filtered_wallets), 800)
        elif gpa_holder_set and fresh_added > 0:
            max_to_trace = min(len(filtered_wallets), 600)
        early_wallets = filtered_wallets[:max_to_trace]

        await progress(3, f"Tracing {len(early_wallets)} wallets ({HOP_DEPTH} hops)...")

        # ── Step 3: Trace both directions ───────────────────
        traced = await self._trace_both_directions(
            early_wallets, sem, mint=mint, sig_counts=sig_counts
        )

        # Collect token recipients and sources discovered during tracing
        all_token_recipients = set()
        token_distributor_map = defaultdict(set)  # {source: set of recipients}
        trace_dict = {}
        for item in traced:
            w, up, down = item[0], item[1], item[2]
            trace_dict[w] = (up, down)
            if len(item) > 3 and item[3]:
                all_token_recipients.update(item[3])
            if len(item) > 4 and item[4]:
                for src, recips in item[4].items():
                    token_distributor_map[src].update(recips)

        # Add token recipients to buyer pool (they received this token via transfer)
        new_from_token = 0
        for w in all_token_recipients:
            if w not in buyers and is_on_curve(w):
                buyers[w] = {"tokens": 0, "sol": 0, "ts": 0}
                buyer_set.add(w)
                new_from_token += 1
        if new_from_token:
            logger.info(f"Token transfer tracking: added {new_from_token} recipients to buyer pool")

        # Log token distribution clusters
        for src, recips in token_distributor_map.items():
            if len(recips) >= 3:
                logger.info(f"Token distributor {src[:12]}: sent tokens to {len(recips)} wallets")

        # ── Step 3b: Re-trace token recipients ──
        # Token recipients discovered in step 3 need to be traced too
        # so their funders/collectors appear in convergence.
        if new_from_token > 0:
            new_wallets = [w for w in all_token_recipients
                           if w in buyer_set and w not in trace_dict]
            if new_wallets:
                await progress(3, f"Re-tracing {len(new_wallets)} token recipients...")
                traced2 = await self._trace_both_directions(
                    new_wallets, sem, mint=mint, sig_counts=sig_counts
                )
                for item in traced2:
                    w, up, down = item[0], item[1], item[2]
                    trace_dict[w] = (up, down)
                    # Collect new token recipients from re-traced wallets
                    if len(item) > 3 and item[3]:
                        all_token_recipients.update(item[3])
                    if len(item) > 4 and item[4]:
                        for src, recips in item[4].items():
                            token_distributor_map[src].update(recips)
                traced.extend(traced2)
                logger.info(f"Re-trace: {len(traced2)} token recipients traced")

        # ── Step 3-DAS: Trace fresh DAS holders ──
        # DAS holders added in step 2a need tracing so their funders/collectors
        # appear in convergence. Only trace low-activity ones (<200 sigs).
        if das_holder_set:
            in_trace = sum(1 for w in das_holder_set if w in trace_dict)
            high_sigs = sum(1 for w in das_holder_set
                            if w not in trace_dict and sig_counts.get(w, 0) >= 200)
            zero_sigs = sum(1 for w in das_holder_set
                            if w not in trace_dict and sig_counts.get(w, 0) == 0)
            logger.info(
                f"DAS trace check: {len(das_holder_set)} holders, "
                f"{in_trace} already traced, {high_sigs} high-sig, {zero_sigs} zero-sig"
            )
            das_to_trace = [w for w in das_holder_set
                            if w in buyer_set and w not in trace_dict
                            and 0 < sig_counts.get(w, 0) < 200]
            if das_to_trace:
                await progress(3, f"Tracing {len(das_to_trace)} token holders...")
                traced_das = await self._trace_both_directions(
                    das_to_trace[:200], sem, mint=mint, sig_counts=sig_counts
                )
                for item in traced_das:
                    w, up, down = item[0], item[1], item[2]
                    trace_dict[w] = (up, down)
                    if len(item) > 3 and item[3]:
                        all_token_recipients.update(item[3])
                    if len(item) > 4 and item[4]:
                        for src, recips in item[4].items():
                            token_distributor_map[src].update(recips)
                traced.extend(traced_das)
                logger.info(f"DAS holder trace: {len(traced_das)} holders traced")

        # ── Step 3c: Deep ATA search for token distributors ──
        # Token distributors (including PDAs) with many sigs can't be traced
        # normally (newest 200 txns miss old token distributions). Search
        # their ATAs directly to find all token distribution recipients.
        #
        # First: discover distributors via ATA check on buyer wallets.
        # Step 3's token tracking only sees newest 100 txns — for old tokens,
        # the distributor interaction is buried. ATA check goes directly to
        # the buyer's token-specific account to find who sent them tokens.
        if total_sigs > 5000:
            await progress(3, "Checking buyer token accounts for distributors...")
            ata_distributors = await self._discover_distributors_via_ata(
                buyers, mint, bonding_curve, sig_counts, sem,
            )
            if ata_distributors:
                for src, recips in ata_distributors.items():
                    token_distributor_map[src].update(recips)

        protocol_pdas = set()  # Protocol PDAs (Raydium AMM, etc.) to exclude

        if token_distributor_map:
            high_activity_sources = set()
            for src, recips in token_distributor_map.items():
                # Include distributors with 3+ recipients — even PDAs (off-curve)
                # that wouldn't be in buyer_set. Skip the bonding curve itself.
                if (len(recips) >= 3
                        and src != bonding_curve
                        and src != bc_token_account):
                    high_activity_sources.add(src)

            if high_activity_sources:
                await progress(3, f"Scanning {len(high_activity_sources)} distributors for token transfers...")
                new_recipients, source_map, protocol_pdas = await self._bonding_era_token_search(
                    list(high_activity_sources), mint, bonding_cursor_sig, sem,
                )
                # Update token_distributor_map with full bonding-era results
                for src, recips in source_map.items():
                    token_distributor_map[src].update(recips)
                    logger.info(
                        f"Bonding-era distributor {src[:12]}: "
                        f"{len(recips)} recipients added to token_distributor_map"
                    )
                if new_recipients:
                    already_in = sum(1 for w in new_recipients if w in buyers)
                    off_curve = sum(1 for w in new_recipients
                                    if w not in buyers and not is_on_curve(w))
                    logger.info(
                        f"Bonding-era search: {len(new_recipients)} recipients, "
                        f"{already_in} already in buyers, {off_curve} off-curve"
                    )
                    added = 0
                    for w in new_recipients:
                        if w not in buyers and is_on_curve(w):
                            buyers[w] = {"tokens": 0, "sol": 0, "ts": 0}
                            buyer_set.add(w)
                            added += 1
                            if added >= 300:
                                break
                    if added:
                        logger.info(f"Bonding-era search: +{added} token recipients from distributors")
                        # Trace new recipients (cap at 200 to limit time)
                        new_to_trace = [w for w in new_recipients
                                        if w in buyer_set and w not in trace_dict][:200]
                        if new_to_trace:
                            await progress(3, f"Tracing {len(new_to_trace)} distributor recipients...")
                            traced3 = await self._trace_both_directions(
                                new_to_trace, sem, mint=mint, sig_counts=sig_counts
                            )
                            for item in traced3:
                                w2, up, down = item[0], item[1], item[2]
                                trace_dict[w2] = (up, down)
                                if len(item) > 4 and item[4]:
                                    for src2, recips2 in item[4].items():
                                        token_distributor_map[src2].update(recips2)
                            traced.extend(traced3)
                            logger.info(f"Distributor recipients traced: {len(traced3)}")

        # ── Step 3d: Trace PDA distributor recipients ──
        # PDA recipients need tracing so mini-convergence in step 6 can
        # find shared funders. Without trace data, they're invisible.
        pda_to_trace = []
        for src, recips in token_distributor_map.items():
            if (is_on_curve(src) or src == bonding_curve
                    or src == bc_token_account):
                continue
            for w in recips:
                if w in buyer_set and w not in trace_dict:
                    pda_to_trace.append(w)
        if pda_to_trace:
            logger.info(f"PDA trace: {len(pda_to_trace)} un-traced PDA recipients")
            # Quick-check activity of un-traced PDA recipients
            pda_unchecked = [w for w in pda_to_trace if w not in sig_counts]
            if pda_unchecked:
                logger.info(f"PDA trace: sig-checking {len(pda_unchecked)} unchecked wallets")
                pda_counts = await self._quick_sig_check(pda_unchecked, sem)
                sig_counts.update(pda_counts)
            # Log distribution
            zero_sig = sum(1 for w in pda_to_trace if sig_counts.get(w, 0) == 0)
            low_sig = sum(1 for w in pda_to_trace if 0 < sig_counts.get(w, 0) < 500)
            high_sig = sum(1 for w in pda_to_trace if sig_counts.get(w, 0) >= 500)
            logger.info(
                f"PDA trace: {low_sig} low-sig (<500), "
                f"{high_sig} high-sig (>=500), {zero_sig} zero-sig"
            )
            # Only trace low-activity recipients
            pda_fresh = [w for w in pda_to_trace
                         if 0 < sig_counts.get(w, 0) < 500]
            if pda_fresh:
                await progress(3, f"Tracing {len(pda_fresh)} PDA recipients...")
                traced_pda = await self._trace_both_directions(
                    pda_fresh[:300], sem, mint=mint, sig_counts=sig_counts
                )
                for item in traced_pda:
                    w, up, down = item[0], item[1], item[2]
                    trace_dict[w] = (up, down)
                traced.extend(traced_pda)
                logger.info(f"PDA recipient trace: {len(traced_pda)} wallets traced")

        # ── Step 3e: Discover hidden DEX buyers (ALT-indexed) ──
        # Raydium/DEX swaps use versioned transactions with Address Lookup
        # Tables. getSignaturesForAddress doesn't index ALT-referenced
        # accounts, so DEX buyers are invisible to the sig-based buyer scan.
        # Fix: check all traced wallets for token accounts via
        # getTokenAccountsByOwner (returns 0-balance accounts too).
        # Only do this for protocol PDA distributors (GpMZbSM2GgvT-style).
        if protocol_pdas and trace_dict:
            # Check traced wallets not already connected to any protocol PDA
            already_connected = set()
            for pda in protocol_pdas:
                already_connected |= token_distributor_map.get(pda, set())

            candidates_to_check = [
                w for w in trace_dict
                if w not in already_connected
                and w in buyer_set
                and is_on_curve(w)
                and sig_counts.get(w, 0) < 500
            ]
            if candidates_to_check:
                await progress(3, f"Checking {len(candidates_to_check)} wallets for hidden DEX buys...")
                session = await self.helius._get_session()

                async def _check_token_acct(wallet):
                    async with sem:
                        key = random.choice(self.helius._keys) if self.helius._keys else ""
                        url = f"https://mainnet.helius-rpc.com/?api-key={key}"
                        payload = {
                            "jsonrpc": "2.0", "id": 1,
                            "method": "getTokenAccountsByOwner",
                            "params": [wallet, {"mint": mint},
                                       {"encoding": "jsonParsed"}],
                        }
                        try:
                            async with session.post(url, json=payload) as resp:
                                if resp.status != 200:
                                    return None
                                data = await resp.json()
                                accts = data.get("result", {}).get("value", [])
                                if accts:
                                    return wallet
                        except Exception:
                            pass
                    return None

                results_dex = await asyncio.gather(
                    *[_check_token_acct(w) for w in candidates_to_check],
                    return_exceptions=True,
                )
                dex_buyers = {r for r in results_dex if isinstance(r, str)}

                if dex_buyers:
                    # Add to the first (largest) protocol PDA's distributor map
                    main_pda = max(protocol_pdas,
                                   key=lambda p: len(token_distributor_map.get(p, set())))
                    token_distributor_map[main_pda].update(dex_buyers)
                    logger.info(
                        f"Hidden DEX buyers: {len(dex_buyers)} traced wallets "
                        f"have {mint[:8]} token accounts, added to {main_pda[:12]}"
                    )

        # ── Step 4: Find convergence ────────────────────────
        await progress(4, f"Finding convergence from {len(traced)} traces...")
        convergence, up_map, down_map, cex_convergence = self._find_convergence(
            traced, buyer_set
        )

        # NOTE: Token-based convergence removed — it creates false connections.
        # Token "distribution" includes DEX sales (market maker → buyer),
        # which links unrelated wallets. SOL flow convergence + timing is
        # more reliable. Token recipients are still added to the buyer pool
        # (step 3) for tracing purposes.

        if not convergence:
            # Fallback 1: simple 1-hop clustering
            await progress(4, "No convergence found, trying behavioral clustering...")
            result.traces, result.clusters = await self._fallback_behavioral(
                early_wallets, buyers, sig_counts, mint, result.total_supply, sem
            )
            if not result.clusters:
                # Fallback 2: simple funder/collector clustering
                await progress(4, "Trying direct funder/collector clustering...")
                result.traces, result.clusters = await self._fallback_simple_trace(
                    early_wallets, buyers, result.total_supply, sem
                )
            return

        # ── Step 5: Verify convergence wallets ─────────────
        conv_sorted = sorted(convergence.items(), key=lambda x: -len(x[1]))
        await progress(5, f"Verifying {min(len(conv_sorted), 30)} convergence wallets...")

        good_conv = await self._verify_convergence(
            conv_sorted[:30], convergence, sem
        )

        # ── Step 6: Dev wallet identification ──────────────
        # Separate upstream (funder) and downstream (collector) convergence.
        # Upstream convergence is weaker — CEX wallets fund everyone.
        # Downstream convergence is stronger — shared collector = coordination.
        # Score buyers by how many convergence wallets connect to them.
        qualifying = []
        for conv_w, info in good_conv.items():
            connected = [b for b in info["buyers"] if b in buyer_set]
            if len(connected) < 3:
                continue
            total_contacts = max(len(info["sent_to"]) + len(info["recv_from"]), 1)
            ratio = len(connected) / total_contacts

            # Determine type: funder (upstream) vs collector (downstream)
            is_funder = conv_w in up_map and len(up_map.get(conv_w, set())) >= 3
            is_collector = conv_w in down_map and len(down_map.get(conv_w, set())) >= 3

            # Fee vaults: pure noise, skip completely
            if conv_w in FEE_VAULTS:
                continue

            # CEX funders: skip ratio checks, use count threshold
            is_cex = conv_w in CEX_HOT_WALLETS
            if is_cex:
                if len(connected) >= 15:
                    qualifying.append((
                        conv_w, info, connected, ratio, 0, "cex_funder"
                    ))
                continue

            # Skip high-volume wallets with few buyer connections
            if ratio > 1.0 and len(connected) < 8:
                logger.info(
                    f"Skip high-volume {conv_w[:12]}: "
                    f"ratio {ratio:.0%}, {len(connected)} buyers"
                )
                continue

            # For pure funders, require buyer ratio above threshold.
            # Slide threshold down for funders with many connections:
            # high count is strong evidence even at lower ratio.
            funder_min_ratio = (
                0.10 if len(connected) >= 15
                else 0.15 if len(connected) >= 8
                else 0.20
            )
            if is_funder and not is_collector and ratio < funder_min_ratio:
                logger.info(
                    f"Skip low-ratio funder {conv_w[:12]}: "
                    f"{len(connected)} buyers / {total_contacts} contacts "
                    f"= {ratio:.0%} (min {funder_min_ratio:.0%})"
                )
                continue

            # For collectors, use lower ratio threshold (stronger signal)
            if is_collector and not is_funder and ratio < 0.10:
                logger.debug(
                    f"Skip low-ratio collector {conv_w[:12]}: "
                    f"{len(connected)} buyers / {total_contacts} contacts "
                    f"= {ratio:.0%}"
                )
                continue

            # Mixed or unknown: use moderate threshold
            if not is_funder and not is_collector and ratio < 0.20:
                continue

            # Use tighter timing window for funders (5 min)
            # vs collectors/mixed (1 hour)
            window_secs = 300 if (is_funder and not is_collector) else 3600
            conv_type = ("funder" if is_funder and not is_collector
                         else "collector" if is_collector and not is_funder
                         else "mixed")
            qualifying.append((
                conv_w, info, connected, ratio, window_secs, conv_type
            ))

        # Process ALL qualifying wallets (no top-5 limit)
        qualifying.sort(key=lambda x: -len(x[2]))
        dev_wallets = set()
        wallet_origin = defaultdict(set)
        buyer_conv_count = Counter()  # buyer → count of convergence wallets
        buyer_non_cex_count = {}  # buyer → count of non-CEX convergence

        for conv_w, info, connected, ratio, window_secs, conv_type in qualifying:
            is_cex = conv_type == "cex_funder"
            buyers_with_ts = [(b, buyers[b]["ts"]) for b in connected if b in buyers]
            buyers_with_ts.sort(key=lambda x: x[1])
            buy_times = [ts for _, ts in buyers_with_ts]

            if len(buy_times) >= 3:
                # CEX funders: use ALL connected buyers (timing meaningless)
                if is_cex:
                    connected = [b for b, _ in buyers_with_ts]
                    logger.info(
                        f"CEX {conv_w[:12]} ({CEX_HOT_WALLETS.get(conv_w, '?')}): "
                        f"{len(connected)} buyers"
                    )
                else:
                    # Find densest window
                    best_start, best_end = 0, 0
                    i = 0
                    for j in range(len(buy_times)):
                        while buy_times[j] - buy_times[i] > window_secs:
                            i += 1
                        if (j - i) > (best_end - best_start):
                            best_start, best_end = i, j

                    window_count = best_end - best_start + 1
                    window_spread = buy_times[best_end] - buy_times[best_start]

                    if window_count < 3:
                        logger.info(
                            f"Skip {conv_type} {conv_w[:12]}: "
                            f"best {window_secs//60}min window = {window_count}"
                        )
                        continue

                    # Skip batch processing for funders that look like CEX batch
                    # deposits (high activity + tight timing). Real dev funders may
                    # also batch-fund but tend to have lower activity.
                    if (window_spread < 10 and window_count >= 5
                            and conv_type == "funder"):
                        funder_sigs = sig_counts.get(conv_w, 0)
                        if conv_w in CEX_HOT_WALLETS or conv_w in FEE_VAULTS or funder_sigs >= 1000:
                            logger.info(
                                f"Skip batch {conv_type} {conv_w[:12]}: "
                                f"{window_count} wallets in {window_spread:.0f}s "
                                f"(sigs={funder_sigs})"
                            )
                            continue
                        logger.info(
                            f"Keep batch {conv_type} {conv_w[:12]}: "
                            f"{window_count} wallets in {window_spread:.0f}s "
                            f"(sigs={funder_sigs}, not CEX)"
                        )

                    # For large collectors (30+), use ALL connected buyers.
                    # Timing window loses early/late buyers. Multi-convergence
                    # (CEX + non-CEX) provides the noise filtering.
                    if len(buyers_with_ts) >= 30 and conv_type in ("collector", "mixed"):
                        connected = [b for b, _ in buyers_with_ts]
                    else:
                        connected = [b for b, _ in buyers_with_ts[best_start:best_end + 1]]

                    # Quality score: penalize weak clusters
                    quality = window_count * ratio
                    # Focused funders (<30 contacts, 20%+ ratio) are likely real
                    # dev funders even with low quality score
                    is_focused = (
                        conv_type == "funder"
                        and total_contacts < 100
                        and ratio >= 0.20
                        and len(connected) >= 3
                    )
                    if quality < 1.5 and not is_focused:
                        logger.info(
                            f"Skip weak {conv_type} {conv_w[:12]}: "
                            f"score {quality:.1f} (count={window_count} × ratio={ratio:.0%})"
                        )
                        continue

                    logger.info(
                        f"Dev {conv_type} {conv_w[:12]}: "
                        f"{window_count}/{len(buyers_with_ts)} buyers "
                        f"in {window_secs//60}min window "
                        f"({window_spread:.0f}s spread), ratio {ratio:.0%}, "
                        f"score {quality:.1f}"
                    )
            else:
                quality = len(connected) * ratio
                if quality < 1.5:
                    continue
                logger.info(
                    f"Dev {conv_type} {conv_w[:12]}: "
                    f"{len(connected)} buyers, ratio {ratio:.0%}, "
                    f"score {quality:.1f}"
                )

            for buyer in connected:
                buyer_conv_count[buyer] += 1
                wallet_origin[buyer].add(conv_w)
                if not is_cex:
                    buyer_non_cex_count[buyer] = buyer_non_cex_count.get(buyer, 0) + 1

        # Multi-convergence: require 2+ connections AND strong non-CEX signal.
        # Broad collectors (25+ connections) are common intermediaries — a wallet
        # connected only to CEX + broad collector is weak. Require either:
        # 1. At least one small (<25) non-CEX source (high precision), OR
        # 2. Multiple (3+) non-CEX sources (cross-confirmation even if broad)
        BROAD_THRESHOLD = 25
        multi_connected = set()
        broad_only_skipped = 0
        for b, count in buyer_conv_count.items():
            if count < 2:
                continue
            non_cex = buyer_non_cex_count.get(b, 0)
            if non_cex < 1:
                continue
            origins = wallet_origin.get(b, set())
            has_strong_non_cex = any(
                o not in CEX_HOT_WALLETS and o not in FEE_VAULTS
                and len(convergence.get(o, set())) < BROAD_THRESHOLD
                for o in origins
            )
            if has_strong_non_cex or non_cex >= 3:
                multi_connected.add(b)
            else:
                broad_only_skipped += 1
        if broad_only_skipped:
            logger.info(
                f"Broad-collector filter: skipped {broad_only_skipped} wallets "
                f"with only broad (>={BROAD_THRESHOLD}) non-CEX sources"
            )
        if len(multi_connected) >= 3:
            # ── Core source filter ──
            # A "core" source connects to many qualifying wallets.
            # Wallets connected ONLY to weak sources (few connections
            # to other qualifying wallets) are likely noise.
            if len(multi_connected) >= 10:
                source_hub = defaultdict(int)
                for w in multi_connected:
                    for o in wallet_origin.get(w, set()):
                        source_hub[o] += 1
                core_min = max(7, int(len(multi_connected) * 0.15))
                core_sources = {
                    s for s, c in source_hub.items()
                    if c >= core_min
                }
                if core_sources:
                    before = len(multi_connected)
                    multi_connected = {
                        w for w in multi_connected
                        if wallet_origin.get(w, set()) & core_sources
                    }
                    removed = before - len(multi_connected)
                    if removed:
                        logger.info(
                            f"Core source filter: -{removed} wallets "
                            f"(no source with {core_min}+ connections). "
                            f"Core: {[(s[:12], c) for s, c in source_hub.items() if c >= core_min]}"
                        )

            # ── Source integration filter ──
            # Sources whose wallets have zero overlap with the
            # top source's wallets are likely from an independent
            # network (e.g. a collector used by non-dev wallets).
            # Remove such sources and recheck multi-convergence.
            if core_sources and len(multi_connected) >= 10:
                source_wallets: dict[str, set[str]] = defaultdict(set)
                for w in multi_connected:
                    for o in wallet_origin.get(w, set()):
                        if o in core_sources:
                            source_wallets[o].add(w)
                if source_wallets:
                    top_src = max(source_wallets, key=lambda s: len(source_wallets[s]))
                    top_wallets = source_wallets[top_src]
                    non_integrated = set()
                    for src, ws in source_wallets.items():
                        if src == top_src:
                            continue
                        overlap = len(ws & top_wallets)
                        if len(ws) >= 5 and overlap == 0:
                            non_integrated.add(src)
                    if non_integrated:
                        before_int = len(multi_connected)
                        # Remove non-integrated sources from origins,
                        # then drop wallets with <2 remaining core sources
                        to_remove = set()
                        for w in multi_connected:
                            origins = wallet_origin.get(w, set())
                            remaining = (origins & core_sources) - non_integrated
                            if len(remaining) < 2:
                                to_remove.add(w)
                        multi_connected -= to_remove
                        if to_remove:
                            logger.info(
                                f"Source integration filter: -{len(to_remove)} wallets "
                                f"(non-integrated sources: "
                                f"{[(s[:12], len(source_wallets[s])) for s in non_integrated]}, "
                                f"0 overlap with top source {top_src[:12]})"
                            )

            dev_wallets = multi_connected
            core_dev_wallets = set(multi_connected)  # save for reverse trace
            logger.info(
                f"Multi-convergence: {len(multi_connected)} wallets "
                f"with 2+ connections, 1+ non-CEX "
                f"(from {len(buyer_conv_count)} total)"
            )
        else:
            # Fall back to all connected buyers
            dev_wallets = set(buyer_conv_count.keys())
            core_dev_wallets = set(dev_wallets)
            logger.info(
                f"Single-convergence fallback: {len(dev_wallets)} wallets "
                f"(multi found only {len(multi_connected)})"
            )

        # ── Distributor-based clustering ──
        # PDA distributors (off-curve) are strong dev signals.
        # Instead of adding ALL fresh recipients (too many FPs), run
        # mini-convergence: only add recipients that share a funder with
        # other PDA recipients (evidence of coordinated funding).
        # Non-PDA distributors: only when SOL-flow convergence is weak.
        if token_distributor_map:
            dist_additions = 0
            for src, recips in token_distributor_map.items():
                if (src == bonding_curve or src == bc_token_account
                        or src in CEX_HOT_WALLETS or src in FEE_VAULTS):
                    continue
                connected = [w for w in recips if w in buyer_set]
                if len(connected) < 5:
                    continue
                src_is_pda = not is_on_curve(src)
                existing_devs = [w for w in connected if w in dev_wallets]

                # Non-PDA: only when SOL convergence is weak
                if not src_is_pda:
                    if len(dev_wallets) >= 10:
                        continue
                    if len(existing_devs) < 2:
                        continue
                    fresh = [w for w in connected
                             if sig_counts.get(w, 0) < 100
                             and w not in dev_wallets]
                    if len(fresh) < 3:
                        continue
                    for w in fresh:
                        dev_wallets.add(w)
                        wallet_origin[w].add(src)
                        dist_additions += 1
                    logger.info(
                        f"Distributor cluster {src[:12]}: "
                        f"{len(connected)} connected, {len(existing_devs)} devs, "
                        f"{len(fresh)} fresh added (PDA=False)"
                    )
                    continue

                # PDA distributor: mini-convergence approach.
                # Find recipients that share SOL funders with each other.
                if len(connected) < 10:
                    continue

                # Skip protocol PDAs (Raydium AMM, etc.) — too many connected
                # wallets, creating massive FPs. Any DEX pool connects to ALL
                # buyers, making shared-funder analysis meaningless.
                # Check by connected count AND by token account count (catches
                # pools like GpMZbSM2GgvT with 386k token accounts).
                if len(connected) > 200:
                    logger.info(
                        f"PDA distributor {src[:12]}: SKIPPED — "
                        f"{len(connected)} connected (likely protocol PDA)"
                    )
                    continue
                # Protocol PDAs are filtered at the top of this loop via
                # the protocol_pdas set (discovered in _bonding_era_token_search)

                # Build funder map for PDA recipients
                pda_funder_map = defaultdict(set)  # funder → PDA recipients
                traced_count = 0
                for w in connected:
                    up_down = trace_dict.get(w)
                    if not up_down:
                        continue
                    up, _ = up_down
                    traced_count += 1
                    if up:
                        for af in up[0].get("all_funders", set()):
                            if af and af not in CEX_HOT_WALLETS and af not in FEE_VAULTS:
                                pda_funder_map[af].add(w)

                # Funders shared by 3+ PDA recipients = coordinated group
                coordinated = set()
                coord_funders = []
                for funder, funded_recipients in pda_funder_map.items():
                    if len(funded_recipients) >= 3:
                        coordinated.update(funded_recipients)
                        coord_funders.append((funder, len(funded_recipients)))

                # (debug removed)
                if not coordinated:
                    logger.info(
                        f"PDA distributor {src[:12]}: {len(connected)} connected, "
                        f"{traced_count} traced, no shared funders found"
                    )
                    continue

                new_devs = [w for w in coordinated if w not in dev_wallets]
                for w in new_devs:
                    dev_wallets.add(w)
                    wallet_origin[w].add(src)
                    dist_additions += 1
                coord_funders.sort(key=lambda x: -x[1])
                logger.info(
                    f"PDA distributor {src[:12]}: {len(connected)} connected, "
                    f"{traced_count} traced, {len(coordinated)} share funders, "
                    f"+{len(new_devs)} new devs. "
                    f"Top funders: {[(f[:12], n) for f, n in coord_funders[:5]]}"
                )

            if dist_additions:
                logger.info(
                    f"Distributor expansion: +{dist_additions} wallets"
                )

        # ── Consistent secondary collector expansion ──
        # Dev networks sometimes share a secondary collector across many wallets
        # even when their primary funders/collectors differ. Find secondary
        # collectors with 10+ buyer connections and add buyers that share them
        # AND have a qualifying primary collector (not funder — funders are too
        # broad as they may service many unrelated wallets).
        qualifying_collectors = {
            cw for cw, _, _, _, _, ct in qualifying
            if ct in ("collector", "mixed")
        }
        if trace_dict and qualifying_collectors:
            sec_coll_map = defaultdict(set)
            for w in buyer_set:
                _, down = trace_dict.get(w, ([], []))
                if down:
                    for ac in down[0].get("all_collectors", set()):
                        if ac and ac not in CEX_HOT_WALLETS and ac not in FEE_VAULTS:
                            sec_coll_map[ac].add(w)

            added_total = 0
            qualifying_sources = {cw for cw, _, _, _, _, _ in qualifying}
            for coll, coll_buyers in sec_coll_map.items():
                if len(coll_buyers) < 20 or coll in qualifying_sources:
                    continue
                # Require collector to connect to ≥3 existing dev wallets
                # (ensures it's truly "consistent" within the dev network)
                dev_overlap = sum(
                    1 for w in coll_buyers if w in dev_wallets
                )
                if dev_overlap < 3:
                    continue
                added_here = 0
                for buyer in coll_buyers:
                    if buyer in dev_wallets:
                        continue
                    # Require qualifying primary COLLECTOR connection
                    _, down = trace_dict.get(buyer, ([], []))
                    if not down:
                        continue
                    primary_coll = down[0].get("collector", "")
                    if primary_coll in qualifying_collectors:
                        dev_wallets.add(buyer)
                        wallet_origin[buyer].add(coll)
                        added_here += 1
                if added_here:
                    logger.info(
                        f"Consistent collector {coll[:12]}: "
                        f"{len(coll_buyers)} buyers, +{added_here} added"
                    )
                    added_total += added_here
            if added_total:
                logger.info(
                    f"Consistent collector expansion: +{added_total} total"
                )

        # ── CEX-confirmed expansion: add qualifying convergence wallets ──
        # that are buyers AND share CEX funder with existing dev_wallets.
        # A wallet that buys AND distributes tokens to 5+ buyers,
        # funded by the same CEX as known devs = strong signal.
        if cex_convergence and dev_wallets:
            for cex_w, cex_buyers in cex_convergence.items():
                cex_devs = dev_wallets & cex_buyers
                if len(cex_devs) < 3:
                    continue
                added = 0
                for conv_w, info, connected, ratio, window_secs, conv_type in qualifying:
                    if (conv_w in cex_buyers and conv_w in buyer_set
                            and conv_w not in dev_wallets
                            and len(connected) >= 5):
                        dev_wallets.add(conv_w)
                        wallet_origin[conv_w].add(cex_w)
                        added += 1
                if added:
                    logger.info(
                        f"CEX expansion {cex_w[:12]} ({CEX_HOT_WALLETS.get(cex_w, '?')}): "
                        f"+{added} convergence buyers"
                    )

        # ── Token-based expansion from confirmed devs ──
        # Expand through token distribution but ONLY from confirmed dev wallets.
        # This avoids the FP from market makers/pools that distribute widely.
        # Require dev-ratio >= 30% to filter out noisy distributors.
        TOKEN_DEV_RATIO = 0.30
        if dev_wallets and token_distributor_map and cex_convergence:
            all_cex_buyers = set()
            for cex_buyers_set in cex_convergence.values():
                all_cex_buyers |= cex_buyers_set

            for iteration in range(3):
                new_devs = set()

                # Direction 1: confirmed dev distributed tokens → add CEX-funded recipients
                for src in list(dev_wallets):
                    recips = token_distributor_map.get(src)
                    if not recips or len(recips) < 3:
                        continue
                    for r in recips:
                        if (r in buyer_set and r in all_cex_buyers
                                and r not in dev_wallets and r not in new_devs):
                            new_devs.add(r)
                            wallet_origin[r].add(src)

                # Direction 2: non-dev distributed to 3+ devs with high dev-ratio
                # → confirmed as dev distributor, add it + CEX-funded recipients
                for src, recips in token_distributor_map.items():
                    if src in dev_wallets or src in new_devs:
                        continue
                    if src not in buyer_set or src not in all_cex_buyers:
                        continue
                    recips_in_buyers = [r for r in recips if r in buyer_set]
                    dev_recips = (dev_wallets | new_devs) & set(recips)
                    dev_ratio = len(dev_recips) / max(len(recips_in_buyers), 1)
                    if len(dev_recips) >= 3 and dev_ratio >= TOKEN_DEV_RATIO:
                        new_devs.add(src)
                        wallet_origin[src].add("token_dist")
                        for r in recips_in_buyers:
                            if (r in all_cex_buyers
                                    and r not in dev_wallets and r not in new_devs):
                                new_devs.add(r)
                                wallet_origin[r].add(src)
                        logger.info(
                            f"Token distributor {src[:12]}: {len(dev_recips)}/{len(recips_in_buyers)} "
                            f"dev ratio={dev_ratio:.0%}, adding +{len(new_devs)} total"
                        )

                if not new_devs:
                    break
                logger.info(
                    f"Token expansion round {iteration + 1}: +{len(new_devs)} wallets"
                )
                dev_wallets |= new_devs

        # ── Broad collector token graph expansion ──
        # For broad (50+) collectors connected to dev wallets, build a token
        # distribution graph within their CEX-funded buyer set. Dense sub-clusters
        # (wallets with 2+ mutual token connections) are likely dev networks.
        # This captures Group 1-style patterns: wallets that distribute tokens
        # among themselves, funded by the same CEX, selling to the same collector.
        logger.info(
            f"Broad collector check: dev={len(dev_wallets)}, "
            f"cex_conv={len(cex_convergence)}, "
            f"token_dist={len(token_distributor_map)}, "
            f"qualifying={len(qualifying)}"
        )
        if dev_wallets and cex_convergence and token_distributor_map:
            all_cex_buyers = set()
            for cex_buyers_set in cex_convergence.values():
                all_cex_buyers |= cex_buyers_set

            broad_collectors = [
                (conv_w, connected)
                for conv_w, info, connected, ratio, ws, ct in qualifying
                if len(connected) >= 50 and ct != "cex_funder"
            ]
            logger.info(f"Broad collectors found: {len(broad_collectors)}")
            for bc, bc_connected in broad_collectors:
                bc_cex = {b for b in bc_connected
                          if b in buyer_set and b in all_cex_buyers}
                logger.info(
                    f"  {bc[:12]}: {len(bc_connected)} connected, "
                    f"{len(bc_cex)} in CEX set"
                )
                if len(bc_cex) < 10:
                    continue

                # Find star hubs: wallets that distribute tokens to 5-25
                # other wallets within the CEX-funded set. This range captures
                # dev distributors while excluding market makers (25+).
                token_out = defaultdict(set)
                for src, recips in token_distributor_map.items():
                    if src not in bc_cex:
                        continue
                    for r in recips:
                        if r in bc_cex and r != src:
                            token_out[src].add(r)

                logger.info(
                    f"  Token out: {len(token_out)} hubs, "
                    f"top: {[(h[:8], len(r)) for h, r in sorted(token_out.items(), key=lambda x: -len(x[1]))[:5]]}"
                )
                for hub, recipients in sorted(
                    token_out.items(), key=lambda x: -len(x[1])
                ):
                    if len(recipients) < 5 or len(recipients) > 25:
                        continue
                    hub_ts = buyers.get(hub, {}).get("ts", 0)
                    if not hub_ts:
                        continue
                    # Filter: keep recipients that ONLY received tokens via
                    # direct transfer (ts=0 = never bought on bonding curve).
                    # DEX buyers have ts > 0. Direct transfer recipients are
                    # dev sub-wallets that received from the hub.
                    direct_transfer = {r for r in recipients
                                       if buyers.get(r, {}).get("ts", 0) == 0}
                    # Also include recipients that bought at SAME time as hub
                    # (Jito bundle — hub buys and distributes in same tx)
                    same_time = {r for r in recipients
                                 if buyers.get(r, {}).get("ts", 0)
                                 and abs(buyers[r]["ts"] - hub_ts) <= 3}
                    bundled = direct_transfer | same_time
                    if len(bundled) < 3:
                        continue
                    # Only add the hub itself — recipients include DEX buyers
                    # that can't be distinguished from dev sub-wallets.
                    if hub not in dev_wallets:
                        dev_wallets.add(hub)
                        wallet_origin[hub].add(bc)
                        logger.info(
                            f"Star hub {hub[:12]}: {len(bundled)}/{len(recipients)} "
                            f"bundled in {bc[:12]}, added hub only"
                        )

        # ── Multi-hub expansion ──
        # Wallets that received tokens from 2+ confirmed dev hubs are likely
        # dev sub-wallets. A random DEX buyer would only buy from 1 hub.
        if dev_wallets and token_distributor_map:
            recipient_dev_hub_count = Counter()
            for src in dev_wallets:
                recips = token_distributor_map.get(src, set())
                for r in recips:
                    if r in buyer_set and r not in dev_wallets:
                        recipient_dev_hub_count[r] += 1
            multi_hub = {
                r for r, count in recipient_dev_hub_count.items()
                if count >= 2
            }
            if multi_hub:
                logger.info(
                    f"Multi-hub expansion: +{len(multi_hub)} wallets "
                    f"(received from 2+ dev hubs)"
                )
                for w in multi_hub:
                    dev_wallets.add(w)
                    wallet_origin[w].add("multi_hub")
        # ── Same-timestamp expansion ──
        # Dev wallets often buy in the same Jito bundle (same second).
        # If 3+ confirmed devs share a timestamp, other wallets in the
        # same broad-collector CEX-funded set at that timestamp are likely devs.
        if dev_wallets and cex_convergence:
            all_cex_buyers = set()
            for cex_buyers_set in cex_convergence.values():
                all_cex_buyers |= cex_buyers_set

            dev_ts_count = Counter()
            for w in dev_wallets:
                ts = buyers.get(w, {}).get("ts", 0)
                if ts:
                    dev_ts_count[ts] += 1
            hot_timestamps = {
                ts for ts, cnt in dev_ts_count.items()
                if cnt >= 3 and ts != 999999999  # skip DAS/GPA default
            }

            if hot_timestamps:
                # Find broad collectors with dev wallets
                broad_coll_wallets = set()
                for conv_w, info, connected, ratio, ws, ct in qualifying:
                    if len(connected) >= BROAD_THRESHOLD and ct != "cex_funder":
                        for b in connected:
                            if (b in buyer_set and b in all_cex_buyers
                                    and b not in dev_wallets):
                                b_ts = buyers.get(b, {}).get("ts", 0)
                                if b_ts in hot_timestamps:
                                    broad_coll_wallets.add(b)

                if broad_coll_wallets:
                    logger.info(
                        f"Same-timestamp expansion: +{len(broad_coll_wallets)} wallets "
                        f"at hot timestamps {hot_timestamps}"
                    )
                    for w in broad_coll_wallets:
                        dev_wallets.add(w)
                        wallet_origin[w].add("same_ts")


        # ── Step 6b: Reverse trace — find common funder of dev wallets ──
        # Dev wallets often withdraw from the same exchange. Find the common
        # funder, then trace all wallets it funded → check if they bought token.
        broad_collector_set = {
            conv_w for conv_w, info, connected, ratio, ws, ct in qualifying
            if len(connected) >= BROAD_THRESHOLD and ct != "cex_funder"
        }
        # Max timestamp from bonding curve buyers — anything after this
        # is a DEX buy, not a bonding curve buy (not a dev).
        bonding_timestamps = [
            info["ts"] for info in buyers.values() if info.get("ts", 0) > 0
        ]
        max_bonding_ts = max(bonding_timestamps) if bonding_timestamps else 0

        if dev_wallets and trace_dict:
            reverse_found, rt_funders = await self._reverse_trace_from_funders(
                dev_wallets, trace_dict, mint, sem,
                skip_collectors=broad_collector_set,
                max_buy_ts=max_bonding_ts,
                buyers=buyers,
            )
            if reverse_found:
                # Require convergence connection INDEPENDENT of the
                # reverse trace funders. If a wallet's only convergence
                # is to the same funder that funded it, that's circular.
                accepted = set()
                for w in reverse_found:
                    origins = wallet_origin.get(w, set())
                    independent = origins - rt_funders
                    if independent:
                        accepted.add(w)
                    elif len(origins) >= 2:
                        # 2+ convergence connections even if some are
                        # funders — strong enough signal
                        accepted.add(w)
                skipped = reverse_found - accepted
                if accepted:
                    logger.info(
                        f"Reverse trace: +{len(accepted)} with "
                        f"independent convergence"
                        f"{f', {len(skipped)} funder-only (skipped)' if skipped else ''}"
                    )
                for w in accepted:
                    dev_wallets.add(w)
                    wallet_origin[w].add("reverse_trace")
                new_counts = await self._quick_sig_check(list(reverse_found), sem)
                sig_counts.update(new_counts)

        # Post-filter: check sig counts for ALL dev wallets, then remove
        # high-activity wallets (>10k sigs = not a throwaway dev wallet).
        unchecked = [w for w in dev_wallets if w not in sig_counts]
        if unchecked:
            logger.info(f"Checking sig counts for {len(unchecked)} unchecked dev wallets...")
            new_counts = await self._batch_estimate_sig_counts(unchecked, sem)
            sig_counts.update(new_counts)

        if dev_wallets:
            before = len(dev_wallets)
            dev_wallets = {
                w for w in dev_wallets if sig_counts.get(w, 0) < 10000
            }
            removed = before - len(dev_wallets)
            if removed:
                logger.info(f"Sig count filter: removed {removed} wallets (>10k sigs)")

        if dev_wallets:
            wallet_origin = {
                w: o for w, o in wallet_origin.items() if w in dev_wallets
            }

        await progress(6, f"Clustering {len(dev_wallets)} dev wallets...")

        # ── Step 7: Cluster results ─────────────────────────
        clusters = self._cluster_expanded(
            dev_wallets, wallet_origin, convergence, buyers, result.total_supply
        )

        # ── Step 7b: Post-filter flagged 5k-10k wallets ──
        if flagged_5k:
            before_count = sum(len(c.wallets) for c in clusters)
            clusters = self._post_filter_flagged(
                clusters, trace_dict, flagged_5k, result.total_supply
            )
            after_count = sum(len(c.wallets) for c in clusters)
            if before_count != after_count:
                logger.info(
                    f"Behavioral filter: {before_count} → {after_count} wallets"
                )

        # ── If main pipeline found nothing, try behavioral fallback ──
        if not clusters:
            await progress(6, "No clusters from convergence, trying behavioral fallback...")
            result.traces, result.clusters = await self._fallback_behavioral(
                early_wallets, buyers, sig_counts, mint, result.total_supply, sem
            )
            if result.clusters:
                dev_count = sum(len(c.wallets) for c in result.clusters)
                dev_pct = sum(c.pct_supply for c in result.clusters)
                await progress(7, f"Done! {dev_count} dev wallets, {dev_pct:.1f}% supply")
            return

        # Build WalletTrace list for formatter compatibility
        traces = []
        for cluster in clusters:
            for wt in cluster.wallets:
                traces.append(wt)
        result.traces = traces
        result.clusters = clusters
        result.wallet_origins = {
            w: list(o) for w, o in wallet_origin.items()
        }

        dev_count = sum(len(c.wallets) for c in clusters)
        dev_pct = sum(c.pct_supply for c in clusters)
        await progress(7, f"Done! {dev_count} dev wallets, {dev_pct:.1f}% supply")

    # ─── Activity estimation ────────────────────────────────

    async def _batch_estimate_sig_counts(
        self, wallets: list[str], sem: asyncio.Semaphore,
    ) -> dict[str, int]:
        """Estimate txn counts for wallets. Returns {wallet: count}.
        Counts up to 10001 sigs then stops (enough to classify >10k)."""

        async def estimate_one(wallet: str) -> tuple[str, int]:
            before = None
            count = 0
            session = await self.helius._get_session()
            for _ in range(11):  # Max 11 pages = 11k sigs
                async with sem:
                    key = random.choice(self.helius._keys) if self.helius._keys else ""
                    url = f"https://mainnet.helius-rpc.com/?api-key={key}"
                    payload = {
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getSignaturesForAddress",
                        "params": [wallet, {
                            "limit": 1000,
                            **({"before": before} if before else {}),
                        }],
                    }
                    try:
                        async with session.post(url, json=payload) as resp:
                            if resp.status != 200:
                                break
                            data = await resp.json()
                            result = data.get("result")
                            if not result:
                                break
                            count += len(result)
                            if count >= 10001:
                                return wallet, count
                            if len(result) < 1000:
                                break
                            before = result[-1]["signature"]
                    except Exception:
                        break
            return wallet, count

        results = await asyncio.gather(
            *[estimate_one(w) for w in wallets],
            return_exceptions=True,
        )
        counts = {}
        for r in results:
            if isinstance(r, Exception):
                continue
            w, c = r
            counts[w] = c
        return counts

    async def _quick_sig_check(
        self, wallets: list[str], sem: asyncio.Semaphore,
    ) -> dict[str, int]:
        """1-page sig count check. Enough to detect fresh wallets (<100 sigs)."""

        async def check_one(wallet: str) -> tuple[str, int]:
            async with sem:
                session = await self.helius._get_session()
                key = random.choice(self.helius._keys) if self.helius._keys else ""
                url = f"https://mainnet.helius-rpc.com/?api-key={key}"
                payload = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [wallet, {"limit": 1000}],
                }
                try:
                    async with session.post(url, json=payload) as resp:
                        if resp.status != 200:
                            return wallet, 0
                        data = await resp.json()
                        result = data.get("result")
                        return wallet, len(result) if result else 0
                except Exception:
                    return wallet, 0

        results = await asyncio.gather(
            *[check_one(w) for w in wallets],
            return_exceptions=True,
        )
        counts = {}
        for r in results:
            if isinstance(r, Exception):
                continue
            w, c = r
            counts[w] = c
        return counts

    # ─── Signature fetching ──────────────────────────────────

    async def _get_oldest_signatures_helius(
        self, address: str, max_pages: int = 60,
        on_progress: Optional[Callable] = None,
    ) -> tuple[list, int]:
        """Paginate to the end via Helius RPC, keeping only the oldest sigs.

        Uses sliding window to keep memory low while paginating through
        millions of sigs. For mega-tokens (2M+ sigs) this takes ~10-15 min.

        Returns:
            (oldest_sigs, total_count)
        """
        before = None
        session = await self.helius._get_session()
        total = 0
        window_size = 30  # keep last 30 pages (~30k sigs)
        last_pages = []

        consecutive_errors = 0
        for page_num in range(max_pages):
            if page_num % 100 == 0 and page_num > 0:
                logger.info(f"Sig pagination: page {page_num}, {total:,} sigs so far...")
                if on_progress:
                    try:
                        await on_progress(2, f"Scanning page {page_num}: {total:,} sigs...")
                    except Exception:
                        pass
            result = None
            for attempt in range(3):  # retry up to 3 times per page
                key = random.choice(self.helius._keys) if self.helius._keys else ""
                url = f"https://mainnet.helius-rpc.com/?api-key={key}"
                payload = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [address, {
                        "limit": 1000,
                        **({"before": before} if before else {}),
                    }],
                }
                try:
                    async with session.post(url, json=payload) as resp:
                        if resp.status == 429:
                            await asyncio.sleep(0.5)
                            continue
                        if resp.status != 200:
                            await asyncio.sleep(0.3)
                            continue
                        data = await resp.json()
                        result = data.get("result")
                        break
                except Exception as e:
                    logger.warning(f"Helius RPC sig fetch error page {page_num}: {e}")
                    await asyncio.sleep(0.3)
                    continue

            if not result:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    break
                continue
            consecutive_errors = 0
            total += len(result)
            last_pages.append(result)
            if len(last_pages) > window_size:
                last_pages.pop(0)
            if len(result) < 1000:
                break
            before = result[-1]["signature"]

        oldest = [sig for page in last_pages for sig in page]
        logger.info(
            f"Helius RPC sigs for {address[:12]}: "
            f"{total} total, keeping {len(oldest)} oldest"
        )
        return oldest, total

    async def _get_all_signatures(self, mint: str, max_pages: int = 60) -> list:
        """Fetch signatures for a mint (paginated, newest-first).

        Uses direct Helius RPC with retry for reliability.
        Capped at max_pages (default 60 = 60k sigs).
        """
        all_sigs = []
        before = None
        session = await self.helius._get_session()
        consecutive_errors = 0

        for page_num in range(max_pages):
            if page_num % 10 == 0 and page_num > 0:
                logger.info(f"Sig fetch: page {page_num}, {len(all_sigs):,} sigs...")

            result = None
            for attempt in range(3):
                key = random.choice(self.helius._keys) if self.helius._keys else ""
                url = f"https://mainnet.helius-rpc.com/?api-key={key}"
                payload = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [mint, {
                        "limit": 1000,
                        **({"before": before} if before else {}),
                    }],
                }
                try:
                    async with session.post(url, json=payload) as resp:
                        if resp.status == 429:
                            await asyncio.sleep(0.5)
                            continue
                        if resp.status != 200:
                            await asyncio.sleep(0.3)
                            continue
                        data = await resp.json()
                        result = data.get("result")
                        break
                except Exception:
                    await asyncio.sleep(0.3)

            if not result:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    break
                continue
            consecutive_errors = 0
            all_sigs.extend(result)
            if len(result) < 1000:
                break
            before = result[-1]["signature"]
        return all_sigs

    # ─── Bonding curve phase sigs (mega-token optimization) ──

    async def _get_bonding_curve_phase_sigs(
        self, mint: str, sem: asyncio.Semaphore, max_pages: int = 30,
    ) -> tuple[list, str, str]:
        """Get sigs from the Pump.fun bonding curve phase for a mega-token.

        Strategy: derive the bonding curve PDA → get its sigs (usually <20)
        → use the oldest PDA sig as a cursor to jump directly to the bonding
        phase in the BC token account → paginate from there.

        This is ~100x faster than paginating millions of mint sigs because
        the cursor jump skips straight to the bonding curve phase.

        Returns (list of sig objects, newest_pda_sig, bc_token_account).
        """
        session = await self.helius._get_session()

        # 1. Derive bonding curve PDA
        try:
            bc_pda = derive_bonding_curve_pda(mint)
        except Exception:
            logger.info("BC cursor jump: can't derive PDA, skipping")
            return [], "", ""

        # 2. Get PDA sigs (usually <20 — these mark the bonding phase)
        # Retry up to 3 times with different keys (RPC can return empty)
        pda_sigs = []
        for attempt in range(3):
            key = random.choice(self.helius._keys) if self.helius._keys else ""
            url = f"https://mainnet.helius-rpc.com/?api-key={key}"
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getSignaturesForAddress",
                "params": [bc_pda, {"limit": 20}],
            }
            try:
                async with session.post(url, json=payload) as resp:
                    data = await resp.json()
                    if data.get("error"):
                        logger.info(
                            f"BC cursor jump: PDA sig fetch error "
                            f"(attempt {attempt+1}): {data['error']}"
                        )
                        await asyncio.sleep(0.3)
                        continue
                    pda_sigs = data.get("result", [])
                    if pda_sigs:
                        break
            except Exception as e:
                logger.info(f"BC cursor jump: PDA sig fetch failed (attempt {attempt+1}): {e}")
                await asyncio.sleep(0.3)

        if not pda_sigs:
            logger.info("BC cursor jump: PDA has 0 sigs after 3 attempts")
            return [], "", ""

        oldest_pda_sig = pda_sigs[-1]["signature"]
        oldest_pda_slot = pda_sigs[-1].get("slot", 0)
        logger.info(
            f"BC cursor jump: PDA has {len(pda_sigs)} sigs, "
            f"oldest at slot {oldest_pda_slot}"
        )

        # 3. Detect BC token account from newest sigs (already parsed)
        # or find it from PDA-related transactions
        bc_token = await self._find_bc_token_account(mint, pda_sigs, sem)
        if not bc_token:
            logger.info("BC cursor jump: can't find BC token account")
            return [], "", ""

        # 4. Use oldest PDA sig as cursor → get BC token account sigs
        #    from the bonding phase (going backwards from the PDA timestamp)
        all_sigs = []
        before = oldest_pda_sig
        consecutive_errors = 0

        for page in range(max_pages):
            key = random.choice(self.helius._keys) if self.helius._keys else ""
            url = f"https://mainnet.helius-rpc.com/?api-key={key}"
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getSignaturesForAddress",
                "params": [bc_token, {
                    "limit": 1000,
                    "before": before,
                }],
            }
            result = None
            for attempt in range(3):
                try:
                    async with session.post(url, json=payload) as resp:
                        if resp.status == 429:
                            await asyncio.sleep(0.5)
                            continue
                        if resp.status != 200:
                            await asyncio.sleep(0.3)
                            continue
                        data = await resp.json()
                        result = data.get("result")
                        break
                except Exception:
                    await asyncio.sleep(0.3)

            if not result:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    break
                continue
            consecutive_errors = 0
            all_sigs.extend(result)
            if len(result) < 1000:
                break
            before = result[-1]["signature"]

        logger.info(
            f"BC cursor jump: {len(all_sigs)} bonding phase sigs "
            f"from {bc_token[:12]}"
        )

        # 5. Also fetch MINT sigs from the bonding phase era.
        # BC token account sigs only capture BC buys/sells.
        # Token transfers between wallets during the bonding phase
        # (e.g., distributor → sub-wallets) are only in MINT sigs.
        # Use the newest PDA sig as cursor to jump to that era.
        newest_pda_sig = pda_sigs[0]["signature"]
        mint_phase_sigs = []
        before_mint = newest_pda_sig
        consecutive_errors = 0

        for page in range(max_pages):
            key = random.choice(self.helius._keys) if self.helius._keys else ""
            url = f"https://mainnet.helius-rpc.com/?api-key={key}"
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getSignaturesForAddress",
                "params": [mint, {
                    "limit": 1000,
                    "before": before_mint,
                }],
            }
            result = None
            for attempt in range(3):
                try:
                    async with session.post(url, json=payload) as resp:
                        if resp.status == 429:
                            await asyncio.sleep(0.5)
                            continue
                        if resp.status != 200:
                            await asyncio.sleep(0.3)
                            continue
                        data = await resp.json()
                        result = data.get("result")
                        break
                except Exception:
                    await asyncio.sleep(0.3)

            if not result:
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    break
                continue
            consecutive_errors = 0
            mint_phase_sigs.extend(result)
            if len(result) < 1000:
                break
            before_mint = result[-1]["signature"]

        if mint_phase_sigs:
            logger.info(
                f"BC cursor jump: {len(mint_phase_sigs)} mint phase sigs "
                f"(token transfers in bonding era)"
            )
            # Merge BC + mint phase sigs
            seen = {s["signature"] for s in all_sigs}
            for s in mint_phase_sigs:
                if s["signature"] not in seen:
                    all_sigs.append(s)

        return all_sigs, newest_pda_sig, bc_token

    async def _bonding_era_token_search(
        self,
        wallets: list[str],
        mint: str,
        cursor_sig: str,
        sem: asyncio.Semaphore,
        max_pages: int = 85,
    ) -> tuple[set[str], dict[str, set[str]], set[str]]:
        """Search distributors' token accounts for token distribution recipients.

        Instead of searching the wallet's general sigs (potentially 50k+),
        finds the wallet's Associated Token Account (ATA) for the target mint
        and paginates its sigs. The ATA only has mint-specific transactions,
        making the search much more efficient.

        Uses 40 pages (40k sigs) to cover high-activity distributors.
        Also searches from bonding-era cursor to fill gaps.

        Returns (all_recipients, per_source_map) where per_source_map is
        {source_wallet: set of recipient wallets}.
        """
        session = await self.helius._get_session()
        all_recipients = set()
        per_source_map: dict[str, set[str]] = {}
        skipped_protocol_pdas: set[str] = set()

        async def search_one(wallet: str) -> tuple[set[str], bool]:
            recipients = set()

            # 0. Skip protocol PDAs (Raydium AMM, etc.) — they have 1000s of
            # token accounts. Their ATA sigs use ALTs so getSignaturesForAddress
            # won't index the actual swap txns. Scanning them is wasted effort.
            if not is_on_curve(wallet):
                async with sem:
                    key = random.choice(self.helius._keys) if self.helius._keys else ""
                    url = f"https://mainnet.helius-rpc.com/?api-key={key}"
                    payload = {
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getTokenAccountsByOwner",
                        "params": [wallet,
                                   {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                                   {"encoding": "jsonParsed"}],
                    }
                    try:
                        async with session.post(url, json=payload) as resp:
                            data = await resp.json()
                            all_accounts = data.get("result", {}).get("value", [])
                            if len(all_accounts) > 1000:
                                logger.info(
                                    f"Bonding-era search {wallet[:12]}: "
                                    f"SKIPPED — protocol PDA with {len(all_accounts)} token accounts"
                                )
                                return recipients, True
                    except Exception:
                        pass

            # 1. Find the wallet's ATA for this mint
            async with sem:
                key = random.choice(self.helius._keys) if self.helius._keys else ""
                url = f"https://mainnet.helius-rpc.com/?api-key={key}"
                payload = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [wallet, {"mint": mint},
                               {"encoding": "jsonParsed"}],
                }
                try:
                    async with session.post(url, json=payload) as resp:
                        data = await resp.json()
                        accounts = data.get("result", {}).get("value", [])
                except Exception as e:
                    logger.info(f"Bonding-era search {wallet[:12]}: ATA lookup failed: {e}")
                    return recipients, False

            if not accounts:
                # Token account may be closed (0 balance).
                # Derive ATA deterministically as fallback.
                try:
                    ata = derive_ata(wallet, mint)
                    logger.info(
                        f"Bonding-era search {wallet[:12]}: "
                        f"account closed, using derived ATA {ata[:12]}"
                    )
                except Exception:
                    logger.info(f"Bonding-era search {wallet[:12]}: no token account found")
                    return recipients, False
            else:
                ata = accounts[0]["pubkey"]

            # 2. Paginate ATA sigs — up to 85k sigs for high-activity ATAs.
            # GpMZbSM2GgvT-style distributors have 80k+ ATA sigs. Dev transfers
            # are in the older half, so we need deep pagination.
            ata_sigs = []
            before = None
            for page in range(max_pages):
                params = {"limit": 1000}
                if before:
                    params["before"] = before
                result = None
                for attempt in range(3):
                    async with sem:
                        key = random.choice(self.helius._keys) if self.helius._keys else ""
                        url = f"https://mainnet.helius-rpc.com/?api-key={key}"
                        payload = {
                            "jsonrpc": "2.0", "id": 1,
                            "method": "getSignaturesForAddress",
                            "params": [ata, params],
                        }
                        try:
                            async with session.post(url, json=payload) as resp:
                                if resp.status != 200:
                                    await asyncio.sleep(0.3)
                                    continue
                                data = await resp.json()
                                result = data.get("result")
                                if result:
                                    break
                        except Exception:
                            pass
                    if not result and attempt < 2:
                        await asyncio.sleep(0.5)
                if not result:
                    break
                ata_sigs.extend(result)
                if len(result) < 1000:
                    break
                before = result[-1]["signature"]

            if not ata_sigs:
                logger.info(f"Bonding-era search {wallet[:12]}: ATA {ata[:12]} has 0 sigs")
                return recipients, False

            logger.info(
                f"Bonding-era search {wallet[:12]}: ATA {ata[:12]}, "
                f"{len(ata_sigs)} sigs, parsing {len(ata_sigs)//100+1} batches..."
            )

            # 3. Parse sigs for token transfers (parallel batches)
            sig_list = [s["signature"] for s in ata_sigs]
            batches = [sig_list[i:i + 100]
                       for i in range(0, len(sig_list), 100)]

            async def parse_batch(batch):
                async with sem:
                    key = random.choice(self.helius._keys) if self.helius._keys else ""
                    parse_url = f"https://api.helius.xyz/v0/transactions?api-key={key}"
                    try:
                        async with session.post(
                            parse_url, json={"transactions": batch}
                        ) as resp:
                            if resp.status != 200:
                                return []
                            return await resp.json()
                    except Exception:
                        return []

            batch_results = await asyncio.gather(
                *[parse_batch(b) for b in batches],
                return_exceptions=True,
            )

            failed = sum(1 for r in batch_results
                         if isinstance(r, Exception) or not r)
            if failed:
                logger.info(
                    f"Bonding-era search {wallet[:12]}: "
                    f"{failed}/{len(batches)} batch parses failed"
                )

            sent_to = set()    # wallets this distributor SENT tokens to
            recv_from = set()  # wallets that SENT tokens to this distributor

            for txns in batch_results:
                if isinstance(txns, Exception) or not txns:
                    continue
                for tx in txns:
                    for tt in tx.get("tokenTransfers", []):
                        if tt.get("mint") != mint:
                            continue
                        amt = tt.get("tokenAmount", 0)
                        if amt <= 0:
                            continue
                        frm = tt.get("fromUserAccount", "")
                        to_w = tt.get("toUserAccount", "")
                        if frm == wallet and to_w and to_w != wallet:
                            sent_to.add(to_w)
                        if to_w == wallet and frm and frm != wallet:
                            recv_from.add(frm)

            recipients = sent_to | recv_from
            if recipients:
                logger.info(
                    f"Bonding-era search {wallet[:12]}: "
                    f"ATA {ata[:12]}, {len(ata_sigs)} sigs, "
                    f"sent_to={len(sent_to)}, recv_from={len(recv_from)}"
                )
            return sent_to, False  # only return wallets that received tokens

        results = await asyncio.gather(
            *[search_one(w) for w in wallets],
            return_exceptions=True,
        )
        exceptions = 0
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                exceptions += 1
                logger.info(
                    f"Bonding-era search {wallets[i][:12]}: exception: {r}"
                )
            elif isinstance(r, tuple) and len(r) == 2:
                recips, is_protocol = r
                if is_protocol:
                    skipped_protocol_pdas.add(wallets[i])
                elif isinstance(recips, set):
                    all_recipients |= recips
                    if recips:
                        per_source_map[wallets[i]] = recips
        if exceptions:
            logger.info(f"Bonding-era search: {exceptions}/{len(wallets)} searches failed")

        return all_recipients, per_source_map, skipped_protocol_pdas

    async def _discover_distributors_via_ata(
        self,
        buyers: dict,
        mint: str,
        bonding_curve: str,
        sig_counts: dict,
        sem: asyncio.Semaphore,
        sample_size: int = 80,
    ) -> dict[str, set[str]]:
        """Discover token distributors by checking buyers' ATAs.

        Step 3's token tracking uses newest 100 txns — for old tokens,
        the distributor interaction is buried. This method checks each
        buyer's ATA (token-specific account) to find who sent them tokens.
        If multiple buyers received from the same non-BC wallet, it's a
        distributor.

        Prioritizes low-activity wallets (likely dev wallets with few txns).

        Returns {distributor_wallet: set of recipient wallets}.
        """
        session = await self.helius._get_session()

        # Sample earliest buyers, prioritize low activity
        early = sorted(buyers.items(), key=lambda x: x[1]["ts"])
        # Sort by activity: lowest sig count first (fresh wallets)
        candidates = [(w, sig_counts.get(w, 0)) for w, _ in early[:500]]
        candidates.sort(key=lambda x: x[1])
        sample = [w for w, _ in candidates[:sample_size]]

        distributor_map = defaultdict(set)

        async def check_one(wallet: str) -> tuple[str, list[str]]:
            # Get ATA
            async with sem:
                key = random.choice(self.helius._keys) if self.helius._keys else ""
                url = f"https://mainnet.helius-rpc.com/?api-key={key}"
                payload = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [wallet, {"mint": mint},
                               {"encoding": "jsonParsed"}],
                }
                try:
                    async with session.post(url, json=payload) as resp:
                        data = await resp.json()
                        accounts = data.get("result", {}).get("value", [])
                except Exception:
                    return wallet, []

            if not accounts:
                try:
                    ata = derive_ata(wallet, mint)
                except Exception:
                    return wallet, []
            else:
                ata = accounts[0]["pubkey"]

            # Get ATA sigs (20 newest — enough for fresh wallets)
            async with sem:
                key = random.choice(self.helius._keys) if self.helius._keys else ""
                url = f"https://mainnet.helius-rpc.com/?api-key={key}"
                payload = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [ata, {"limit": 20}],
                }
                try:
                    async with session.post(url, json=payload) as resp:
                        data = await resp.json()
                        sigs = data.get("result", [])
                except Exception:
                    return wallet, []

            if not sigs:
                return wallet, []

            # Parse sigs to find token sources
            sig_list = [s["signature"] for s in sigs]
            async with sem:
                key = random.choice(self.helius._keys) if self.helius._keys else ""
                parse_url = f"https://api.helius.xyz/v0/transactions?api-key={key}"
                try:
                    async with session.post(
                        parse_url, json={"transactions": sig_list}
                    ) as resp:
                        if resp.status != 200:
                            return wallet, []
                        txns = await resp.json()
                except Exception:
                    return wallet, []

            sources = []
            for tx in txns:
                for tt in tx.get("tokenTransfers", []):
                    if tt.get("mint") != mint:
                        continue
                    frm = tt.get("fromUserAccount", "")
                    to_w = tt.get("toUserAccount", "")
                    amt = tt.get("tokenAmount", 0)
                    if amt > 0 and to_w == wallet and frm and frm != wallet:
                        sources.append(frm)
            return wallet, sources

        results = await asyncio.gather(
            *[check_one(w) for w in sample],
            return_exceptions=True,
        )

        for r in results:
            if isinstance(r, Exception):
                continue
            wallet, sources = r
            for src in sources:
                if src != bonding_curve:
                    distributor_map[src].add(wallet)

        # Filter: keep sources with 3+ buyer recipients
        found = {
            src: recips
            for src, recips in distributor_map.items()
            if len(recips) >= 3
        }
        if found:
            for src, recips in found.items():
                logger.info(
                    f"ATA distributor discovery: {src[:12]} "
                    f"sent tokens to {len(recips)} buyers"
                )
        return found

    async def _find_bc_token_account(
        self, mint: str, pda_sigs: list, sem: asyncio.Semaphore,
    ) -> str:
        """Find the bonding curve token account address.

        In Pump.fun, the bonding curve token account is the token's
        authority/creator (available from DAS metadata). Falls back to
        parsing the earliest mint sigs for the most common token sender.
        Note: Pump.fun BC token accounts are NOT standard ATAs — they're
        created by the program directly, so derive_ata() won't work.
        """
        session = await self.helius._get_session()

        # Method 1: DAS metadata — authority is the BC token account
        # Retry up to 3 times with different API keys for reliability
        for das_attempt in range(3):
            key = random.choice(self.helius._keys) if self.helius._keys else ""
            url = f"https://mainnet.helius-rpc.com/?api-key={key}"
            try:
                payload = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getAsset",
                    "params": {"id": mint},
                }
                async with session.post(url, json=payload) as resp:
                    data = await resp.json()
                    result = data.get("result", {})
                    if not result or data.get("error"):
                        if das_attempt < 2:
                            await asyncio.sleep(0.3)
                            continue
                    authorities = result.get("authorities", [])
                    for auth in authorities:
                        addr = auth.get("address", "")
                        # BC token account is a PDA (off-curve), don't filter
                        if addr and addr != mint:
                            logger.info(f"BC token account (from DAS): {addr[:16]}...")
                            return addr
                    creators = result.get("creators", [])
                    for cr in creators:
                        addr = cr.get("address", "")
                        if addr and addr != mint:
                            logger.info(f"BC token account (from creator): {addr[:16]}...")
                            return addr
                    break  # Got result but no useful data
            except Exception as e:
                if das_attempt < 2:
                    await asyncio.sleep(0.3)
                else:
                    logger.debug(f"DAS lookup failed: {e}")

        # Method 2: Parse earliest mint sigs near bonding phase
        # Use the PDA's NEWEST sig to get mint sigs DURING the bonding phase
        for sig_obj in pda_sigs:
            sig = sig_obj["signature"]
            key = random.choice(self.helius._keys) if self.helius._keys else ""
            url = f"https://mainnet.helius-rpc.com/?api-key={key}"
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getSignaturesForAddress",
                "params": [mint, {"limit": 50, "before": sig}],
            }
            try:
                async with session.post(url, json=payload) as resp:
                    data = await resp.json()
                    mint_sigs = data.get("result", [])
            except Exception:
                continue
            if not mint_sigs:
                continue

            sig_list = [s["signature"] for s in mint_sigs[:30]]
            key = random.choice(self.helius._keys) if self.helius._keys else ""
            url2 = f"https://api.helius.xyz/v0/transactions/?api-key={key}"
            try:
                async with session.post(url2, json={"transactions": sig_list}) as resp:
                    txns = await resp.json()
            except Exception:
                continue

            sender_counts = Counter()
            for tx in txns:
                if not isinstance(tx, dict):
                    continue
                for tt in tx.get("tokenTransfers", []):
                    if tt.get("mint") == mint and tt.get("tokenAmount", 0) > 0:
                        fr = tt.get("fromUserAccount", "")
                        if fr:
                            sender_counts[fr] += 1
            if sender_counts:
                # Validate: skip protocol PDAs (Raydium AMM, etc.)
                # by checking if the candidate has too many token accounts
                for bc_token, _cnt in sender_counts.most_common(5):
                    if is_on_curve(bc_token):
                        # On-curve addresses can be BC token accounts
                        logger.info(f"BC token account (from sigs): {bc_token[:16]}...")
                        return bc_token
                    # Off-curve: verify it's not a protocol PDA
                    try:
                        key2 = random.choice(self.helius._keys) if self.helius._keys else ""
                        url3 = f"https://mainnet.helius-rpc.com/?api-key={key2}"
                        payload3 = {
                            "jsonrpc": "2.0", "id": 1,
                            "method": "getTokenAccountsByOwner",
                            "params": [bc_token,
                                       {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                                       {"encoding": "jsonParsed"}],
                        }
                        async with session.post(url3, json=payload3) as resp3:
                            data3 = await resp3.json()
                            accts = data3.get("result", {}).get("value", [])
                            if len(accts) > 100:
                                logger.info(
                                    f"BC token account skip {bc_token[:12]}: "
                                    f"protocol PDA ({len(accts)} token accounts)"
                                )
                                continue
                        logger.info(f"BC token account (from sigs): {bc_token[:16]}...")
                        return bc_token
                    except Exception:
                        logger.info(f"BC token account (from sigs): {bc_token[:16]}...")
                        return bc_token

        return ""

    # ─── Fast path: small token ──────────────────────────────

    async def _get_buyers_fast(self, mint: str) -> tuple[dict, str]:
        """Get buyers using get_token_transactions (for small tokens < 1000 txns)."""
        txns = await self.helius.get_token_transactions(
            address=mint, source_filter="", max_pages=20,
        )
        if not txns:
            return {}, ""

        logger.info(f"Dev trace: fetched {len(txns)} total txns (fast path)")
        sorted_txns = sorted(txns, key=lambda t: t.get("timestamp", 0))
        bonding_curve = self._detect_bonding_curve(sorted_txns, mint)
        buyers = self._parse_buyers(sorted_txns, bonding_curve, mint)
        return buyers, bonding_curve

    # ─── Deep path: large token ──────────────────────────────

    async def _get_buyers_deep(
        self, mint: str, all_sigs: list, sem: asyncio.Semaphore,
        parse_count: int = 0,
    ) -> tuple[dict, str]:
        """Get earliest buyers from pre-fetched signatures.

        Args:
            parse_count: How many sigs to parse. 0 = use MAX_OLDEST_TXNS global.
        """
        if not all_sigs:
            return {}, ""

        # Sort oldest first
        all_sigs.sort(key=lambda s: s.get("slot", 0))

        # Parse oldest N txns (0 = parse all)
        limit = parse_count or MAX_OLDEST_TXNS
        subset = all_sigs if limit == 0 else all_sigs[:limit]
        oldest_sigs = [s["signature"] for s in subset]
        logger.info(f"Deep parse: {len(oldest_sigs)} sigs in {len(oldest_sigs)//100+1} batches...")

        # Parse concurrently (with semaphore to avoid rate limits)
        batches = [oldest_sigs[i:i + 100] for i in range(0, len(oldest_sigs), 100)]

        async def _parse_one(batch, idx):
            async with sem:
                result = await self._parse_transactions_batch(batch)
                if idx % 50 == 0 and idx > 0:
                    logger.info(f"Deep parse progress: {idx}/{len(batches)} batches...")
                return result

        batch_results = await asyncio.gather(
            *[_parse_one(b, i) for i, b in enumerate(batches)],
            return_exceptions=True,
        )
        all_parsed = []
        for r in batch_results:
            if isinstance(r, list):
                all_parsed.extend(r)

        all_parsed.sort(key=lambda t: t.get("timestamp", 0))
        bonding_curve = self._detect_bonding_curve(all_parsed, mint)
        buyers = self._parse_buyers(all_parsed, bonding_curve, mint)
        return buyers, bonding_curve

    # ─── Buyer parsing (works for both paths) ────────────────

    def _detect_bonding_curve(self, txns: list, mint: str) -> str:
        sc = Counter()
        for tx in txns[:200]:
            for tt in tx.get("tokenTransfers", []):
                if tt.get("mint") == mint:
                    fa = tt.get("fromUserAccount", "")
                    if fa:
                        sc[fa] += 1
        if not sc:
            from pump_analyzer import derive_bonding_curve_pda
            return derive_bonding_curve_pda(mint)
        return sc.most_common(1)[0][0]

    def _parse_buyers(self, txns: list, bonding_curve: str, mint: str) -> dict:
        """Parse buyers from enhanced txns. Handles bonding curve + DEX swaps.

        Supports bundled txns (Jito bundles) where multiple wallets receive
        tokens in the same transaction — each recipient is a separate buyer.
        """
        buyers = {}
        seen = set()
        for tx in txns:
            sig = tx.get("signature", "")
            if sig in seen:
                continue
            seen.add(sig)
            ts = tx.get("timestamp", 0)
            fp = tx.get("feePayer", "")

            # Collect ALL token recipients (handles bundled buys)
            recipients = {}  # wallet -> token_amount
            for tt in tx.get("tokenTransfers", []):
                if tt.get("mint") != mint:
                    continue
                ta = tt.get("tokenAmount", 0)
                if ta <= 0:
                    continue
                to_user = tt.get("toUserAccount", "")
                if to_user and is_on_curve(to_user):
                    recipients[to_user] = recipients.get(to_user, 0) + ta

            sol = 0.0
            for nt in tx.get("nativeTransfers", []):
                if nt.get("toUserAccount") == bonding_curve:
                    sol += nt.get("amount", 0) / 1e9

            if recipients:
                # Split SOL evenly among recipients (approximation for bundles)
                sol_each = sol / len(recipients) if sol > 0 else 0
                for wallet, tok in recipients.items():
                    if wallet not in buyers:
                        buyers[wallet] = {"tokens": tok, "sol": sol_each, "ts": ts}
                    else:
                        buyers[wallet]["tokens"] += tok
                        buyers[wallet]["sol"] += sol_each
                        buyers[wallet]["ts"] = min(buyers[wallet]["ts"], ts)
            elif fp and sol > 0:
                # Fallback: no token recipient found, use fee payer
                if fp not in buyers:
                    buyers[fp] = {"tokens": 0, "sol": sol, "ts": ts}
                else:
                    buyers[fp]["sol"] += sol
                    buyers[fp]["ts"] = min(buyers[fp]["ts"], ts)

        return buyers

    async def _get_token_holders_das(self, mint: str, max_pages: int = 5) -> list[str]:
        """Get current token holders via DAS getTokenAccounts.
        Returns list of wallet addresses. Capped to avoid huge tokens."""
        session = await self.helius._get_session()
        holders = []

        for page in range(1, max_pages + 1):
            key = random.choice(self.helius._keys) if self.helius._keys else ""
            url = f"https://mainnet.helius-rpc.com/?api-key={key}"
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getTokenAccounts",
                "params": {"mint": mint, "page": page, "limit": 1000},
            }
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        break
                    data = await resp.json()
                    accounts = data.get("result", {}).get("token_accounts", [])
                    if not accounts:
                        break
                    for acc in accounts:
                        owner = acc.get("owner", "")
                        if owner:
                            holders.append(owner)
            except Exception:
                break

        logger.info(f"DAS token holders: {len(holders)} accounts for {mint[:12]}")
        return holders

    async def _get_all_token_owners_gpa(self, mint: str) -> set[str]:
        """Get ALL token account owners via getProgramAccounts (includes 0-balance).
        This catches wallets invisible to getSignaturesForAddress (ALT-indexed DEX
        swaps) and DAS (only returns balance > 0). Returns owner wallet addresses."""
        session = await self.helius._get_session()
        owners = set()

        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getProgramAccounts",
            "params": [
                "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                {
                    "filters": [
                        {"dataSize": 165},
                        {"memcmp": {"offset": 0, "bytes": mint}},
                    ],
                    "encoding": "jsonParsed",
                },
            ],
        }
        for attempt in range(2):
            try:
                key = random.choice(self.helius._keys) if self.helius._keys else ""
                url = f"https://mainnet.helius-rpc.com/?api-key={key}"
                timeout = aiohttp.ClientTimeout(total=45)
                async with session.post(url, json=payload, timeout=timeout) as resp:
                    if resp.status != 200:
                        logger.info(f"GPA attempt {attempt}: status {resp.status}")
                        continue
                    data = await resp.json()
                    if "error" in data:
                        logger.info(f"GPA attempt {attempt}: error {data['error']}")
                        continue
                    accounts = data.get("result", [])
                    if len(accounts) > 10000:
                        logger.info(f"GPA: {len(accounts)} accounts, too many, skipping")
                        return owners
                    for acc in accounts:
                        info = (acc.get("account", {}).get("data", {})
                                .get("parsed", {}).get("info", {}))
                        owner = info.get("owner", "")
                        if owner:
                            owners.add(owner)
                    break  # success
            except asyncio.TimeoutError:
                logger.info(f"GPA attempt {attempt}: timed out (45s)")
            except Exception as e:
                logger.info(f"GPA attempt {attempt}: {type(e).__name__}: {e}")

        logger.info(f"GPA token owners: {len(owners)} for {mint[:12]}")
        return owners

    # ─── Multi-hop tracing ───────────────────────────────────

    async def _trace_both_directions(
        self,
        wallets: list[str],
        sem: asyncio.Semaphore,
        mint: str = "",
        sig_counts: dict = None,
    ) -> list[tuple]:
        """Trace HOP_DEPTH hops upstream + downstream for each wallet.
        Also tracks token recipients for `mint` (if provided).
        If sig_counts provided, wallets with >100 sigs also fetch oldest
        txns to find original funders outside the 100-tx window."""

        async def trace_one(idx, wallet):
            # Upstream: funder chain
            upstream = []
            token_recipients = set()  # wallets involved in token transfers
            token_sources = {}  # {source_wallet: set of recipient wallets}
            current = wallet
            visited = set()
            for hop in range(HOP_DEPTH):
                if current in visited:
                    break
                visited.add(current)
                async with sem:
                    htxns = await self._fetch_wallet_txns(
                        current, idx * HOP_DEPTH + hop
                    )
                if not htxns:
                    break
                all_funders_hop = set()
                best_from, best_amt = "", 0
                for tx in htxns:
                    for nt in tx.get("nativeTransfers", []):
                        ta = nt.get("toUserAccount", "")
                        fa = nt.get("fromUserAccount", "")
                        a = nt.get("amount", 0) / 1e9
                        if (ta == current and fa != current and a > 0.01):
                            all_funders_hop.add(fa)
                            if a > best_amt:
                                best_amt, best_from = a, fa
                    # Track token transfers for this mint
                    if mint and hop == 0:
                        for tt in tx.get("tokenTransfers", []):
                            if tt.get("mint") != mint:
                                continue
                            from_w = tt.get("fromUserAccount", "")
                            to_w = tt.get("toUserAccount", "")
                            amt = tt.get("tokenAmount", 0)
                            if amt > 0:
                                if from_w == current and to_w and to_w != current:
                                    token_recipients.add(to_w)
                                    token_sources.setdefault(current, set()).add(to_w)
                                if to_w == current and from_w and from_w != current:
                                    token_recipients.add(from_w)
                                    token_sources.setdefault(from_w, set()).add(current)
                # For hop 0: also fetch oldest txns if wallet has >100 sigs
                # to find original funders outside the newest 100-tx window
                if (hop == 0 and sig_counts
                        and 100 < sig_counts.get(wallet, 0) < 200):
                    pre_funders = len(all_funders_hop)
                    async with sem:
                        oldest_txns = await self._fetch_oldest_wallet_txns(
                            wallet, sig_counts.get(wallet, 0)
                        )
                    for tx in oldest_txns:
                        for nt in tx.get("nativeTransfers", []):
                            ta = nt.get("toUserAccount", "")
                            fa = nt.get("fromUserAccount", "")
                            a = nt.get("amount", 0) / 1e9
                            if ta == current and fa != current and a > 0.01:
                                all_funders_hop.add(fa)
                                if a > best_amt:
                                    best_amt, best_from = a, fa
                    new_funders = len(all_funders_hop) - pre_funders
                    if new_funders > 0:
                        logger.debug(
                            f"Oldest-txn fetch {wallet[:12]}: "
                            f"+{new_funders} funders from {len(oldest_txns)} oldest txns "
                            f"(sigs={sig_counts.get(wallet, 0)}, "
                            f"best_from={best_from[:12] if best_from else 'none'})"
                        )

                upstream.append({
                    "wallet": current, "funder": best_from, "amt": best_amt,
                    "all_funders": all_funders_hop,
                })
                if not best_from:
                    break
                current = best_from

            # Downstream: collector chain
            downstream = []
            current = wallet
            visited = set()
            for hop in range(HOP_DEPTH):
                if current in visited:
                    break
                visited.add(current)
                async with sem:
                    htxns = await self._fetch_wallet_txns(
                        current, idx * HOP_DEPTH + hop + 500
                    )
                if not htxns:
                    break
                all_collectors_hop = set()
                best_to, best_amt = "", 0
                for tx in htxns:
                    for nt in tx.get("nativeTransfers", []):
                        fa = nt.get("fromUserAccount", "")
                        ta = nt.get("toUserAccount", "")
                        a = nt.get("amount", 0) / 1e9
                        if (fa == current and ta != current
                                and a > 0.01 and is_on_curve(ta)):
                            all_collectors_hop.add(ta)
                            if a > best_amt:
                                best_amt, best_to = a, ta
                downstream.append({
                    "wallet": current, "collector": best_to, "amt": best_amt,
                    "all_collectors": all_collectors_hop,
                })
                if not best_to:
                    break
                current = best_to

            return wallet, upstream, downstream, token_recipients, token_sources

        tasks = [trace_one(i, w) for i, w in enumerate(wallets)]
        raw = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in raw if not isinstance(r, Exception)]

    # ─── Convergence detection ───────────────────────────────

    def _find_convergence(
        self,
        traced: list[tuple],
        buyer_set: set[str],
    ) -> tuple[dict, dict, dict]:
        """Find wallets that appear in multiple buyer chains.

        Only uses HOP 1 (direct funder/collector) to avoid false
        convergence from deep-hop coincidences.
        """
        up_map = defaultdict(set)
        down_map = defaultdict(set)
        # Phase 1: primary collectors only
        secondary_down = defaultdict(set)

        for item in traced:
            buyer_w, up, down = item[0], item[1], item[2]
            # Only use hop 1: direct funder of the buyer
            if up:
                node = up[0]
                f = node.get("funder", "")
                if f and f != buyer_w:
                    up_map[f].add(buyer_w)
                for af in node.get("all_funders", set()):
                    if af and af != buyer_w:
                        up_map[af].add(buyer_w)
            # Only use hop 1: direct collector from the buyer
            if down:
                node = down[0]
                c = node.get("collector", "")
                if c and c != buyer_w:
                    down_map[c].add(buyer_w)
                # Store secondary collectors separately for phase 2
                for ac in node.get("all_collectors", set()):
                    if ac and ac != buyer_w and ac != c:
                        secondary_down[ac].add(buyer_w)

        # Phase 1: Merge and find initial convergence with primary only
        merged = defaultdict(set)
        for w, connected in up_map.items():
            merged[w] |= connected
        for w, connected in down_map.items():
            merged[w] |= connected

        convergence = {}
        cex_convergence = {}  # track CEX convergence separately for filtering
        for w, connected in merged.items():
            if w in FEE_VAULTS:
                continue  # fee vaults are pure noise
            if w in CEX_HOT_WALLETS:
                if len(connected) >= 15:
                    convergence[w] = connected
                    cex_convergence[w] = connected
                continue
            if len(connected) >= 3:
                convergence[w] = connected

        # Phase 2: Add secondary collector links for already-qualifying sources.
        # Only add secondary connections for collectors that qualified in phase 1
        # via primary connections. This prevents noisy secondary collectors from
        # creating false convergence.
        for coll in list(convergence.keys()):
            if coll in secondary_down and coll not in CEX_HOT_WALLETS and coll not in FEE_VAULTS:
                extra = secondary_down[coll] - convergence.get(coll, set())
                if extra:
                    convergence[coll] |= extra
                    down_map[coll] |= extra

        return convergence, up_map, down_map, cex_convergence

    # ─── Convergence verification ────────────────────────────

    async def _verify_convergence(
        self,
        conv_sorted: list,
        convergence: dict,
        sem: asyncio.Semaphore,
    ) -> dict:
        """Verify convergence wallets, skip mega-hubs (>300 interactions)."""

        async def verify_one(idx, wallet):
            async with sem:
                htxns = await self._fetch_wallet_txns(wallet, idx + 700)
            if not htxns:
                return wallet, 0, set(), set()
            sent_to = set()
            recv_from = set()
            for tx in htxns:
                for nt in tx.get("nativeTransfers", []):
                    fa = nt.get("fromUserAccount", "")
                    ta = nt.get("toUserAccount", "")
                    a = nt.get("amount", 0) / 1e9
                    if a < 0.01:
                        continue
                    if fa == wallet and ta != wallet and is_on_curve(ta):
                        sent_to.add(ta)
                    if ta == wallet and fa != wallet and is_on_curve(fa):
                        recv_from.add(fa)
            return wallet, len(htxns), sent_to, recv_from

        vtasks = [verify_one(i, w) for i, (w, _) in enumerate(conv_sorted)]
        vraw = await asyncio.gather(*vtasks, return_exceptions=True)

        good = {}
        for r in vraw:
            if isinstance(r, Exception):
                continue
            w, txn_count, sent_to, recv_from = r
            total = len(sent_to) + len(recv_from)
            if total < 2:
                continue
            # Allow high-contact wallets if they connect to many buyers
            # (dev funders can have 300+ contacts but still be signal)
            buyers_connected = len(convergence.get(w, set()))
            if total > 300 and buyers_connected < 10:
                logger.debug(
                    f"Skip mega-hub {w[:12]}: {total} contacts, "
                    f"only {buyers_connected} buyer connections"
                )
                continue
            good[w] = {
                "buyers": convergence[w],
                "txns": txn_count,
                "sent_to": sent_to,
                "recv_from": recv_from,
            }
        return good

    # ─── Reverse trace from convergence ──────────────────────

    async def _reverse_trace_from_funders(
        self,
        dev_wallets: set,
        trace_dict: dict,
        mint: str,
        sem: asyncio.Semaphore,
        skip_collectors: set = None,
        max_buy_ts: int = 0,
        buyers: dict = None,
    ) -> tuple[set, set]:
        """Find common funders AND collectors of existing dev wallets,
        then trace all connected wallets. Check if they bought the token.

        Two directions:
        1. Funders: trace wallets funded by the same source (CEX/splitter)
        2. Collectors: trace wallets that sent profits to the same collector
        """
        # ── Find common funders ──
        funder_counts = Counter()
        collector_counts = Counter()
        for w in dev_wallets:
            up, down = trace_dict.get(w, ([], []))
            if up:
                f = up[0].get("funder", "")
                if f:
                    funder_counts[f] += 1
                for af in up[0].get("all_funders", set()):
                    if af:
                        funder_counts[af] += 1
            if down:
                c = down[0].get("collector", "")
                if c:
                    collector_counts[c] += 1

        top_funders = [(f, cnt) for f, cnt in funder_counts.most_common(5)
                       if cnt >= 3 and f not in CEX_HOT_WALLETS and f not in FEE_VAULTS]
        # Skip high-connection collectors (50+) — likely DEX routes or
        # aggregators, not personal collectors. Tracing these creates
        # massive false positives.
        _skip = skip_collectors or set()
        top_collectors = [(c, cnt) for c, cnt in collector_counts.most_common(5)
                          if cnt >= 3 and c not in CEX_HOT_WALLETS and c not in FEE_VAULTS
                          and cnt < 50 and c not in _skip]

        top_funder_set = {f for f, _ in top_funders}
        if not top_funders and not top_collectors:
            return set(), top_funder_set

        if top_funders:
            logger.info(f"Reverse trace funders: {[(f[:12], c) for f, c in top_funders]}")
        if top_collectors:
            logger.info(f"Reverse trace collectors: {[(c[:12], n) for c, n in top_collectors]}")

        candidates = set()

        # ── From funders: find all wallets they sent SOL to ──
        for funder_w, cnt in top_funders:
            async with sem:
                ftxns = await self._fetch_wallet_txns_paginated(
                    funder_w, max_pages=5, key_start=0
                )
            for tx in ftxns:
                for nt in tx.get("nativeTransfers", []):
                    fa = nt.get("fromUserAccount", "")
                    ta = nt.get("toUserAccount", "")
                    a = nt.get("amount", 0) / 1e9
                    if (a >= 0.01 and fa == funder_w
                            and ta != funder_w and is_on_curve(ta)
                            and ta not in dev_wallets):
                        candidates.add(ta)

        # ── From collectors: find all wallets that sent SOL to them ──
        for collector_w, cnt in top_collectors:
            async with sem:
                ctxns = await self._fetch_wallet_txns_paginated(
                    collector_w, max_pages=5, key_start=0
                )
            for tx in ctxns:
                for nt in tx.get("nativeTransfers", []):
                    fa = nt.get("fromUserAccount", "")
                    ta = nt.get("toUserAccount", "")
                    a = nt.get("amount", 0) / 1e9
                    if (a >= 0.01 and ta == collector_w
                            and fa != collector_w and is_on_curve(fa)
                            and fa not in dev_wallets):
                        candidates.add(fa)

        # ── From trace_dict: find buyers funded by top funders ──
        # Catches wallets missed by funder txn pagination (funder has
        # deep txn history, oldest funding txns outside newest 500).
        td_added = 0
        for w in trace_dict:
            if w in dev_wallets or w in candidates:
                continue
            up, down = trace_dict.get(w, ([], []))
            if up:
                w_funders = up[0].get("all_funders", set())
                if w_funders & top_funder_set:
                    candidates.add(w)
                    td_added += 1
        if td_added:
            logger.info(
                f"Reverse trace: +{td_added} candidates from trace_dict "
                f"(funded by top funders)"
            )

        if not candidates:
            return set(), top_funder_set

        logger.info(f"Reverse trace: {len(candidates)} candidate wallets")

        # Quick sig check — only keep fresh wallets (<200 sigs)
        fresh_counts = await self._quick_sig_check(list(candidates), sem)
        fresh_candidates = [
            w for w in candidates if 0 < fresh_counts.get(w, 0) < 200
        ]

        if not fresh_candidates:
            logger.info("Reverse trace: no fresh candidates found")
            return set(), top_funder_set

        logger.info(f"Reverse trace: {len(fresh_candidates)} fresh candidates to verify")

        # Check if candidate wallets bought the target token.
        # Primary: use buyers dict (instant, reliable — no RPC needed).
        # Fallback: RPC check for candidates not in buyers dict.
        additional = set()
        remaining = []
        if buyers:
            for w in fresh_candidates:
                if w in buyers:
                    additional.add(w)
                else:
                    remaining.append(w)
            if additional:
                logger.info(
                    f"Reverse trace: {len(additional)} candidates "
                    f"confirmed via buyers dict"
                )
        else:
            remaining = list(fresh_candidates)

        # RPC fallback for candidates not in buyers dict
        if remaining:
            async def check_token_held(wallet):
                async with sem:
                    key = random.choice(self.helius._keys) if self.helius._keys else ""
                    url = f"https://mainnet.helius-rpc.com/?api-key={key}"
                    payload = {
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getTokenAccountsByOwner",
                        "params": [wallet, {"mint": mint},
                                   {"encoding": "jsonParsed"}],
                    }
                    try:
                        async with self.helius._session.post(
                            url, json=payload
                        ) as resp:
                            if resp.status != 200:
                                return None
                            data = await resp.json()
                            accounts = data.get("result", {}).get(
                                "value", []
                            )
                            if accounts:
                                return wallet
                    except Exception:
                        pass
                return None

            results = await asyncio.gather(
                *[check_token_held(w) for w in remaining],
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, str):
                    additional.add(r)

        return additional, top_funder_set

    # ─── Convergence expansion ───────────────────────────────

    async def _expand_convergence(
        self,
        good_conv: dict,
        sem: asyncio.Semaphore,
    ) -> tuple[set, dict]:
        """Expand from convergence wallets to find full dev wallet network."""
        expanded = set()
        wallet_origin = defaultdict(set)  # wallet -> set of convergence IDs

        async def _expand_one(conv_w: str, info: dict):
            """Expand a single convergence wallet (runs in parallel)."""
            local_expanded = set()
            local_origin = defaultdict(set)
            sent_to = info["sent_to"]
            recv_from = info["recv_from"]

            # SPLITTER: paginate txns, get all destinations
            # Cap at 100 destinations per convergence wallet to avoid hub flooding
            if len(sent_to) >= 3:
                async with sem:
                    all_txns = await self._fetch_wallet_txns_paginated(
                        conv_w, max_pages=5, key_start=0
                    )
                dests = []
                for tx in all_txns:
                    for nt in tx.get("nativeTransfers", []):
                        fa = nt.get("fromUserAccount", "")
                        ta = nt.get("toUserAccount", "")
                        a = nt.get("amount", 0) / 1e9
                        if (a >= 0.01 and fa == conv_w
                                and ta != conv_w and is_on_curve(ta)):
                            dests.append(ta)
                if len(dests) > 100:
                    # Too many destinations — likely a hub, skip splitter expansion
                    pass
                else:
                    for ta in dests:
                        local_expanded.add(ta)
                        local_origin[ta].add(conv_w)

            # COLLECTOR: get sources (intermediaries), then trace 1 more hop
            if len(recv_from) >= 3:
                async with sem:
                    all_txns = await self._fetch_wallet_txns_paginated(
                        conv_w, max_pages=5, key_start=100
                    )
                intermediaries = set()
                for tx in all_txns:
                    for nt in tx.get("nativeTransfers", []):
                        ta = nt.get("toUserAccount", "")
                        fa = nt.get("fromUserAccount", "")
                        a = nt.get("amount", 0) / 1e9
                        if (a >= 0.01 and ta == conv_w
                                and fa != conv_w and is_on_curve(fa)):
                            intermediaries.add(fa)

                if len(intermediaries) > 100:
                    # Too many sources — likely a hub, skip collector expansion
                    return local_expanded, local_origin

                for inter in intermediaries:
                    local_expanded.add(inter)
                    local_origin[inter].add(conv_w)

                # 2nd hop: trace each intermediary (cap at 50)
                inter_list = list(intermediaries)[:50]

                async def trace_inter(inter_w):
                    async with sem:
                        itxns = await self._fetch_wallet_txns(
                            inter_w, random.randint(900, 9999)
                        )
                    if not itxns:
                        return set()
                    sources = set()
                    for tx in itxns:
                        for nt in tx.get("nativeTransfers", []):
                            ta = nt.get("toUserAccount", "")
                            fa = nt.get("fromUserAccount", "")
                            a = nt.get("amount", 0) / 1e9
                            if (ta == inter_w and fa != inter_w
                                    and a > 0.01 and is_on_curve(fa)):
                                sources.add(fa)
                    return sources

                iraw = await asyncio.gather(
                    *[trace_inter(w) for w in inter_list],
                    return_exceptions=True,
                )
                for r in iraw:
                    if isinstance(r, Exception):
                        continue
                    for src in r:
                        local_expanded.add(src)
                        local_origin[src].add(conv_w)

            return local_expanded, local_origin

        # Run all convergence wallets in parallel
        sorted_conv = sorted(
            good_conv.items(), key=lambda x: -len(x[1]["buyers"])
        )
        results = await asyncio.gather(
            *[_expand_one(w, info) for w, info in sorted_conv],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                continue
            local_exp, local_orig = r
            expanded |= local_exp
            for w, origins in local_orig.items():
                wallet_origin[w] |= origins

        return expanded, wallet_origin

    # ─── Collector expansion ─────────────────────────────────

    async def _expand_from_collectors(
        self,
        dev_wallets: set,
        buyer_set: set,
        sem: asyncio.Semaphore,
    ) -> tuple[set, dict]:
        """Find where dev wallets send profits, then find more dev wallets.

        Flow:
        1. For each dev wallet, find all SOL destinations
        2. Find common collectors (3+ dev wallets send to same wallet)
        3. For each collector, get ALL sources
        4. Filter by buyer_set → new dev wallets
        """
        if not dev_wallets:
            return set(), {}

        # Step 1: Get destinations of all dev wallets (parallel)
        collector_map = defaultdict(set)  # dest → set of dev wallets that send to it

        async def get_dests(wallet):
            async with sem:
                txns = await self._fetch_wallet_txns(
                    wallet, random.randint(2000, 9999)
                )
            dests = set()
            if txns:
                for tx in txns:
                    for nt in tx.get("nativeTransfers", []):
                        fa = nt.get("fromUserAccount", "")
                        ta = nt.get("toUserAccount", "")
                        a = nt.get("amount", 0) / 1e9
                        if (fa == wallet and ta != wallet
                                and a > 0.01 and is_on_curve(ta)):
                            dests.add(ta)
            return wallet, dests

        results = await asyncio.gather(
            *[get_dests(w) for w in list(dev_wallets)[:100]],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                continue
            wallet, dests = r
            for d in dests:
                if d not in buyer_set and d not in CEX_HOT_WALLETS and d not in FEE_VAULTS:
                    collector_map[d].add(wallet)

        # Step 2: Strong collectors = 3+ dev wallets send to them
        strong = sorted(
            [(c, devs) for c, devs in collector_map.items() if len(devs) >= 3],
            key=lambda x: -len(x[1]),
        )[:10]

        if not strong:
            return set(), {}

        # Step 3: For each collector, find ALL wallets that sent to it
        new_dev = set()
        new_origin = defaultdict(set)

        async def expand_coll(collector):
            async with sem:
                txns = await self._fetch_wallet_txns_paginated(
                    collector, max_pages=5,
                    key_start=random.randint(0, 500),
                )
            sources = set()
            if txns:
                for tx in txns:
                    for nt in tx.get("nativeTransfers", []):
                        ta = nt.get("toUserAccount", "")
                        fa = nt.get("fromUserAccount", "")
                        a = nt.get("amount", 0) / 1e9
                        if (ta == collector and fa != collector
                                and a > 0.01 and is_on_curve(fa)):
                            sources.add(fa)
            return collector, sources

        coll_results = await asyncio.gather(
            *[expand_coll(c) for c, _ in strong],
            return_exceptions=True,
        )
        for r in coll_results:
            if isinstance(r, Exception):
                continue
            collector, sources = r
            for src in sources:
                if src in buyer_set:
                    new_dev.add(src)
                    new_origin[src].add(collector)

        return new_dev, new_origin

    # ─── Clustering ──────────────────────────────────────────

    def _cluster_expanded(
        self,
        dev_wallets: set,
        wallet_origin: dict,
        convergence: dict,
        buyers: dict,
        total_supply: float,
    ) -> List[DevCluster]:
        """Cluster dev wallets by shared convergence origin."""
        if not dev_wallets:
            return []

        parent = {w: w for w in dev_wallets}

        # Group by convergence origin
        conv_groups = defaultdict(list)
        for w in dev_wallets:
            for origin in wallet_origin.get(w, set()):
                conv_groups[origin].append(w)

        for origin, wallets in conv_groups.items():
            if len(wallets) >= 2:
                for w in wallets[1:]:
                    _union(parent, wallets[0], w)

        # Also cluster wallets from same convergence buyer group
        for conv_w, connected_buyers in convergence.items():
            in_dev = [b for b in connected_buyers if b in dev_wallets]
            if len(in_dev) >= 2:
                for w in in_dev[1:]:
                    _union(parent, in_dev[0], w)

        # Build cluster map
        cluster_map = defaultdict(set)
        for w in dev_wallets:
            root = _find(parent, w)
            cluster_map[root].add(w)

        # Sort by size, filter 3+
        sorted_clusters = sorted(cluster_map.values(), key=lambda c: -len(c))
        labels = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

        clusters = []
        for idx, members in enumerate(sorted_clusters):
            if len(members) < 3:
                continue

            label = labels[idx] if idx < 26 else f"Z{idx - 25}"

            # Find most common convergence wallet for this cluster
            conv_counts = Counter()
            for w in members:
                for origin in wallet_origin.get(w, set()):
                    conv_counts[origin] += 1

            shared_funder = ""
            shared_collector = ""
            if conv_counts:
                top_conv = conv_counts.most_common(1)[0][0]
                shared_collector = top_conv  # convergence wallet = collector/funder

            # Build WalletTrace list for formatter compatibility
            wallet_traces = []
            total_tokens = 0.0
            for w in members:
                info = buyers.get(w, {})
                tokens = info.get("tokens", 0)
                total_tokens += tokens
                wallet_traces.append(WalletTrace(
                    wallet=w,
                    token_amount=tokens,
                    sol_spent=info.get("sol", 0),
                    funder=shared_funder,
                    funder_amount=0.0,
                    collector=shared_collector,
                    collector_amount=0.0,
                    funder_is_cex="",
                ))

            # Sort by token amount descending
            wallet_traces.sort(key=lambda t: -t.token_amount)

            pct = min((total_tokens / total_supply * 100) if total_supply > 0 else 0, 100.0)

            # Score: bigger cluster + more convergence = higher confidence
            score = 0
            if len(members) >= 10:
                score += 3
            elif len(members) >= 5:
                score += 2
            if shared_collector and shared_collector not in CEX_HOT_WALLETS and shared_collector not in FEE_VAULTS:
                score += 3
            if pct >= 1.0:
                score += 2

            cluster_type = "dev" if score >= 5 else (
                "suspicious" if score >= 3 else "hunter"
            )

            clusters.append(DevCluster(
                cluster_id=label,
                cluster_type=cluster_type,
                wallets=wallet_traces,
                shared_funder=shared_funder,
                shared_collector=shared_collector,
                total_tokens=total_tokens,
                pct_supply=pct,
                confidence=score,
            ))

        # Sort: dev first, then by supply
        type_order = {"dev": 0, "suspicious": 1, "hunter": 2, "unknown": 3}
        clusters.sort(key=lambda c: (type_order.get(c.cluster_type, 9), -c.pct_supply))

        return clusters

    def _post_filter_flagged(
        self,
        clusters: List[DevCluster],
        trace_dict: dict,
        flagged: set,
        total_supply: float,
    ) -> List[DevCluster]:
        """Remove flagged (5k-10k txn) wallets that don't match cluster behavior.

        For each cluster, collect direct funders/collectors of clean members.
        Flagged wallets must share at least one direct funder or collector
        with clean members to stay in the cluster.
        """
        filtered = []
        for cluster in clusters:
            has_flagged = any(wt.wallet in flagged for wt in cluster.wallets)
            if not has_flagged:
                filtered.append(cluster)
                continue

            # Collect direct funders/collectors of clean (non-flagged) members
            clean_members = [wt for wt in cluster.wallets if wt.wallet not in flagged]

            # If ALL wallets are flagged, skip this filter — convergence
            # already validated them via shared funder + timing.
            if not clean_members:
                filtered.append(cluster)
                continue

            clean_funders = set()
            clean_collectors = set()
            for wt in clean_members:
                up, down = trace_dict.get(wt.wallet, ([], []))
                if up:
                    f = up[0].get("funder", "")
                    if f:
                        clean_funders.add(f)
                    clean_funders |= up[0].get("all_funders", set())
                if down:
                    c = down[0].get("collector", "")
                    if c:
                        clean_collectors.add(c)
                    # Only primary collector — all_collectors includes minor
                    # SOL transfers that create false matches

            # Keep flagged wallets only if they share funder or collector
            kept = list(clean_members)
            for wt in cluster.wallets:
                if wt.wallet not in flagged:
                    continue
                up, down = trace_dict.get(wt.wallet, ([], []))
                match = False
                if up:
                    wf = {up[0].get("funder", "")} | up[0].get("all_funders", set())
                    wf.discard("")
                    if wf & clean_funders:
                        match = True
                if not match and down:
                    wc = {down[0].get("collector", "")}
                    wc.discard("")
                    if wc & clean_collectors:
                        match = True
                if match:
                    kept.append(wt)
                else:
                    logger.info(
                        f"Behavioral filter removed: {wt.wallet[:12]}..."
                    )

            if len(kept) >= 3:
                cluster.wallets = kept
                total_tokens = sum(wt.token_amount for wt in kept)
                cluster.total_tokens = total_tokens
                if total_supply > 0:
                    cluster.pct_supply = min(
                        total_tokens / total_supply * 100, 100.0
                    )
                filtered.append(cluster)

        return filtered

    # ─── Fallback: behavioral clustering ─────────────────────

    async def _fallback_behavioral(
        self,
        wallets: list[str],
        buyers: dict,
        sig_counts: dict,
        mint: str,
        total_supply: float,
        sem: asyncio.Semaphore,
    ) -> tuple[List[WalletTrace], List[DevCluster]]:
        """Behavioral fallback: cluster non-bot wallets by buy timing.

        When convergence fails (no shared funder/collector at hop 1),
        dev wallets can still be identified by:
        1. Non-bot wallets (sig count < 1000, not heavy traders/bots)
        2. Bought within a tight time window
        3. Multiple such wallets = coordinated buying = likely dev
        """
        # Find non-bot wallets among traced wallets
        # Dev wallets aren't necessarily "fresh" — they might have 200-900 sigs
        # from trading other tokens. The key is they're NOT bots (>3k sigs).
        FRESH_THRESHOLD = 1000
        fresh = []
        no_sig_data = 0
        for w in wallets:
            count = sig_counts.get(w, -1)
            if count == -1:
                no_sig_data += 1
                continue
            if 0 < count < FRESH_THRESHOLD and w in buyers:
                fresh.append((w, buyers[w]))

        # Log sig count distribution for debugging
        counts_with_data = [sig_counts.get(w, 0) for w in wallets if sig_counts.get(w, -1) >= 0]
        if counts_with_data:
            under50 = sum(1 for c in counts_with_data if 0 < c < 50)
            under200 = sum(1 for c in counts_with_data if 0 < c < 200)
            under1000 = sum(1 for c in counts_with_data if 0 < c < 1000)
            logger.info(
                f"Behavioral fallback: sig distribution of {len(counts_with_data)} wallets: "
                f"<50={under50}, <200={under200}, <1k={under1000}"
            )

        logger.info(
            f"Behavioral fallback: {len(fresh)} non-bot wallets (<{FRESH_THRESHOLD} sigs) "
            f"out of {len(wallets)} traced, {no_sig_data} missing sig data"
        )

        if len(fresh) < 3:
            return [], []

        # Sort by buy timestamp
        fresh.sort(key=lambda x: x[1]["ts"])
        logger.info(f"Behavioral fallback: {len(fresh)} non-bot wallets to analyze")

        # Sliding window clustering: group wallets buying within 5 min
        TIME_WINDOW = 300  # 5 minutes
        best_cluster = []
        i = 0
        for j in range(len(fresh)):
            while fresh[j][1]["ts"] - fresh[i][1]["ts"] > TIME_WINDOW:
                i += 1
            window = fresh[i:j + 1]
            if len(window) > len(best_cluster):
                best_cluster = list(window)

        if len(best_cluster) < 3:
            # Try larger window (15 min) for devs that buy gradually
            TIME_WINDOW_LARGE = 900
            i = 0
            for j in range(len(fresh)):
                while fresh[j][1]["ts"] - fresh[i][1]["ts"] > TIME_WINDOW_LARGE:
                    i += 1
                window = fresh[i:j + 1]
                if len(window) > len(best_cluster):
                    best_cluster = list(window)

        if len(best_cluster) < 3:
            # Last resort: 1 hour window
            TIME_WINDOW_XL = 3600
            i = 0
            for j in range(len(fresh)):
                while fresh[j][1]["ts"] - fresh[i][1]["ts"] > TIME_WINDOW_XL:
                    i += 1
                window = fresh[i:j + 1]
                if len(window) > len(best_cluster):
                    best_cluster = list(window)

        if len(best_cluster) < 3:
            logger.info(f"Behavioral fallback: largest timing cluster = {len(best_cluster)}, need ≥3")
            return [], []

        logger.info(
            f"Behavioral fallback: found cluster of {len(best_cluster)} fresh wallets "
            f"within {best_cluster[-1][1]['ts'] - best_cluster[0][1]['ts']:.0f}s"
        )

        # Build traces for the cluster — get funder/collector for each
        dev_wallets_set = {w for w, _ in best_cluster}

        # Also expand: check if there are MORE fresh wallets outside the window
        # that share the same collector/funder as cluster members
        traces = []
        for w, info in best_cluster:
            traces.append(WalletTrace(
                wallet=w,
                token_amount=info.get("tokens", 0),
                sol_spent=info.get("sol", 0),
                funder="", funder_amount=0,
                collector="", collector_amount=0,
                funder_is_cex="",
            ))

        # Try reverse trace from this behavioral cluster
        # to find even more dev wallets
        trace_dict = {}
        traced_results = await self._trace_both_directions(
            [w for w, _ in best_cluster[:50]], sem  # trace up to 50
        )
        for item in traced_results:
            trace_dict[item[0]] = (item[1], item[2])

        # Check for common collectors/funders within the behavioral cluster
        reverse_found = await self._reverse_trace_from_funders(
            dev_wallets_set, trace_dict, mint, sem
        )
        if reverse_found:
            logger.info(f"Behavioral + reverse trace: {len(reverse_found)} more wallets")
            for w in reverse_found:
                if w in buyers:
                    info = buyers[w]
                else:
                    info = {"tokens": 0, "sol": 0, "ts": 0}
                traces.append(WalletTrace(
                    wallet=w, token_amount=info.get("tokens", 0),
                    sol_spent=info.get("sol", 0),
                    funder="", funder_amount=0,
                    collector="", collector_amount=0,
                    funder_is_cex="",
                ))

        total_tokens = sum(t.token_amount for t in traces)
        pct = min((total_tokens / total_supply * 100) if total_supply > 0 else 0, 100.0)

        cluster = DevCluster(
            cluster_id="A",
            cluster_type="dev",
            wallets=traces,
            shared_funder="",
            shared_collector="",
            total_tokens=total_tokens,
            pct_supply=pct,
            confidence=6,  # behavioral match
        )
        return traces, [cluster]

    # ─── Fallback: simple 1-hop trace ────────────────────────

    async def _fallback_simple_trace(
        self,
        wallets: list[str],
        buyers: dict,
        total_supply: float,
        sem: asyncio.Semaphore,
    ) -> tuple[List[WalletTrace], List[DevCluster]]:
        """Fallback when no convergence found: simple 1-hop funder/collector."""
        num_keys = len(self.helius._keys)

        async def trace_one(idx, wallet):
            info = buyers.get(wallet, {})
            funder, funder_amt, funder_cex = "", 0.0, ""
            collector, collector_amt = "", 0.0

            async with sem:
                key = self.helius._keys[idx % num_keys]
                funding = await self.helius.get_wallet_incoming_sol(
                    wallet=wallet, key=key,
                    before_timestamp=info.get("ts", 0), lookback_hours=72,
                )
                if funding:
                    funder = funding["from_wallet"]
                    funder_amt = funding["amount_sol"]
                    funder_cex = CEX_HOT_WALLETS.get(funder, "")

                key2 = self.helius._keys[(idx + num_keys // 2) % num_keys]
                profit = await self.helius.get_wallet_outgoing_sol(
                    wallet=wallet, key=key2,
                    after_timestamp=info.get("ts", 0),
                )
                if profit:
                    collector = profit["to_wallet"]
                    collector_amt = profit["amount_sol"]

            return WalletTrace(
                wallet=wallet,
                token_amount=info.get("tokens", 0),
                sol_spent=info.get("sol", 0),
                funder=funder, funder_amount=funder_amt,
                collector=collector, collector_amount=collector_amt,
                funder_is_cex=funder_cex,
            )

        limited = wallets[:200]
        tasks = [trace_one(i, w) for i, w in enumerate(limited)]
        raw = await asyncio.gather(*tasks, return_exceptions=True)
        traces = [r for r in raw if not isinstance(r, Exception)]

        # Simple clustering by shared funder/collector
        clusters = self._cluster_by_connections_simple(traces, total_supply)
        return traces, clusters

    def _cluster_by_connections_simple(
        self,
        traces: List[WalletTrace],
        total_supply: float,
    ) -> List[DevCluster]:
        """Simple Union-Find clustering by shared funder/collector."""
        if not traces:
            return []

        parent = {t.wallet: t.wallet for t in traces}

        funder_groups = defaultdict(list)
        for t in traces:
            if t.funder:
                funder_groups[t.funder].append(t.wallet)
        for wallets in funder_groups.values():
            if len(wallets) >= 2:
                for w in wallets[1:]:
                    _union(parent, wallets[0], w)

        collector_groups = defaultdict(list)
        for t in traces:
            if t.collector:
                collector_groups[t.collector].append(t.wallet)
        for wallets in collector_groups.values():
            if len(wallets) >= 2:
                for w in wallets[1:]:
                    _union(parent, wallets[0], w)

        # Cross-link
        all_funders = {t.funder for t in traces if t.funder}
        all_collectors = {t.collector for t in traces if t.collector}
        for addr in all_funders & all_collectors:
            fw = funder_groups.get(addr, [])
            cw = collector_groups.get(addr, [])
            if fw and cw:
                for w in cw:
                    _union(parent, fw[0], w)

        cluster_members = defaultdict(list)
        trace_map = {t.wallet: t for t in traces}
        for t in traces:
            root = _find(parent, t.wallet)
            cluster_members[root].append(t)

        labels = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        clusters = []
        for idx, (root, members) in enumerate(
            sorted(cluster_members.items(),
                   key=lambda x: sum(m.token_amount for m in x[1]),
                   reverse=True)
        ):
            if len(members) < 2:
                continue
            label = labels[idx] if idx < 26 else f"Z{idx - 25}"

            fc = Counter(m.funder for m in members if m.funder)
            shared_funder = fc.most_common(1)[0][0] if fc else ""
            cc = Counter(m.collector for m in members if m.collector)
            shared_collector = cc.most_common(1)[0][0] if cc else ""

            total_tokens = sum(m.token_amount for m in members)
            pct = min((total_tokens / total_supply * 100) if total_supply > 0 else 0, 100.0)

            score = 0
            if shared_funder and shared_funder not in CEX_HOT_WALLETS and shared_funder not in FEE_VAULTS:
                score += 3
            if shared_collector and shared_collector not in CEX_HOT_WALLETS and shared_collector not in FEE_VAULTS:
                score += 2
            if len(members) >= 5:
                score += 2

            cluster_type = "dev" if score >= 5 else (
                "suspicious" if score >= 3 else "hunter"
            )

            clusters.append(DevCluster(
                cluster_id=label,
                cluster_type=cluster_type,
                wallets=members,
                shared_funder=shared_funder,
                shared_collector=shared_collector,
                total_tokens=total_tokens,
                pct_supply=pct,
                confidence=score,
            ))

        type_order = {"dev": 0, "suspicious": 1, "hunter": 2, "unknown": 3}
        clusters.sort(key=lambda c: (type_order.get(c.cluster_type, 9), -c.pct_supply))
        return clusters

    # ─── API helpers ─────────────────────────────────────────

    async def _fetch_wallet_txns(self, wallet: str, key_offset: int = 0) -> list:
        """Fetch enhanced txns for a wallet (single page, 100 txns).
        Retries up to 3 times with different random keys on 429."""
        for attempt in range(3):
            key = random.choice(self.helius._keys)
            await asyncio.sleep(random.uniform(0.02, 0.15))
            url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
            params = {"api-key": key, "limit": 100}
            try:
                session = await self.helius._get_session()
                async with session.get(url, params=params) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(random.uniform(0.3, 1.0))
                        continue
                    if resp.status != 200:
                        return []
                    return await resp.json()
            except Exception:
                return []
        return []

    async def _fetch_wallet_txns_paginated(
        self, wallet: str, max_pages: int = 10, key_start: int = 0
    ) -> list:
        """Fetch paginated enhanced txns for a wallet.
        Retries each page up to 2 times with different keys on 429."""
        all_txns = []
        before_sig = None
        for pg in range(max_pages):
            page_data = None
            for attempt in range(2):
                key = random.choice(self.helius._keys)
                await asyncio.sleep(random.uniform(0.02, 0.15))
                url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
                params = {"api-key": key, "limit": 100}
                if before_sig:
                    params["before"] = before_sig
                try:
                    session = await self.helius._get_session()
                    async with session.get(url, params=params) as resp:
                        if resp.status == 429:
                            await asyncio.sleep(random.uniform(0.3, 1.0))
                            continue
                        if resp.status != 200:
                            break
                        page_data = await resp.json()
                        break
                except Exception:
                    break
            if not page_data:
                break
            all_txns.extend(page_data)
            before_sig = page_data[-1].get("signature")
        return all_txns

    async def _fetch_oldest_wallet_txns(
        self, wallet: str, sig_count: int
    ) -> list:
        """Fetch oldest transactions for a wallet to find original funders.
        Paginates getSignaturesForAddress to reach the end, then parses
        the oldest batch with Helius Enhanced API."""
        session = await self.helius._get_session()
        all_sigs = []
        before = None
        max_pages = min(max(1, (sig_count // 1000) + 1), 3)

        for page in range(max_pages):
            params = {"limit": 1000}
            if before:
                params["before"] = before
            key = random.choice(self.helius._keys) if self.helius._keys else ""
            url = f"https://mainnet.helius-rpc.com/?api-key={key}"
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "getSignaturesForAddress",
                "params": [wallet, params],
            }
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        break
                    data = await resp.json()
                    result = data.get("result", [])
                    if not result:
                        break
                    all_sigs.extend(result)
                    if len(result) < 1000:
                        break
                    before = result[-1]["signature"]
            except Exception:
                break

        if not all_sigs:
            return []

        # Take the oldest 20 sigs (last in list since newest-first order)
        oldest_sigs = [s["signature"] for s in all_sigs[-20:]]
        return await self._parse_transactions_batch(oldest_sigs)

    async def _rpc_call(self, method: str, params: list):
        """Solana RPC call — Helius first for speed + accuracy."""
        session = await self.helius._get_session()
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": method, "params": params,
        }

        # Helius RPC first (reliable, full history)
        key = random.choice(self.helius._keys) if self.helius._keys else ""
        url = f"https://mainnet.helius-rpc.com/?api-key={key}"
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = data.get("result")
                    if result is not None:
                        return result
        except Exception:
            pass

        # Fallback: free RPC
        if FREE_RPC_ENDPOINTS:
            url = random.choice(FREE_RPC_ENDPOINTS)
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("result")
            except Exception:
                pass

        return None

    async def _parse_transactions_batch(self, signatures: list) -> list:
        """Parse a batch of signatures into enhanced txn data.
        Retries up to 3 times with different keys on 429."""
        for attempt in range(3):
            key = random.choice(self.helius._keys)
            url = f"https://api.helius.xyz/v0/transactions?api-key={key}"
            try:
                session = await self.helius._get_session()
                async with session.post(
                    url, json={"transactions": signatures}
                ) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(random.uniform(0.3, 1.0))
                        continue
                    if resp.status != 200:
                        return []
                    return await resp.json()
            except Exception:
                return []
        return []
