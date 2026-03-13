# Quy trình dò Dev Wallet — Pump.fun Token

## Input
- Mint address (token contract)

## Bước 1: Lấy danh sách giao dịch đầu tiên của token

```
getSignaturesForAddress(mint, limit=1000)
→ Sort theo blockTime ASC
→ Lấy 50 tx đầu tiên (earliest)
```

**Mục đích:** Tìm tất cả ví mua dưới bonding curve (trước khi migrate Raydium)

## Bước 2: Parse early transactions → Tìm early buyers

```
Helius Enhanced API: POST /v0/transactions/
→ Parse tokenTransfers where mint = target token
→ Với mỗi transfer:
   - toUserAccount = buyer (nhận token)
   - fromUserAccount = seller (bán token)
   - Bỏ qua bonding curve address (WLHv2... hoặc pool address)
```

**Output:** Danh sách ví + số lượng token mua, thời gian mua

**Red flag:** Nhiều ví mua cùng 1 giây = bundled transaction = snipe có tổ chức

## Bước 3: Trace funding source cho mỗi early buyer

```
Với mỗi ví buyer:
  GET /v0/addresses/{wallet}/transactions?limit=50
  → Sort theo time ASC (cũ nhất trước)
  → Tìm giao dịch SOL đầu tiên IN (nativeTransfers where toUserAccount = wallet, amount > 0.1 SOL)
  → Đó là FUNDER
```

**Output:** Map: buyer → funder address

## Bước 4: Trace profit destination cho mỗi early buyer

```
Với mỗi ví buyer:
  → Tìm giao dịch SOL OUT lớn nhất (nativeTransfers where fromUserAccount = wallet, amount > 0.1 SOL)
  → Đó là COLLECTOR (ví gom profit)
```

**Output:** Map: buyer → collector address

## Bước 5: Cluster bằng shared connections

```
Group theo FUNDER:
  funder_clusters = {}
  for buyer, funder in funding_map:
      funder_clusters[funder].append(buyer)
  → Funder nào fund >= 2 buyers = 1 cluster

Group theo COLLECTOR:
  collector_clusters = {}
  for buyer, collector in profit_map:
      collector_clusters[collector].append(buyer)
  → Collector nào nhận từ >= 2 buyers = 1 cluster

Merge clusters:
  Nếu wallet A và wallet B cùng funder HOẶC cùng collector → cùng cluster
  Nếu funder của cluster X = collector của cluster Y → merge X+Y
```

## Bước 6: Phân loại Dev vs Hunter

```
Với mỗi cluster:
  - Check current dKOL balance (getTokenAccountsByOwner)
  - Check current SOL balance (getBalance)
  - Check tổng số giao dịch (activity level)
  - Check wallet age (first tx → last tx)
  - Check số token khác đã trade

DEV indicators:
  ✓ Funded bởi cùng 1 master funder
  ✓ Profit gom về cùng 1 collector
  ✓ Ví chỉ sống vài ngày rồi chết
  ✓ Balance = 0 (bán sạch)
  ✓ Chỉ trade 1-2 token
  ✓ Mua cùng giây với token creation

HUNTER indicators:
  ✓ Funded từ nguồn khác nhau (CEX, random wallets)
  ✓ Profit đi về nhiều hướng khác nhau
  ✓ Ví sống lâu, trade nhiều token
  ✓ Có thể còn holding
```

## Bước 7: Tính exposure

```
Với mỗi cluster:
  total_dkol = sum(mỗi ví trong cluster)
  pct_supply = total_dkol / total_supply * 100
  status = HOLDING / PARTIALLY_SOLD / FULLY_SOLD
```

---

## API Calls cần thiết

| Step | API | Endpoint | Rate |
|------|-----|----------|------|
| 1 | Helius RPC | getSignaturesForAddress | 1 call |
| 2 | Helius Enhanced | POST /v0/transactions/ | N/10 calls (batch 10) |
| 3-4 | Helius Enhanced | GET /v0/addresses/{w}/transactions | N calls (1 per wallet) |
| 5 | Logic only | — | — |
| 6 | Helius RPC | getTokenAccountsByOwner + getBalance | 2N calls |

**Tổng cho 30 wallets:** ~40-50 API calls

## Data Flow

```
mint address
    │
    ▼
[1] getSignaturesForAddress → oldest 50 sigs
    │
    ▼
[2] parse tokenTransfers → 30 early buyers
    │
    ▼
[3-4] trace each buyer:
    ├── funding source (first SOL in)
    └── profit destination (SOL out)
    │
    ▼
[5] cluster by shared funder/collector
    │
    ▼
[6] classify dev vs hunter
    │
    ▼
[7] output: clusters + % supply + status
```

## Edge Cases

1. **Ví không có SOL in trực tiếp** → Có thể được fund qua token transfer hoặc có SOL từ trước
2. **Bundled transactions** → Nhiều ví mua trong cùng 1 tx signature = chắc chắn cùng operator
3. **Intermediate wallets** → Funder → intermediate → buyer. Cần trace 2 hop
4. **CEX withdrawals** → Nếu funder là CEX hot wallet (Binance, MEXC...) thì không cluster được qua funder, phải dùng collector
5. **Bot wallets (MEV)** → Pattern: fund/withdraw loop liên tục với cùng 1 hub address
6. **Ví tái sử dụng** → Cùng 1 ví snipe nhiều token khác nhau → track cross-token activity
