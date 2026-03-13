# Degen Scanner — Detection Conditions

## 1. Cluster Detection (ClusterEngine)

**Source:** Group "box 1-12" — CEX withdrawals 1-12 SOL

| Condition | Value | Note |
|-----------|-------|------|
| Time window | `15 min` | Sliding window, transfers cũ hơn → tự xóa |
| Min cluster size | `2 wallets` | Tối thiểu 2 ví từ cùng CEX mới trigger alert |
| Max amount spread | `1.0 SOL` | Cho INCREASING/RAPID_FIRE, loại outlier |
| CEX grouping | Per-CEX riêng biệt | Mỗi sàn scan độc lập, không trộn |
| Duplicate wallet | Skip | Cùng wallet + cùng CEX trong 1 window → bỏ qua |
| Already alerted | Skip | Wallet đã alert → không alert lại trong cùng window |

---

### Amount Patterns (Chi tiết)

**Thứ tự ưu tiên:** SAME → INCREASING → SIMILAR → RAPID_FIRE
Check lần lượt từ trên xuống, dừng ngay khi match.

---

#### 🎯 SAME — Tất cả cùng số tiền

**Điều kiện:** Mọi amount chênh lệch < `0.001 SOL` so với amount đầu tiên.

**Logic:**
```
amounts = [1.00, 1.00, 1.00, 1.00]
→ |1.00 - 1.00| = 0.000 < 0.001 ✅ → SAME
```

**Ví dụ MATCH:**
```
08:50  Wallet_A ← MEXC   1.00 SOL
08:51  Wallet_B ← MEXC   1.00 SOL
08:53  Wallet_C ← MEXC   1.00 SOL
08:55  Wallet_D ← MEXC   1.00 SOL
→ 4 ví × 1.00 SOL → 🎯 SAME
```

**Ví dụ KHÔNG MATCH:**
```
08:50  Wallet_A ← MEXC   1.00 SOL
08:51  Wallet_B ← MEXC   1.02 SOL   ← chênh 0.02 > 0.001
→ Không phải SAME, check tiếp INCREASING
```

---

#### 📈 INCREASING — Số tiền tăng dần theo thời gian

**Điều kiện:** Sắp xếp theo timestamp, mọi `amount[i] < amount[i+1]` (strict tăng).

**Logic:**
```
Sắp xếp theo thời gian:
  08:50 → 0.20 SOL
  08:52 → 0.30 SOL
  08:53 → 0.50 SOL
  08:55 → 0.80 SOL

Check: 0.20 < 0.30 ✅ → 0.30 < 0.50 ✅ → 0.50 < 0.80 ✅ → INCREASING
```

**Ví dụ MATCH:**
```
08:50  Wallet_A ← OKX   0.20 SOL
08:52  Wallet_B ← OKX   0.30 SOL
08:53  Wallet_C ← OKX   0.50 SOL
08:55  Wallet_D ← OKX   0.80 SOL
→ Tăng dần: 0.20 → 0.30 → 0.50 → 0.80 → 📈 INCREASING
```

**Ví dụ KHÔNG MATCH:**
```
08:50  Wallet_A ← OKX   0.20 SOL
08:52  Wallet_B ← OKX   0.50 SOL
08:53  Wallet_C ← OKX   0.30 SOL   ← 0.50 > 0.30, không tăng
→ Không phải INCREASING, check tiếp SIMILAR
```

**Lưu ý:** Bỏ qua transfers có amount = 0. Phải strict tăng (không bằng).

---

#### 🔄 SIMILAR — Số tiền gần giống nhau

**Điều kiện:** Mọi amount nằm trong **±10%** của trung bình (average).

**Logic:**
```
amounts = [2.10, 2.20, 2.15, 2.05]
average = (2.10 + 2.20 + 2.15 + 2.05) / 4 = 2.125
threshold = 2.125 × 10% = 0.2125

Check mỗi amount:
  |2.10 - 2.125| = 0.025 ≤ 0.2125 ✅
  |2.20 - 2.125| = 0.075 ≤ 0.2125 ✅
  |2.15 - 2.125| = 0.025 ≤ 0.2125 ✅
  |2.05 - 2.125| = 0.075 ≤ 0.2125 ✅
→ Tất cả trong ±10% → 🔄 SIMILAR
```

**Ví dụ MATCH:**
```
08:50  Wallet_A ← Bybit   2.10 SOL
08:51  Wallet_B ← Bybit   2.20 SOL
08:53  Wallet_C ← Bybit   2.15 SOL
08:54  Wallet_D ← Bybit   2.05 SOL
→ Average = 2.125, tất cả trong ±0.2125 → 🔄 SIMILAR
```

