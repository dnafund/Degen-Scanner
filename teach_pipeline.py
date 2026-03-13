"""
Degen Scanner - Teaching Pipeline

Wraps the existing DevTracer to provide an extended teaching flow:

  Step 0: Trace mint wallet (who deployed? funder of mint?)
  Step 1-7: Standard DevTracer.trace(mint)
  Step 8: Deep trace each taught wallet (5 hops up + down)
  Step 9: Backward expand from collectors → find more dev wallets
  Step 10: Extract patterns + save to DB

Usage:
    pipeline = TeachingPipeline(helius=helius_client)
    result = await pipeline.teach(mint, taught_wallets, on_progress=callback)
"""

import asyncio
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional

from dev_tracer import DevTracer, TraceResult, HOP_DEPTH, MAX_CONCURRENT
from helius import HeliusClient, is_on_curve
from pump_analyzer import CEX_HOT_WALLETS
import teach_store

logger = logging.getLogger(__name__)

# Limits
MAX_BACKWARD_EXPAND_PER_COLLECTOR = 500
BACKWARD_EXPAND_MAX_COLLECTORS = 20
TEACH_TIMEOUT = 1200  # 20 min


@dataclass
class TeachResult:
    """Result of a teaching session."""
    case_id: int = 0
    mint: str = ""
    token_name: str = ""
    token_symbol: str = ""
    taught_wallets: list[str] = field(default_factory=list)
    discovered_wallets: list[str] = field(default_factory=list)
    funders: list[str] = field(default_factory=list)
    collectors: list[str] = field(default_factory=list)
    patterns: list[dict] = field(default_factory=list)
    trace_result: Optional[TraceResult] = None
    error: str = ""


