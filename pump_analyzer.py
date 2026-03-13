"""
Degen Scanner - Pump.fun Token Analyzer

Reverse cluster analysis: given a Pump.fun token mint address,
find all pre-bond buyers, trace their funding sources (CEX?),
and cluster by behavioral patterns.

Flow:
  /analyze <mint>
    → fetch token metadata (DAS API)
    → derive bonding curve PDA
    → fetch all pre-bond transactions
    → parse buyers (SOL → bonding curve = buy)
    → trace funding source for each buyer (parallel)
    → cluster by patterns (CEX, timing, test wallets)
    → format and send results
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Dict, Tuple

from solders.pubkey import Pubkey

from helius import HeliusClient

logger = logging.getLogger(__name__)

# ─── Known CEX Hot Wallets ────────────────────────────────────
# Maps known Solana CEX hot wallet addresses to exchange names.
# These are the main withdrawal wallets for major exchanges.
CEX_HOT_WALLETS: dict[str, str] = {
    # Binance
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S": "Binance",
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": "Binance",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "Binance",
    "3yFwqXBfZY4jBVUafQ1YEXw189y2dN3V5KQq9uzBDy1E": "Binance",
    # OKX
    "5VCwKtCXgCDuQosrzGRDzfkak1MJRHxgCo8FPsJNhEX3": "OKX",
    "4jJTxNqhPPWGUbqB9zpj4FsVFJZGmSrbHx2FN5kFgnYd": "OKX",
    # Bybit
    "AC5RDfQFmDS1deWZos921JfqscXdByf6BKHAbXjKUixd": "Bybit",
    "GJRs4FwHtemZ5ZE9Q3MNFTAs5r45JhJd4Fb9EgpKGMJx": "Bybit",
    # KuCoin
    "BmFdpraQhkiDQE6SNLaxvJFVdB1MiJHQF3bNMwt3xEze": "KuCoin",
    "FxteHmLwG9nk1eL4pjNve3Eub2goGkkz6g6TbvdmW46a": "KuCoin",
    # MEXC
    "ASTyfSima4LLAdDgoFGkgqoKowG1LZFDr9fAQrg7iaJZ": "MEXC",
    # Gate.io
    "u6PJ8DtQuPFnfmwHbGFULQ4u4EgjDiyYKjVEsynXq2w": "Gate.io",
    # Coinbase
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": "Coinbase",
    "GJa1VeEYLSMfJqSMGnMRKte82bVbD1FvJbwssxZJdS5N": "Coinbase",
    "2AQdump6KVb8MEVCpiaGmVKbCXvqRGiijRxEMEvSMR3Z": "Coinbase",
    # Kraken
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5": "Kraken",
    # Bitget
    "GjGPnQegRGqRuDm7DaGFoVcqAkNBU4JiXhkkNhnri5rD": "Bitget",
    # Crypto.com
    "AobVSwdW9BbpMdJvTqeCN4hPAmh4rHm7vwLnQ5ATbo3f": "Crypto.com",
    # HTX (Huobi)
    "88xTWZMeKfiTgbfEmPLdsUCQcZinwUfk25MXBUL87ez2": "HTX",
}

# ─── Trading Platform Fee Vaults ─────────────────────────────
# Fee vaults collect small SOL fees from every trade.
# Unlike CEX wallets, these are NOISE — not funding signals.
# Completely excluded from convergence (no CEX expansion).
FEE_VAULTS: set[str] = {
    # GMGN (router: GMgnVFR8Jb39LoXsEVzb3DvBy3ywCmdmJquHUy1Lrkqb)
    "DymeoWc5WLNiQBaoLuxrxDnDRvLgGZ1QGsEoCAM7Jsrx",
    "dBhdrmwBkRa66XxBuAK4WZeZnsZ6bHeHCCLXa3a8bTJ",
    "6TxjC5wJzuuZgTtnTMipwwULEbMPx5JPW3QwWkdTGnrn",
    "7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ",
    "AQChXZ1ZWvPH8EjdPxXXsC8VqCaBmPVruJbswhE3xNZ8",
    "4BjQeBGZmGNWeHfQC4scHK5d4RtDr79h1hZNPcrLDS8C",
    "7sHXjs1j7sDJGVSMSPjD1b4v3FD6uRSvRWfhRdfv5BiA",
    "HeZVpHj9jLwTVtMMbzQRf6mLtFPkWNSg11o68qrbUBa3",
    "3t9EKmRiAUcQUYzTZpNojzeGP1KBAVEEbDNmy6wECQpK",
    "DXfkEGoo6WFsdL7x6gLZ7r6Hw2S6HrtrAQVPWYx2A1s9",
    "ByRRgnZenY6W2sddo1VJzX9o4sMU4gPDUkcmgrpGBxRy",
    "uXYxbbhcD9kHU1KzZM6ZNzPvieEvRcJCPHpqcnpwdcR",
    "BvH5CteddzPsNquinN4NKTHqcUtAnxDjVtFeGrx9E8jQ",
    # Trojan (router: troY36YiPGqMyAYCNbEqYCdN2tb91Zf7bHcQt7KUi61)
    "62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV",
    "9yMwSPk9mrXSN7yDHUuZurAh1sjbJsfpUqjZ7SvVtdco",
    # Other trading bots
    "FogxVNs6Mm2w9rnGL1vkARSwJxvLE8mujTv3LK8RnUhF",
    "HWEoBxYs7ssKuudEjzjmpfJVX7Dvi7wescFsVx2L5yoY",
    "ste11MWPjXCRfQryCshzi86SGhuXjF4Lv6xMXD2AoSt",
}

# Launchpad program IDs
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
RAYDIUM_LAUNCHLAB_PROGRAM = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
MOONSHOT_PROGRAM = "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG"
METEORA_DBC_PROGRAM = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
BOOP_PROGRAM = "boop8hVGQGqehUK2iVEMEnMrL5RbjywRzHKBmBE7ry4"

# Limits
MAX_PRE_BOND_TXS = 5000
MAX_WALLETS_TO_TRACE = 200
MAX_CONCURRENT_TRACE = 30
ANALYSIS_TIMEOUT = 55
MAX_WALLET_ACTIVITY = 1000  # skip wallets with >1000 txns (traders/bots)


# ─── Dataclasses ──────────────────────────────────────────────

@dataclass
class TokenBuy:
    """A single buy on the bonding curve."""
    wallet: str
    sol_spent: float
    timestamp: int  # unix timestamp
    signature: str
    buy_index: int  # order of buy (1 = first buyer)


@dataclass
class WalletFunding:
    """Funding source trace result for a buyer wallet."""
    wallet: str
    cex_name: str
    cex_source_wallet: str
    funding_amount: float
    timestamp: int


@dataclass
class BuyerCluster:
    """A detected cluster of related buyers."""
    cluster_type: str  # cex_funded, same_source, timing_high/med/low, test_wallet
    wallets: List[str]
    cex_name: str = ""
    amount_pattern: str = ""
    notes: str = ""


@dataclass
class TokenAnalysisResult:
    """Complete analysis result for a Pump.fun token."""
    mint: str
    token_name: str = ""
    token_symbol: str = ""
    bonding_curve: str = ""
    total_buys: int = 0
    unique_buyers: int = 0
    buys: List[TokenBuy] = field(default_factory=list)
    funding: List[WalletFunding] = field(default_factory=list)
    clusters: List[BuyerCluster] = field(default_factory=list)
    unfunded: List[str] = field(default_factory=list)  # wallets with no CEX trace
    error: str = ""


# ─── PDA Derivation ──────────────────────────────────────────

WSOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ASSOCIATED_TOKEN_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"


def derive_ata(owner_str: str, mint_str: str) -> str:
    """Derive the Associated Token Account address for a (owner, mint) pair."""
    owner = Pubkey.from_string(owner_str)
    mint = Pubkey.from_string(mint_str)
    token_program = Pubkey.from_string(TOKEN_PROGRAM_ID)
    ata_program = Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID)
    pda, _ = Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)],
        ata_program,
    )
    return str(pda)


def derive_bonding_curve_pda(mint_str: str, platform: str = "pumpfun") -> str:
    """
    Derive the bonding curve PDA for a token on various launchpads.

    Supported platforms: pumpfun, launchlab (bonk.fun), moonshot, meteora (believe)
    """
    mint_pubkey = Pubkey.from_string(mint_str)

    if platform == "pumpfun":
        # Seeds: ["bonding-curve", mint]
        pda, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(mint_pubkey)],
            Pubkey.from_string(PUMP_FUN_PROGRAM),
        )
    elif platform == "launchlab":
        # Raydium LaunchLab / Bonk.fun — Seeds: ["pool", mintA, mintB]
        wsol = Pubkey.from_string(WSOL_MINT)
        pda, _ = Pubkey.find_program_address(
            [b"pool", bytes(mint_pubkey), bytes(wsol)],
            Pubkey.from_string(RAYDIUM_LAUNCHLAB_PROGRAM),
        )
    elif platform == "moonshot":
        # Moonshot — Seeds: ["bonding-curve", mint] (same pattern as pump)
        pda, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(mint_pubkey)],
            Pubkey.from_string(MOONSHOT_PROGRAM),
        )
    elif platform == "meteora":
        # Meteora DBC (Believe.app) — Seeds: ["pool", baseMint, quoteMint, config]
        # Without config key, use simpler derivation
        wsol = Pubkey.from_string(WSOL_MINT)
        pda, _ = Pubkey.find_program_address(
            [b"pool", bytes(mint_pubkey), bytes(wsol)],
            Pubkey.from_string(METEORA_DBC_PROGRAM),
        )
    else:
        # Default: pump.fun
        pda, _ = Pubkey.find_program_address(
            [b"bonding-curve", bytes(mint_pubkey)],
            Pubkey.from_string(PUMP_FUN_PROGRAM),
        )

    return str(pda)


def detect_platform(mint_str: str, helius_txns: list = None) -> str:
    """
    Detect which launchpad platform a token was created on.
    Checks first transaction's program IDs.
    """
    if not helius_txns:
        return "pumpfun"  # default

    program_ids = set()
    for tx in helius_txns[:10]:
        # Check accountData / instructions for program IDs
        for inst in tx.get("instructions", []):
            pid = inst.get("programId", "")
            if pid:
                program_ids.add(pid)
        # Also check top-level source
        source = tx.get("source", "")
        if source:
            program_ids.add(source)

    if RAYDIUM_LAUNCHLAB_PROGRAM in program_ids:
        return "launchlab"
    if MOONSHOT_PROGRAM in program_ids:
        return "moonshot"
    if METEORA_DBC_PROGRAM in program_ids:
        return "meteora"
    if BOOP_PROGRAM in program_ids:
        return "boop"

    return "pumpfun"


_dynamic_cex: dict[str, str] = {}


def load_dynamic_cex() -> None:
    """Load learned CEX wallets from DB into memory."""
    global _dynamic_cex
    try:
        from db import load_cex_wallets
        _dynamic_cex = load_cex_wallets()
        if _dynamic_cex:
            logger.info(f"Loaded {len(_dynamic_cex)} CEX wallets from DB")
    except Exception as e:
        logger.warning(f"Failed to load CEX wallets from DB: {e}")


def register_cex_wallet(address: str, name: str) -> None:
    """Register a CEX wallet (save to DB + memory)."""
    _dynamic_cex[address] = name
    try:
        from db import save_cex_wallet
        save_cex_wallet(address, name)
    except Exception as e:
        logger.warning(f"Failed to save CEX wallet: {e}")


def lookup_cex_name(wallet_address: str) -> Optional[str]:
    """Look up a wallet address in known CEX hot wallets (hardcoded + learned)."""
    return CEX_HOT_WALLETS.get(wallet_address) or _dynamic_cex.get(wallet_address)


# ─── PumpAnalyzer ─────────────────────────────────────────────

class PumpAnalyzer:
    """
    Analyzes a Pump.fun token: finds pre-bond buyers,
    traces their funding, and clusters by behavior.
    """

    def __init__(self, helius: HeliusClient):
        self.helius = helius

    async def analyze(self, mint: str) -> TokenAnalysisResult:
        """
        Main analysis pipeline. Returns TokenAnalysisResult.

        Steps:
          1. Fetch token metadata (DAS)
          2. Derive bonding curve PDA
          3. Fetch pre-bond transactions
          4. Parse buyers
          5. Trace funding (parallel)
          6. Cluster by patterns
        """
        result = TokenAnalysisResult(mint=mint)

        try:
            async with asyncio.timeout(ANALYSIS_TIMEOUT):
                # Step 1: Token metadata
                asset = await self.helius.get_das_asset(mint)
                if asset:
                    content = asset.get("content", {})
                    metadata = content.get("metadata", {})
                    result.token_name = metadata.get("name", "")
                    result.token_symbol = metadata.get("symbol", "")

                # Step 2: Derive bonding curve PDA
                result.bonding_curve = derive_bonding_curve_pda(mint)
                logger.info(
                    f"Pump analyze: {result.token_symbol or mint[:8]} "
                    f"PDA={result.bonding_curve[:12]}..."
                )

                # Step 3: Fetch pre-bond transactions
                txns = await self.helius.get_token_transactions(
                    address=result.bonding_curve,
                    source_filter="PUMP_FUN",
                    max_pages=50,
                )

                if not txns:
                    result.error = "No transactions found for this token"
                    return result

                # Step 4: Parse buyers
                buys = self._parse_buyers(txns, result.bonding_curve)
                result.buys = buys
                result.total_buys = len(buys)
                result.unique_buyers = len(set(b.wallet for b in buys))

                if not buys:
                    result.error = "No buy transactions found"
                    return result

                # Step 4.5: Filter out high-activity wallets (>1000 txns)
                high_activity = await self._filter_high_activity_wallets(buys)
                if high_activity:
                    logger.info(
                        f"Filtered {len(high_activity)} high-activity wallets "
                        f"(>{MAX_WALLET_ACTIVITY} txns)"
                    )
                    buys = [b for b in buys if b.wallet not in high_activity]
                    result.buys = buys
                    result.total_buys = len(buys)
                    result.unique_buyers = len(set(b.wallet for b in buys))

                # Step 5: Trace funding (parallel)
                funding = await self._trace_funding_parallel(buys)
                result.funding = funding
                funded_wallets = {f.wallet for f in funding}
                result.unfunded = [
                    w for w in set(b.wallet for b in buys)
                    if w not in funded_wallets
                ]

                # Step 6: Cluster
                result.clusters = self._cluster_buyers(buys, funding)

        except TimeoutError:
            result.error = "Analysis timed out (55s limit)"
            logger.warning(f"Pump analyze timeout for {mint[:12]}...")
        except Exception as e:
            result.error = str(e)
            logger.error(f"Pump analyze error: {e}", exc_info=True)

        return result

    def _parse_buyers(
        self, txns: List[dict], bonding_curve: str
    ) -> List[TokenBuy]:
        """
        Parse buy transactions from Helius enhanced transaction data.

        A buy = SOL transferred TO the bonding curve PDA.
        The feePayer is the buyer wallet.
        """
        buys: List[TokenBuy] = []
        seen_sigs: set[str] = set()

        # Sort by timestamp ascending (earliest first)
        sorted_txns = sorted(txns, key=lambda t: t.get("timestamp", 0))

        buy_index = 0
        for tx in sorted_txns:
            sig = tx.get("signature", "")
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)

            ts = tx.get("timestamp", 0)
            fee_payer = tx.get("feePayer", "")

            # Find SOL sent to bonding curve = buy
            total_sol_to_curve = 0.0
            for nt in tx.get("nativeTransfers", []):
                to_addr = nt.get("toUserAccount", "")
                lamports = nt.get("amount", 0)
                if to_addr == bonding_curve and lamports > 0:
                    total_sol_to_curve += lamports / 1_000_000_000

            if total_sol_to_curve > 0 and fee_payer:
                buy_index += 1
                buys.append(TokenBuy(
                    wallet=fee_payer,
                    sol_spent=total_sol_to_curve,
                    timestamp=ts,
                    signature=sig,
                    buy_index=buy_index,
                ))

        logger.info(
            f"Parsed {len(buys)} buys from {len(txns)} txns "
            f"({len(set(b.wallet for b in buys))} unique wallets)"
        )
        return buys

    async def _filter_high_activity_wallets(
        self, buys: List[TokenBuy]
    ) -> set[str]:
        """
        Check unique buyer wallets and return those with >MAX_WALLET_ACTIVITY txns.

        Uses Solana getSignaturesForAddress RPC via Helius with limit=1000.
        If 1000 results returned = wallet has >1000 txns = filter it out.
        """
        unique_wallets = list(dict.fromkeys(b.wallet for b in buys))[:MAX_WALLETS_TO_TRACE]
        high_activity: set[str] = set()
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_TRACE)
        num_keys = len(self.helius._keys)

        async def _check_one(idx: int, wallet: str) -> Optional[str]:
            key = self.helius._keys[idx % num_keys]
            # Use Solana RPC getSignaturesForAddress via Helius
            # limit=1000: if we get 1000 back, wallet has >1000 txns
            url = f"https://mainnet.helius-rpc.com/?api-key={key}"
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignaturesForAddress",
                "params": [wallet, {"limit": 1000}],
            }

            async with semaphore:
                try:
                    session = await self.helius._get_session()
                    async with session.post(url, json=payload) as resp:
                        if resp.status != 200:
                            return None
                        data = await resp.json()
                        sigs = data.get("result", [])
                        if len(sigs) >= 1000:
                            return wallet
                        return None
                except Exception:
                    return None

        tasks = [_check_one(i, w) for i, w in enumerate(unique_wallets)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, str):
                high_activity.add(r)

        return high_activity

    async def _trace_funding_parallel(
        self, buys: List[TokenBuy]
    ) -> List[WalletFunding]:
        """
        Trace funding source for each unique buyer wallet.
        Runs up to MAX_CONCURRENT_TRACE in parallel.
        """
        # Deduplicate wallets, keep earliest buy timestamp
        wallet_first_buy: dict[str, int] = {}
        for buy in buys:
            if buy.wallet not in wallet_first_buy:
                wallet_first_buy[buy.wallet] = buy.timestamp
            else:
                wallet_first_buy[buy.wallet] = min(
                    wallet_first_buy[buy.wallet], buy.timestamp
                )

        wallets = list(wallet_first_buy.items())[:MAX_WALLETS_TO_TRACE]
        logger.info(f"Tracing funding for {len(wallets)} wallets")

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_TRACE)
        num_keys = len(self.helius._keys)
        results: List[WalletFunding] = []

        async def _trace_one(
            idx: int, wallet: str, buy_ts: int
        ) -> Optional[WalletFunding]:
            key = self.helius._keys[idx % num_keys]
            async with semaphore:
                funding = await self.helius.get_wallet_incoming_sol(
                    wallet=wallet,
                    key=key,
                    before_timestamp=buy_ts,
                    lookback_hours=48,
                )

            if not funding:
                return None

            from_wallet = funding["from_wallet"]
            cex_name = lookup_cex_name(from_wallet)

            if not cex_name:
                # Not a known CEX — skip
                return None

            return WalletFunding(
                wallet=wallet,
                cex_name=cex_name,
                cex_source_wallet=from_wallet,
                funding_amount=funding["amount_sol"],
                timestamp=funding["timestamp"],
            )

        tasks = [
            _trace_one(i, wallet, buy_ts)
            for i, (wallet, buy_ts) in enumerate(wallets)
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in raw_results:
            if isinstance(r, Exception):
                logger.error(f"Funding trace exception: {r}")
                continue
            if r is not None:
                results.append(r)

        logger.info(
            f"Funding traced: {len(results)}/{len(wallets)} wallets "
            f"have CEX funding"
        )
        return results

    def _cluster_buyers(
        self,
        buys: List[TokenBuy],
        funding: List[WalletFunding],
    ) -> List[BuyerCluster]:
        """
        Cluster buyers by behavioral patterns.

        Cluster types:
          - cex_funded: Same CEX + similar/same funding amounts
          - same_source: Exact same CEX hot wallet
          - timing_high: 3+ buys within 5s
          - timing_medium: 3+ buys within 5-10s
          - timing_low: 3+ buys within 10-30s
          - test_wallet: micro-buys < 0.01 SOL
        """
        clusters: List[BuyerCluster] = []
        clustered_wallets: set[str] = set()

        # ─── 1. Same source wallet ───────────────────────────
        source_groups: dict[str, List[WalletFunding]] = {}
        for f in funding:
            source_groups.setdefault(f.cex_source_wallet, []).append(f)

        for source_wallet, group in source_groups.items():
            if len(group) >= 2:
                wallets = [f.wallet for f in group]
                cex_name = group[0].cex_name
                clusters.append(BuyerCluster(
                    cluster_type="same_source",
                    wallets=wallets,
                    cex_name=cex_name,
                    notes=f"All funded from same {cex_name} hot wallet "
                          f"{source_wallet[:8]}...",
                ))
                clustered_wallets.update(wallets)

        # ─── 2. CEX funded — same CEX + amount pattern ──────
        cex_groups: dict[str, List[WalletFunding]] = {}
        for f in funding:
            if f.wallet not in clustered_wallets:
                cex_groups.setdefault(f.cex_name, []).append(f)

        for cex_name, group in cex_groups.items():
            if len(group) >= 2:
                amounts = [f.funding_amount for f in group]
                pattern = self._detect_funding_pattern(amounts)
                if pattern != "random":
                    wallets = [f.wallet for f in group]
                    clusters.append(BuyerCluster(
                        cluster_type="cex_funded",
                        wallets=wallets,
                        cex_name=cex_name,
                        amount_pattern=pattern,
                        notes=f"{cex_name} funding with {pattern} pattern",
                    ))
                    clustered_wallets.update(wallets)

        # ─── 3. Timing clusters ─────────────────────────────
        # Sort buys by timestamp
        sorted_buys = sorted(buys, key=lambda b: b.timestamp)

        # Sliding window for different thresholds
        for window_sec, cluster_type in [
            (5, "timing_high"),
            (10, "timing_medium"),
            (30, "timing_low"),
        ]:
            timing_groups = self._find_timing_groups(
                sorted_buys, window_sec, min_size=3
            )
            for group in timing_groups:
                wallets = [b.wallet for b in group]
                # Only include wallets not already in a stronger cluster
                new_wallets = [w for w in wallets if w not in clustered_wallets]
                if len(new_wallets) >= 3:
                    time_range = group[-1].timestamp - group[0].timestamp
                    clusters.append(BuyerCluster(
                        cluster_type=cluster_type,
                        wallets=new_wallets,
                        notes=f"{len(new_wallets)} buys within {time_range}s",
                    ))
                    clustered_wallets.update(new_wallets)

        # ─── 4. Test wallets (micro-buys) ────────────────────
        test_wallets = [
            b.wallet for b in buys
            if b.sol_spent < 0.01 and b.wallet not in clustered_wallets
        ]
        unique_test = list(dict.fromkeys(test_wallets))  # dedupe, preserve order
        if len(unique_test) >= 2:
            clusters.append(BuyerCluster(
                cluster_type="test_wallet",
                wallets=unique_test,
                notes=f"{len(unique_test)} wallets with micro-buys (&lt; 0.01 SOL)",
            ))

        return clusters

    @staticmethod
    def _detect_funding_pattern(amounts: List[float]) -> str:
        """
        Detect pattern in funding amounts.

        Returns: "same", "increasing", "similar", or "random"
        """
        if len(amounts) < 2:
            return "random"

        # SAME: all amounts identical (±0.01 SOL)
        if all(abs(a - amounts[0]) < 0.01 for a in amounts):
            return "same"

        # INCREASING: each amount >= previous
        sorted_amounts = sorted(amounts)
        if sorted_amounts == amounts:
            return "increasing"

        # SIMILAR: all within 15% of average
        avg = sum(amounts) / len(amounts)
        if avg > 0:
            if all(abs(a - avg) / avg <= 0.15 for a in amounts):
                return "similar"

        return "random"

    @staticmethod
    def _find_timing_groups(
        sorted_buys: List[TokenBuy],
        window_sec: int,
        min_size: int = 3,
    ) -> List[List[TokenBuy]]:
        """
        Find groups of buys within window_sec of each other.
        Uses sliding window on sorted (by timestamp) buys.
        """
        groups: List[List[TokenBuy]] = []
        used: set[str] = set()  # signatures already grouped

        for i, buy in enumerate(sorted_buys):
            if buy.signature in used:
                continue

            group = [buy]
            for j in range(i + 1, len(sorted_buys)):
                other = sorted_buys[j]
                if other.signature in used:
                    continue
                if other.timestamp - buy.timestamp > window_sec:
                    break
                group.append(other)

            if len(group) >= min_size:
                groups.append(group)
                for b in group:
                    used.add(b.signature)

        return groups


# ─── Formatter ────────────────────────────────────────────────

CLUSTER_LABELS = {
    "same_source": "🔗 Same Source Wallet",
    "cex_funded": "💰 CEX Funded Pattern",
    "timing_high": "⚡ Bot/Script (&lt; 5s)",
    "timing_medium": "⏱ Coordinated (5-10s)",
    "timing_low": "🕐 Possibly Related (10-30s)",
    "test_wallet": "🧪 Test Wallets",
}


def format_pump_analysis(result: TokenAnalysisResult) -> List[str]:
    """
    Format analysis result into Telegram HTML message chunks.
    Each chunk ≤ 3800 chars (Telegram limit 4096 with margin).
    """
    if result.error:
        return [
            f"❌ <b>Analysis failed</b>\n\n"
            f"Token: <code>{result.mint[:12]}...</code>\n"
            f"Error: {result.error}"
        ]

    chunks: List[str] = []

    # ─── Header ──────────────────────────────────────────
    name_display = result.token_name or "Unknown"
    symbol_display = result.token_symbol or "???"
    header = (
        f"🔬 <b>Pump.fun Token Analysis</b>\n\n"
        f"Token: <b>{name_display}</b> (${symbol_display})\n"
        f"Mint: <code>{result.mint}</code>\n"
        f"Bonding Curve: <code>{result.bonding_curve[:16]}...</code>\n\n"
        f"📊 <b>Stats:</b>\n"
        f"  Total buys: <b>{result.total_buys}</b>\n"
        f"  Unique buyers: <b>{result.unique_buyers}</b>\n"
        f"  CEX funded: <b>{len(result.funding)}</b>\n"
        f"  No CEX trace: <b>{len(result.unfunded)}</b>\n"
    )

    # ─── Top buyers ──────────────────────────────────────
    # Aggregate by wallet
    wallet_totals: dict[str, float] = {}
    wallet_first_buy: dict[str, int] = {}
    for buy in result.buys:
        wallet_totals[buy.wallet] = wallet_totals.get(buy.wallet, 0) + buy.sol_spent
        if buy.wallet not in wallet_first_buy:
            wallet_first_buy[buy.wallet] = buy.buy_index

    sorted_wallets = sorted(
        wallet_totals.items(), key=lambda x: x[1], reverse=True
    )

    top_lines = ["\n🏆 <b>Top Buyers:</b>"]
    for i, (wallet, total_sol) in enumerate(sorted_wallets[:10], 1):
        buy_idx = wallet_first_buy.get(wallet, 0)
        # Check if this wallet has CEX funding
        cex_tag = ""
        for f in result.funding:
            if f.wallet == wallet:
                cex_tag = f" ← {f.cex_name}"
                break
        top_lines.append(
            f"  {i}. <code>{wallet[:8]}...</code> "
            f"<b>{total_sol:.3f}</b> SOL "
            f"(buy #{buy_idx}){cex_tag}"
        )
    if len(sorted_wallets) > 10:
        top_lines.append(f"  <i>... +{len(sorted_wallets) - 10} more</i>")

    header += "\n".join(top_lines)

    # ─── CEX breakdown ───────────────────────────────────
    if result.funding:
        cex_counts: dict[str, int] = {}
        cex_total_sol: dict[str, float] = {}
        for f in result.funding:
            cex_counts[f.cex_name] = cex_counts.get(f.cex_name, 0) + 1
            cex_total_sol[f.cex_name] = (
                cex_total_sol.get(f.cex_name, 0) + f.funding_amount
            )

        cex_lines = ["\n\n🏦 <b>CEX Funding Breakdown:</b>"]
        for cex, count in sorted(
            cex_counts.items(), key=lambda x: x[1], reverse=True
        ):
            total = cex_total_sol[cex]
            cex_lines.append(
                f"  {cex}: <b>{count}</b> wallets, "
                f"<b>{total:.2f}</b> SOL total"
            )
        header += "\n".join(cex_lines)

    chunks.append(header)

    # ─── Clusters ────────────────────────────────────────
    if result.clusters:
        cluster_text = "🚨 <b>Clusters Detected</b>\n"

        for i, cluster in enumerate(result.clusters, 1):
            label = CLUSTER_LABELS.get(cluster.cluster_type, cluster.cluster_type)
            c_header = f"\n<b>{i}. {label}</b>"
            if cluster.cex_name:
                c_header += f" ({cluster.cex_name})"
            if cluster.amount_pattern:
                c_header += f" — {cluster.amount_pattern}"
            c_header += f"\n  {cluster.notes}\n"

            wallet_lines = []
            for w in cluster.wallets[:8]:
                # Find this wallet's buy info
                buy_info = ""
                for buy in result.buys:
                    if buy.wallet == w:
                        buy_info = f" | {buy.sol_spent:.3f} SOL (#{buy.buy_index})"
                        break
                wallet_lines.append(
                    f"  • <code>{w[:12]}...</code>{buy_info}"
                )
            if len(cluster.wallets) > 8:
                wallet_lines.append(
                    f"  <i>... +{len(cluster.wallets) - 8} more</i>"
                )

            section = c_header + "\n".join(wallet_lines) + "\n"

            # Check if adding this section would exceed chunk limit
            if len(cluster_text) + len(section) > 3800:
                chunks.append(cluster_text)
                cluster_text = f"🚨 <b>Clusters (cont.)</b>\n{section}"
            else:
                cluster_text += section

        if cluster_text.strip():
            chunks.append(cluster_text)
    else:
        chunks.append("✅ <b>No suspicious clusters detected</b>")

    return chunks
