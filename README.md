# Aegis Engine (Binance USDT-M)

Trading engine cho Binance Futures USDT-M, thiết kế để chạy `demo` trước rồi mới sang `live`.

## Kiến trúc

- `strategy`: xác định tín hiệu `long/short/flat` từ khung `1h` (bias) + `15m` (entry)
- `range_trading`: phát hiện thị trường đi ngang và đánh mean-reversion ở biên range
- `risk`: sizing theo `%risk` hoặc margin, sinh `SL/TP`
- `execution`: đặt lệnh entry + bracket TP/SL, có spread/slippage guard
- `core/bot.py`: vòng lặp scheduler + daily drawdown kill-switch

## Cảnh báo bảo mật

Không lưu API key trong YAML.
Project này đọc key từ biến môi trường.

## Setup

```bash
cd /Users/bowthoois/Documents/GitHub/Binance
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Điền key vào `.env`:

```bash
BINANCE_DEMO_API_KEY=...
BINANCE_DEMO_API_SECRET=...
BINANCE_LIVE_API_KEY=...
BINANCE_LIVE_API_SECRET=...
```

## Chạy engine

Chạy 1 vòng để test:

```bash
aegis-engine --config configs/config.yaml --mode demo --once
```

Chạy với coin chỉ định trực tiếp trên câu lệnh (không cần sửa config):

```bash
aegis-engine --config configs/config.yaml --mode demo --symbols BTCUSDT --once
aegis-engine --config configs/config.yaml --mode demo --symbols BTCUSDT,ETHUSDT
```

Chạy demo account nhưng dùng data live để quyết định lệnh:

```bash
aegis-engine --config configs/config.yaml --mode demo --market-data-source live --symbols BTCUSDT,ETHUSDT,SOLUSDT
```

Chạy liên tục trên demo:

```bash
aegis-engine --config configs/config.yaml --mode demo
```

Chạy live (bắt buộc có cờ xác nhận):

```bash
aegis-engine --config configs/config.yaml --mode live --allow-live
```

## File chính

- Config: `configs/config.yaml`
- Log: `logs/live.log`
- Trade KPI CSV: `logs/trades.csv`

## Mặc định risk hiện tại

- Leverage: `5x`
- Risk per trade: `0.25%`
- SL/TP: `0.6% / 1.0%`
- Max concurrent positions: `1`
- Daily DD stop: `2%`
- Consecutive loss stop: `4`

## Ưu tiên setup mặc định

- Priority: `retest -> pullback -> breakout`
- Size multiplier:
- `retest`: `1.0x`
- `pullback`: `0.7x`
- `breakout`: `0.4x`
- `range_revert`: `0.6x`
- Entry mode:
- `retest`: `maker`
- `pullback`: `maker`
- `breakout`: `market`
- `range_revert`: `maker`
- Maker timeout: `10s`, partial fill min ratio: `70%`

## Range Trading

- `range_trading.enabled: true` bật chế độ mean-reversion khi trend yếu.
- Regime range cần `ADX <= range_trading.max_adx`, EMA nhanh/chậm không tách quá xa, range đủ rộng theo `bps` và theo `ATR`.
- Bot xác nhận range bằng số lần chạm hỗ trợ/kháng cự trong `range_trading.lookback`.
- Long chỉ xét gần đáy range với RSI thấp và nến rejection; short chỉ xét gần đỉnh range với RSI cao và nến rejection.
- Setup range dùng `entry_type=range_revert`, vào lệnh maker, size mặc định nhỏ hơn trend.
- TP/SL cho range là động: TP đặt trước mid-range, SL đặt ngoài biên range cộng buffer theo ATR. Risk sizing dùng đúng khoảng cách SL động thay vì luôn dùng `risk.sl_pct`.

## Bộ lọc ưu tiên winrate

- `filters.min_confluence_score: 4`
- `filters.score_gap_min: 2` (điểm phe thuận phải hơn phe ngược tối thiểu 2)
- `filters.opponent_score_max: 2` (phe ngược không được quá mạnh)
- `safety.min_atr_pct: 0.10`, `safety.max_atr_pct: 1.20` (lọc thị trường quá lặng hoặc quá nhiễu)
- `entry.max_dist_ema_bps: 35` (entry không được quá xa EMA nhanh)

## Market Context Filters

- `market_context.enabled: true`
- `market_context.require_data: true` (thiếu dữ liệu OI/Funding/Flow thì bỏ kèo)
- `market_context.market_data_source: execution|live|demo` (tách nguồn data khỏi tài khoản đặt lệnh)
- `market_context.min_oi_change_pct: 0.02` (chỉ vào khi OI tăng đủ mức)
- `market_context.funding_abs_max: 0.0005` (tránh funding quá nóng)
- `market_context.basis_abs_max: 0.0015` (tránh basis premium/discount quá lệch)
- `market_context.taker_ratio_long_min: 1.01`, `market_context.taker_ratio_short_max: 0.99` (lọc theo lực taker)
- `market_context.global_long_ratio_max: 1.8`, `market_context.global_short_ratio_min: 0.55` (lọc crowding toàn thị trường)
- `market_context.top_trader_long_ratio_max: 1.6`, `market_context.top_trader_short_ratio_min: 0.65` (lọc crowding nhóm top trader)
- `market_context.depth_*` lọc imbalance orderbook (ưu tiên depth stream, fallback REST nếu cần)
- `market_context.liquidation_*` chặn lệnh khi có liquidation shock trong cửa sổ ngắn
- `market_context.adl_block_levels` chặn giao dịch khi ADL risk ở mức không an toàn
- `market_context.ws_*` bật hạ tầng WS route mới (`/public` cho depth, `/market` cho forceOrder)

## Hold Management

- `hold_management.soft_timeout_bars` timeout mềm theo setup (`retest/pullback/breakout`)
- `hold_management.hard_timeout_bars` timeout cứng theo setup (đóng bắt buộc)
- Khi tới timeout mềm:
- Nếu `R` hiện tại đủ tốt + trend còn khỏe, bot cho gia hạn giữ lệnh
- Nếu kèo yếu, bot đóng market với reason `timeout_soft_weak`
- Nếu tới timeout cứng, bot đóng bắt buộc với reason `timeout_hard`
- Khi gia hạn thành công, bot có thể dời SL về `BE + be_buffer_bps` để bảo toàn vốn
- `execution.exit_order` được áp dụng cho các lệnh đóng chủ động (timeout/force close): `maker` hoặc `taker`

## Candle Filter (OHLCV)

- `entry.candle_filter.*` dùng trực tiếp `open/high/low/close/volume` để lọc chất lượng nến vào lệnh.
- Lọc thân nến tối thiểu theo `bps` và theo `ATR`.
- Lọc tổng wick và wick ngược hướng để giảm nến nhiễu.
- Breakout bắt buộc nến thân cùng hướng (`bullish` cho long, `bearish` cho short).
- Pullback/retest có thể yêu cầu wick rejection tối thiểu.

## KPI Theo Setup

Engine tự ghi từng trade đóng vào `logs/trades.csv` và log KPI theo cửa sổ `kpi_window_trades`:

- Winrate tổng và theo setup
- Expectancy theo `R`
- Net PnL tổng theo setup
- KPI net đã trừ phí (`analytics.fee_maker/fee_taker`) và cộng/trừ funding khi bật `analytics.funding_apply`
- Active trade state được persist tại `analytics.active_state_json` để recover sau restart

## Runtime Safety

- `risk.consecutive_loss_stop` đã được thực thi trong runtime (chặn lệnh mới khi chạm chuỗi thua).
- Startup reconciliation: nếu bot restart, engine sẽ reconcile trạng thái vị thế sàn với state nội bộ KPI.
- `scheduler.align_to_candle_close: true` chạy signal chính gần thời điểm đóng nến, nhưng vẫn giữ vòng maintenance.
- `logging.runtime_heartbeat_sec` log heartbeat định kỳ ra terminal: equity, drawdown, loss streak, số vị thế mở, số trade đang track.

## Auto Repair TP/SL

Khi bot phát hiện symbol đang có vị thế nhưng thiếu TP hoặc SL trên sàn, engine sẽ tự đặt lại phần còn thiếu theo `risk.sl_pct/tp_pct`.
Tùy chỉnh bằng `safety.auto_repair_brackets` và `safety.repair_brackets_cooldown_sec` trong config.
