"""Parallel chain tracer — follow outgoing SOL from source to final destinations."""

import asyncio
import logging
import random

from helius import HeliusClient

logger = logging.getLogger(__name__)

MAX_HOPS = 20
CONCURRENCY = 5
MAX_BRANCHES = 5  # Max outgoing to trace per hop (prevents hub wallet explosion)


async def trace_forward(
    helius: HeliusClient,
    source: str,
    max_hops: int = MAX_HOPS,
    concurrency: int = CONCURRENCY,
) -> list[str]:
    """
    Trace forward from source wallet to find all final destination wallets.

    Handles recursive splits: if a sub-wallet splits again, trace ALL branches.

    Logic per wallet:
    - Has swap activity → destination, STOP
    - Has multiple non-CEX outgoing → split, trace ALL branches
    - Has 1 non-CEX outgoing → forwarding, follow it
    - No outgoing / all CEX → dead end
    """
    from pump_analyzer import lookup_cex_name

    # Auto-warmup: if no dead keys known yet, run health check to discover them
    if not helius._dead_keys and len(helius._all_keys) > 50:
        logger.info("trace_forward: fresh client, warming up keys...")
        health = await helius.check_keys_health(force=True)
        logger.info(
            f"trace_forward: warmup done — "
            f"active={health.get('active', '?')}, "
            f"dead={health.get('no_credits', '?')}"
        )

    sem = asyncio.Semaphore(concurrency)
    visited = set()

    async def _get_trace_info(wallet: str) -> dict | None:
        """Get wallet trace info with concurrency control and retry."""
        async with sem:
            key = random.choice(helius._keys) if helius._keys else ""
            result = await helius.get_wallet_trace_info(wallet=wallet, key=key)
            if result is not None:
                return result
            if helius._keys:
                key2 = random.choice(helius._keys)
                return await helius.get_wallet_trace_info(wallet=wallet, key=key2)
            return None

    async def _trace_wallet(wallet: str, depth: int) -> list[str]:
        """Trace a single wallet recursively. Returns list of destination wallets."""
        if depth >= max_hops or wallet in visited:
            return [wallet] if depth > 0 else []
        visited.add(wallet)

        info = await _get_trace_info(wallet)
        if info is None:
            return [wallet]

        # Destination found: wallet has DEX/swap activity
        if info["has_swap"]:
            return [wallet]

        non_cex = [o for o in info["outgoing"] if not lookup_cex_name(o["to_wallet"])]
        if not non_cex:
            return [wallet] if depth > 0 else []

        # Hub detection: real forwarding wallets have 1-5 outgoing.
        # More than that = active/hub wallet, not forwarding → stop here.
        if len(non_cex) > MAX_BRANCHES:
            return [wallet]

        # Trace outgoing branches (handles both forwarding and splits)
        tasks = [
            _trace_wallet(o["to_wallet"], depth + 1)
            for o in non_cex
            if o["to_wallet"] not in visited
        ]
        if not tasks:
            return [wallet]

        results = await asyncio.gather(*tasks)
        destinations = []
        for sublist in results:
            destinations.extend(sublist)
        return destinations

    # Step 1: Get source's sub-wallets
    source_info = await _get_trace_info(source)
    if source_info is None or not source_info["outgoing"]:
        logger.info("trace_forward: no outgoing from source")
        return [source]

    non_cex_out = [o for o in source_info["outgoing"] if not lookup_cex_name(o["to_wallet"])]
    if not non_cex_out:
        logger.info("trace_forward: all outgoing are CEX")
        return [source]

    logger.info(f"trace_forward: source has {len(non_cex_out)} sub-wallets, following chains...")

    # Step 2: Trace all sub-wallets (recursive — handles splits at any depth)
    tasks = [_trace_wallet(o["to_wallet"], 1) for o in non_cex_out]
    results = await asyncio.gather(*tasks)

    # Flatten and deduplicate while preserving order
    seen = set()
    dest_wallets = []
    for sublist in results:
        for w in sublist:
            if w not in seen:
                seen.add(w)
                dest_wallets.append(w)

    logger.info(f"trace_forward: {len(dest_wallets)} final destinations")
    return [source] + dest_wallets