**Ví dụ KHÔNG MATCH:**
```
08:50  Wallet_A ← Bybit   1.00 SOL
08:51  Wallet_B ← Bybit   2.00 SOL   ← chênh quá 10% avg
→ Không phải SIMILAR, check tiếp RAPID_FIRE
```

---

#### ⚡ RAPID_FIRE — Rút nhanh liên tiếp (bất kỳ số tiền)

**Điều kiện:** Default pattern — chỉ cần ≥ `min_cluster_size` ví từ cùng CEX trong time window.

Không yêu cầu gì về amount. Đây là fallback khi không match SAME, INCREASING, hoặc SIMILAR.

**Ví dụ:**
```
08:50  Wallet_A ← Gate   0.50 SOL
08:51  Wallet_B ← Gate   3.00 SOL
08:52  Wallet_C ← Gate   1.20 SOL
→ 3 ví trong 2 phút, amounts khác nhau → ⚡ RAPID_FIRE
```

---

### Amount Spread Filter (cho INCREASING & RAPID_FIRE)

**Vấn đề:** Có outlier lớn trong cluster.
```
08:50  Wallet_A ← OKX   1.40 SOL
08:51  Wallet_B ← OKX   1.50 SOL
08:52  Wallet_C ← OKX   1.60 SOL
08:53  Wallet_D ← OKX  12.00 SOL   ← outlier
```

**Giải pháp:** Sliding window trên sorted amounts, tìm nhóm lớn nhất có `max - min ≤ 1.0 SOL`:

```
Sorted: [1.40, 1.50, 1.60, 12.00]

Window [1.40, 1.50, 1.60]: spread = 0.20 ≤ 1.0 ✅ → size 3
Window [1.50, 1.60, 12.00]: spread = 10.50 > 1.0 ❌

Best group: [1.40, 1.50, 1.60] → 3 ví
→ Re-check pattern trong group này → 📈 INCREASING (1.40 < 1.50 < 1.60)
```

**Lưu ý:** SAME và SIMILAR tự kiểm spread chặt hơn (giống nhau / ±10%), nên không cần filter thêm.

---

### Detection Strategy

- Detect mỗi **7.5 phút** (= TIME_WINDOW / 2) trong quá trình add transfers
- Loop `detect_clusters()` cho đến khi hết (bắt nhiều cluster/CEX trong 1 lần)
- Detect cả giữa chừng + cuối scan để tránh cleanup xóa data trước khi detect

---

## 2. Custom Pattern Rules (CustomPatternEngine)

**Source:** Cùng group "box 1-12", nhưng fetch window mở rộng theo rule lớn nhất

| Condition | Range | Note |
|-----------|-------|------|
| Time window | `1-60 min` | Mỗi rule có window riêng |
| Min wallets | `2-50` | Mỗi rule tự set |
| Amount range | `amount_min` - `amount_max` | Optional, filter trước khi vào window |
| CEX filter | Keyword (case-insensitive) hoặc `*` = tất cả | Filter trước khi vào window |
| Max interval | Khoảng cách max giữa 2 ví liên tiếp (seconds) | Optional |
| Tolerance % | Cho `exact_amount`: ±X% so với median | Optional |

---

### Custom Pattern Types (Chi tiết)

#### 📈 increasing — Tăng strict

**Giống ClusterEngine INCREASING** nhưng thêm các filter:
- Chỉ count transfers match CEX keyword + amount range
- Sắp xếp theo timestamp
- Mọi `amount[i] < amount[i+1]`

```
Rule: MEXC, 0.5-3.0 SOL, increasing, 15 min, min 3 ví

08:50  MEXC  0.50 SOL  ✅ (match CEX + amount range)
08:52  MEXC  1.20 SOL  ✅
08:53  OKX   0.80 SOL  ❌ (wrong CEX)
08:55  MEXC  1.85 SOL  ✅
08:57  MEXC  2.50 SOL  ✅

Candidates: [0.50, 1.20, 1.85, 2.50]
Check: 0.50 < 1.20 < 1.85 < 2.50 ✅ → MATCH! 4 ví
```

---

#### 📉 decreasing — Giảm strict

**Ngược lại increasing:** mọi `amount[i] > amount[i+1]`

```
Rule: Bybit, decreasing, 10 min, min 3 ví

08:50  Bybit  3.00 SOL
08:52  Bybit  2.10 SOL
08:54  Bybit  1.50 SOL
08:55  Bybit  0.80 SOL

Check: 3.00 > 2.10 > 1.50 > 0.80 ✅ → MATCH! 4 ví
```

**KHÔNG match nếu:**
```
3.00 → 2.10 → 2.50 → 0.80
         2.10 < 2.50 ❌ → không giảm strict
```

---

#### 🎯 exact_amount — Cùng số tiền (có tolerance)