class TeachingPipeline:
    """
    Extended teaching pipeline that wraps DevTracer.

    Adds deep tracing of taught wallets and backward expansion
    from collectors to discover the full dev wallet network.
    """

    def __init__(self, helius: HeliusClient):
        self.helius = helius
        self.tracer = DevTracer(helius=helius)

    async def teach(
        self,
        mint: str,
        taught_wallets: list[str],
        on_progress: Optional[Callable] = None,
    ) -> TeachResult:
        """
        Main teaching pipeline.

        Args:
            mint: Token mint address
            taught_wallets: Known dev wallets provided by user
            on_progress: Optional async callback(step, message)
        """
        result = TeachResult(mint=mint, taught_wallets=list(taught_wallets))

        async def progress(step: int, msg: str):
            logger.info(f"Teach [{step}]: {msg}")
            if on_progress:
                try:
                    await on_progress(step, msg)
                except Exception:
                    pass

        try:
            async with asyncio.timeout(TEACH_TIMEOUT):
                await self._run_pipeline(mint, taught_wallets, result, progress)
        except TimeoutError:
            result.error = f"Teach pipeline timed out ({TEACH_TIMEOUT}s)"
            logger.warning(f"Teach timeout for {mint[:12]}")
        except Exception as e:
            result.error = str(e)
            logger.error(f"Teach error: {e}", exc_info=True)

        return result

    async def _run_pipeline(
        self,
        mint: str,
        taught_wallets: list[str],
        result: TeachResult,
        progress: Callable,
    ) -> None:
        sem = asyncio.Semaphore(MAX_CONCURRENT)
        all_dev_wallets: set[str] = set(taught_wallets)
        all_funders: set[str] = set()
        all_collectors: set[str] = set()

        # ── Step 0: Trace mint wallet ─────────────────────
        await progress(0, "Trace mint wallet + funder...")
        mint_funder = await self._trace_mint_wallet(mint, sem)
        if mint_funder:
            all_funders.add(mint_funder)

        # ── Step 1-7: Standard DevTrace ───────────────────
        await progress(1, "Running DevTracer pipeline...")
        trace_result = await self.tracer.trace(
            mint,
            on_progress=lambda step, msg: progress(step, f"DevTrace: {msg}"),
        )
        result.trace_result = trace_result
        result.token_name = trace_result.token_name
        result.token_symbol = trace_result.token_symbol

        # Collect wallets from trace result
        for cluster in trace_result.clusters:
            if cluster.shared_funder and cluster.shared_funder not in CEX_HOT_WALLETS:
                all_funders.add(cluster.shared_funder)
            if cluster.shared_collector and cluster.shared_collector not in CEX_HOT_WALLETS:
                all_collectors.add(cluster.shared_collector)
            for wt in cluster.wallets:
                all_dev_wallets.add(wt.wallet)

        await progress(7, f"DevTrace done: {len(trace_result.clusters)} clusters")

        # ── Step 8: Deep trace each taught wallet ─────────
        await progress(8, f"Deep tracing {len(taught_wallets)} taught wallets...")
        for tw in taught_wallets:
            tw_funders, tw_collectors = await self._deep_trace_wallet(
                tw, sem
            )
            for f in tw_funders:
                if f not in CEX_HOT_WALLETS:
                    all_funders.add(f)
            for c in tw_collectors:
                if c not in CEX_HOT_WALLETS:
                    all_collectors.add(c)

        await progress(
            8,
            f"Taught wallet trace done: {len(all_funders)} funders, "
            f"{len(all_collectors)} collectors",
        )

        # ── Step 9: Backward expand from collectors ───────
        await progress(9, f"Backward expand from {len(all_collectors)} collectors...")
        expanded = await self._backward_expand(
            list(all_collectors)[:BACKWARD_EXPAND_MAX_COLLECTORS],
            all_dev_wallets,
            sem,
        )
        all_dev_wallets.update(expanded)

        await progress(
            9,
            f"Backward expand done: +{len(expanded)} wallets "
            f"(total: {len(all_dev_wallets)})",
        )

        # ── Step 10: Extract patterns + save DB ───────────
        await progress(10, "Extracting patterns + saving...")

        # Separate taught vs discovered
        taught_set = set(taught_wallets)
        discovered = [w for w in all_dev_wallets if w not in taught_set]
        result.discovered_wallets = discovered
        result.funders = list(all_funders)
        result.collectors = list(all_collectors)

        # Save case
        case_id = teach_store.save_case(
            mint=mint,
            token_name=result.token_name,
            token_symbol=result.token_symbol,
            taught_wallets=taught_wallets,
            discovered_wallets=discovered,
            funders=list(all_funders),
            collectors=list(all_collectors),
        )
        result.case_id = case_id

        # Extract and save patterns
        patterns = self._extract_patterns(
            mint=mint,
            taught_wallets=taught_wallets,
            all_dev_wallets=all_dev_wallets,
            funders=all_funders,
            collectors=all_collectors,
            mint_funder=mint_funder,
        )
        result.patterns = patterns
        if patterns:
            teach_store.save_patterns(case_id, patterns)

        # Save known dev wallets
        wallet_entries = []
        for w in taught_wallets:
            wallet_entries.append({
                "wallet": w,
                "role": "taught",
                "confidence": 0.9,
            })
        for w in discovered:
            wallet_entries.append({
                "wallet": w,
                "role": "buyer",
                "confidence": 0.5,
            })
        for w in all_funders:
            wallet_entries.append({
                "wallet": w,
                "role": "funder",
                "confidence": 0.7,
            })
        for w in all_collectors:
            wallet_entries.append({
                "wallet": w,
                "role": "collector",
                "confidence": 0.6,
            })
        if wallet_entries:
            teach_store.save_dev_wallets(case_id, mint, wallet_entries)

        await progress(
            10,
            f"Saved case #{case_id}: "
            f"{len(taught_wallets)} taught, {len(discovered)} discovered, "
            f"{len(patterns)} patterns",
        )

    # ─── Step 0: Trace mint wallet ────────────────────────

    async def _trace_mint_wallet(
        self, mint: str, sem: asyncio.Semaphore
    ) -> str:
        """
        Find the deployer wallet and its funder.
        Returns funder address or empty string.
        """
        try:
            # Get first txns for the mint (deployer = fee payer of first tx)
            async with sem:
                txns = await self.tracer._fetch_wallet_txns(mint, key_offset=0)

            if not txns:
                return ""

            # Sort by timestamp, first tx fee payer = deployer
            txns.sort(key=lambda t: t.get("timestamp", 0))
            deployer = txns[0].get("feePayer", "")
            if not deployer:
                return ""

            logger.info(f"Teach: mint deployer = {deployer[:12]}...")

            # Trace deployer's funding source (1 hop)
            async with sem:
                dep_txns = await self.tracer._fetch_wallet_txns(
                    deployer, key_offset=1
                )
            if not dep_txns:
                return ""

            best_from, best_amt = "", 0.0
            for tx in dep_txns:
                for nt in tx.get("nativeTransfers", []):
                    ta = nt.get("toUserAccount", "")
                    fa = nt.get("fromUserAccount", "")
                    a = nt.get("amount", 0) / 1e9
                    if (
                        ta == deployer
                        and fa != deployer
                        and a > 0.01
                        and a > best_amt
                    ):
                        best_amt = a
                        best_from = fa

            if best_from:
                logger.info(
                    f"Teach: mint funder = {best_from[:12]}... "
                    f"({best_amt:.3f} SOL)"
                )
            return best_from

        except Exception as e:
            logger.error(f"Trace mint wallet error: {e}")
            return ""

    # ─── Step 8: Deep trace taught wallets ────────────────

    async def _deep_trace_wallet(
        self,
        wallet: str,
        sem: asyncio.Semaphore,
    ) -> tuple[list[str], list[str]]:
        """
        Trace a taught wallet 5 hops up (funders) and 5 hops down (collectors).
        Returns (funders, collectors).
        """
        funders = []
        collectors = []

        # Upstream: funder chain
        current = wallet
        visited = set()
        for hop in range(HOP_DEPTH):
            if current in visited:
                break
            visited.add(current)
            async with sem:
                txns = await self.tracer._fetch_wallet_txns(
                    current, key_offset=hop + 2000
                )
            if not txns:
                break
            best_from, best_amt = "", 0.0
            for tx in txns:
                for nt in tx.get("nativeTransfers", []):
                    ta = nt.get("toUserAccount", "")
                    fa = nt.get("fromUserAccount", "")
                    a = nt.get("amount", 0) / 1e9
                    if ta == current and fa != current and a > 0.01 and a > best_amt:
                        best_amt = a
                        best_from = fa
            if best_from:
                funders.append(best_from)
                current = best_from
            else:
                break

        # Downstream: collector chain
        current = wallet
        visited = set()
        for hop in range(HOP_DEPTH):
            if current in visited:
                break
            visited.add(current)
            async with sem:
                txns = await self.tracer._fetch_wallet_txns(
                    current, key_offset=hop + 3000
                )
            if not txns:
                break
            best_to, best_amt = "", 0.0
            for tx in txns:
                for nt in tx.get("nativeTransfers", []):
                    fa = nt.get("fromUserAccount", "")
                    ta = nt.get("toUserAccount", "")
                    a = nt.get("amount", 0) / 1e9
                    if (
                        fa == current
                        and ta != current
                        and a > 0.01
                        and is_on_curve(ta)
                        and a > best_amt
                    ):
                        best_amt = a
                        best_to = ta
            if best_to:
                collectors.append(best_to)
                current = best_to
            else:
                break

        return funders, collectors

    # ─── Step 9: Backward expand from collectors ──────────

    async def _backward_expand(
        self,
        collectors: list[str],
        existing_wallets: set[str],
        sem: asyncio.Semaphore,
    ) -> set[str]:
        """
        From each collector, fetch ALL incoming txns to find more dev wallets.
        Skip CEX hot wallets. Cap per collector.
        """
        expanded = set()

        async def expand_one(idx: int, collector: str) -> set[str]:
            found = set()
            async with sem:
                txns = await self.tracer._fetch_wallet_txns_paginated(
                    collector, max_pages=5, key_start=idx + 4000
                )
            if not txns:
                return found

            for tx in txns:
                for nt in tx.get("nativeTransfers", []):
                    ta = nt.get("toUserAccount", "")
                    fa = nt.get("fromUserAccount", "")
                    a = nt.get("amount", 0) / 1e9
                    if (
                        ta == collector
                        and fa != collector
                        and a >= 0.01
                        and fa not in CEX_HOT_WALLETS
                        and is_on_curve(fa)
                    ):
                        found.add(fa)
                        if len(found) >= MAX_BACKWARD_EXPAND_PER_COLLECTOR:
                            break

            return found

        tasks = [expand_one(i, c) for i, c in enumerate(collectors)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, set):
                expanded.update(r)

        return expanded

    # ─── Step 10: Pattern extraction ──────────────────────

    def _extract_patterns(
        self,
        mint: str,
        taught_wallets: list[str],
        all_dev_wallets: set[str],
        funders: set[str],
        collectors: set[str],
        mint_funder: str = "",
    ) -> list[dict]:
        """Extract reusable patterns from the teach result."""
        patterns = []

        # Funder patterns
        for funder in funders:
            if funder in CEX_HOT_WALLETS:
                continue
            patterns.append({
                "pattern_type": "funder_wallet",
                "wallet": funder,
                "related_wallet": mint,
                "confidence": 0.7,
                "metadata": {"source": "teach_pipeline"},
            })

        # Collector patterns
        for collector in collectors:
            if collector in CEX_HOT_WALLETS:
                continue
            patterns.append({
                "pattern_type": "collector_wallet",
                "wallet": collector,
                "related_wallet": mint,
                "confidence": 0.6,
                "metadata": {"source": "teach_pipeline"},
            })

        # Mint authority pattern (deployer's funder)
        if mint_funder and mint_funder not in CEX_HOT_WALLETS:
            patterns.append({
                "pattern_type": "mint_authority",
                "wallet": mint_funder,
                "related_wallet": mint,
                "confidence": 0.9,
                "metadata": {"source": "teach_pipeline"},
            })

        # Wallet reuse patterns (taught wallets are high confidence)
        for tw in taught_wallets:
            patterns.append({
                "pattern_type": "wallet_reuse",
                "wallet": tw,
                "related_wallet": mint,
                "confidence": 0.8,
                "metadata": {"source": "user_taught"},
            })

        return patterns