**Điều kiện:**
1. Tính **median** của tất cả amounts
2. Mọi amount phải trong **±tolerance_pct%** của median

```
Rule: *, exact_amount, tolerance 5%, min 4 ví

Amounts: [0.95, 0.97, 0.93, 0.96]
Median: 0.955  (sorted middle values)
Tolerance: 0.955 × 5% = 0.04775

Check:
  |0.95 - 0.955| = 0.005 ≤ 0.04775 ✅
  |0.97 - 0.955| = 0.015 ≤ 0.04775 ✅
  |0.93 - 0.955| = 0.025 ≤ 0.04775 ✅
  |0.96 - 0.955| = 0.005 ≤ 0.04775 ✅
→ MATCH! 4 ví × ~0.95 SOL
```

**So với Cluster SAME:** SAME yêu cầu giống hệt (±0.001), exact_amount cho phép tolerance % tùy chỉnh.

---

#### ⚡ rapid_fire — Chỉ cần đủ ví

**Điều kiện:** Chỉ cần ≥ `min_wallets` transfers match CEX + amount range trong window. Không check pattern amount.

```
Rule: Gate, 1.0-5.0 SOL, rapid_fire, 10 min, min 5 ví

Bất kỳ 5 ví nào rút 1-5 SOL từ Gate trong 10 phút → MATCH!
```

---

### Max Interval Chain (Chi tiết)

Khi `max_interval_seconds` được set, engine tìm **chuỗi liên tiếp dài nhất** mà khoảng cách giữa 2 ví kề nhau < limit.

**Ví dụ chi tiết:**
```
Rule: max_interval = 60s

Transfers (sorted by time):
  T+0s     Wallet_A  1.00 SOL
  T+30s    Wallet_B  1.20 SOL     gap = 30s  < 60s ✅
  T+45s    Wallet_C  1.50 SOL     gap = 15s  < 60s ✅
  T+200s   Wallet_D  1.80 SOL     gap = 155s > 60s ❌ → CẮT
  T+230s   Wallet_E  2.00 SOL     gap = 30s  < 60s ✅

Chains tìm được:
  Chain 1: [A, B, C] → 3 ví (T+0 → T+45)
  Chain 2: [D, E]    → 2 ví (T+200 → T+230)

Dùng chain dài nhất: Chain 1 (3 ví)
→ Check pattern trên [1.00, 1.20, 1.50] → increasing ✅ → MATCH!
```

**Nếu không set max_interval:** Dùng toàn bộ transfers trong window, không lọc gap.

---

### Wider Fetch Window

```
SCAN_INTERVAL = 30 min (default)
Custom rule time_window = 50 min

→ Bot fetch 50 phút message history (thay vì 30)
→ Cluster engine chỉ nhận transfers trong 30 phút gần nhất
→ Custom engine nhận toàn bộ 50 phút
→ Nếu không có custom rule → giữ nguyên 30 phút
```

### Dedup

- Key: `(rule_id, frozenset(wallet_addresses))`
- Cùng combo rule + wallets → không alert lại
- Persist suốt session (không reset theo window)

---

## 3. Whale Splitter (SplitterEngine)

**Source:** Group "box >12" — CEX withdrawals >12 SOL

| Condition | Value | Note |
|-----------|-------|------|
| Min split destinations | `2 wallets` | Ít nhất 2 ví đích khác nhau |
| Split time window | `5 min` | Các transfer chia tiền phải trong 5 phút kể từ transfer đầu |
| Min split amount | `0.15 SOL` | Mỗi sub-transfer phải ≥ 0.15 SOL |
| Valid sources | `SYSTEM_PROGRAM` only | Chỉ count SOL transfer trực tiếp |
| On-curve check | Wallet phải on Ed25519 curve | Bỏ PDA addresses (program accounts) |
| Helius rate limit | `150ms` giữa mỗi request | Tránh bị rate limit |
| Daily call limit | `950 calls/day` | Free tier 1000, buffer 50 |

### Tại sao chỉ SYSTEM_PROGRAM?

On-chain có nhiều loại SOL transfer:
- `SYSTEM_PROGRAM` → Transfer SOL trực tiếp giữa 2 ví → **Đây là split thật**
- `RAYDIUM`, `JUPITER`, `PUMP_FUN`, `PUMP_AMM` → DEX swap → **Không phải split**

Nếu count cả DEX swap sẽ false positive (whale mua token ≠ chia tiền).

### Tại sao check On-Curve?

- **On-curve** (Ed25519): Wallet thật của người dùng → ✅ count
- **Off-curve** (PDA): Program Derived Address, thuộc smart contract → ❌ bỏ

Ví dụ PDA: Raydium pool address, token vault, staking account... Không phải ví người nhận thật.

### Detection Flow (Chi tiết)

```
=== Scan N (08:00) ===
Đọc Group 2: whale rút 15 SOL từ Binance → Wallet_X
→ Queue Wallet_X vào current_batch

=== End of Scan N ===
Rotate: current_batch → ready_batch
(Cho Wallet_X thời gian để chia tiền)

=== Scan N+1 (08:30) ===
1. Check ready_batch:
   → Query Helius: Wallet_X có outgoing transfers không?
   → Helius trả về:
     08:05  Wallet_X → Wallet_A  3.0 SOL  (SYSTEM_PROGRAM) ✅
     08:05  Wallet_X → Wallet_B  3.0 SOL  (SYSTEM_PROGRAM) ✅
     08:06  Wallet_X → Wallet_C  3.0 SOL  (SYSTEM_PROGRAM) ✅
     08:10  Wallet_X → Raydium   5.0 SOL  (RAYDIUM_AMM)    ❌ swap, bỏ

   Unique destinations: {A, B, C} = 3 ≥ 2 → 🐋 SPLIT DETECTED!

2. Collect whale mới từ scan này → queue cho scan tiếp
```

### Double Buffer

```
Scan 1: [Whale_A, Whale_B] → current_batch
         rotate → ready_batch = [Whale_A, Whale_B]

Scan 2: Check ready_batch [Whale_A, Whale_B] via Helius
         [Whale_C] → current_batch
         rotate → ready_batch = [Whale_C]

Scan 3: Check ready_batch [Whale_C] via Helius
         ...
```

Lý do double buffer: Whale rút tiền → cần **thời gian** để chia (thường vài phút đến ~30 phút). Nếu check ngay lập tức sẽ chưa có outgoing transfers.

---

## 4. Message Parsing (Parser)

| Condition | Value | Chi tiết |
|-----------|-------|----------|
| Transfer indicator | `💸` | Phải có trong dòng đầu |
| Type filter | `TRANSFER` only | Bỏ SWAP, BUY, SELL, BURN... |
| Amount regex | `transferred X SOL` | Hỗ trợ dấu `,` (e.g., 1,500.5 SOL) |
| CEX name regex | `🔹 CEX_NAME transferred` | Extract tên sàn + emoji |
| Wallet extraction | Solscan URLs trong entities | `solscan.io/account/ADDRESS` |
| Min wallets in msg | 2 | Phải có CEX wallet + destination wallet |
| Skip SOL token | Bỏ `So1111...` | Địa chỉ token SOL, không phải ví |
| CEX wallet | URL đầu tiên | Ví sàn (nguồn) |
| Dest wallet | URL cuối cùng ≠ CEX wallet | Ví người nhận (đích) |
| TX link | `solscan.io/tx/...` | Link giao dịch (optional) |

**Message mẫu từ Ray Onyx:**
```
💸 TRANSFER
🔹 Bybit 📙

🔹Bybit 📙 transferred 3 SOL to Fsd5...BzyA*
```

---

## 5. Scan Loop

| Setting | Value | Note |
|---------|-------|------|
| Auto-scan interval | `30 min` | Configurable: `SCAN_INTERVAL_MINUTES` |
| Fetch limit | `2000 msgs` per scan | Max messages per iter_messages call |
| Periodic detection | Every `7.5 min` of data | = TIME_WINDOW_MINUTES / 2 |
| First scan delay | `10 sec` | Chờ Telethon connect xong |
| Scan order | Cluster → Whale | Mỗi cycle chạy cả 2 |

---

## 6. Alert Dedup & Cleanup

### ClusterEngine
- Track: `(wallet_address, cex_key)`
- **Auto-expire** khi wallet rời khỏi time window (15 min)
- Wallet có thể alert lại ở window mới

### CustomPatternEngine
- Track: `(rule_id, frozenset(wallet_addresses))`
- **Không expire** — persist suốt session
- Cùng combo wallet set + rule → chỉ alert 1 lần suốt đời bot

### SplitterEngine
- Track: `wallet_address` trong `_checked` set
- **Không expire** — mỗi whale wallet chỉ check Helius 1 lần
- Tránh query trùng, tiết kiệm Helius credits

---

## Summary Table

| Engine | Source | Window | Min Size | Pattern | Key Filter |
|--------|--------|--------|----------|---------|------------|
| Cluster | box 1-12 | 15 min | 2 ví | SAME / INCREASING / SIMILAR / RAPID_FIRE | Amount spread ≤ 1.0 SOL |
| Custom | box 1-12 | 1-60 min | 2-50 ví | increasing / decreasing / exact_amount / rapid_fire | CEX keyword + amount range + max interval |
| Whale | box >12 | 5 min (split) | 2 destinations | — | SYSTEM_PROGRAM only + on-curve + ≥ 0.15 SOL |
