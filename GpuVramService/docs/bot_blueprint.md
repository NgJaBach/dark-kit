# GPU VRAM Sentinel — Bot Blueprint

## Purpose

Pre-reserve GPU VRAM on shared servers to prevent lightweight jobs from claiming memory that belongs to heavy model workloads. Alerts when VRAM runs low so the operator can react or bloat proactively.

## Identity

**VRAM Garrison Commander** — a soldier-disciplined warden bound to Bach the Monarch.
Formal, precise, no humor. Speaks in terms of territory, occupation, and garrison.

## Architecture

Single Python file (`gpu_vram_bot.py`). Two background threads:

| Thread | Role |
|--------|------|
| `telegram_poll_loop` | Long-polls Telegram for messages and callback queries. Runs all CUDA operations. |
| `gpu_monitor_loop` | Polls `nvidia-smi` every N seconds. Fires low-VRAM alerts. No CUDA. |

## VRAM Bloat Mechanism

Uses the **CUDA Driver API** (`nvcuda.dll` / `libcuda.so.1`) directly via `ctypes` — no PyTorch or CUDA toolkit required, only the GPU driver.

**Flow for `bloat_gpu(gpu_idx, target_pct)`:**
1. Query current VRAM usage via `nvidia-smi`.
2. Calculate `to_alloc = target_pct% of total − current_used − 300MB_margin`.
3. Create a CUDA context on the target device via `cuCtxCreate_v2`.
4. Allocate one large block via `cuMemAlloc_v2`. Steps down 256MB on OOM until ≥128MB succeeds.
5. Pop the context off the thread stack — memory remains resident while the context lives.
6. Store `BloatSession(ctx, ptr, allocated_mb)` in `_sessions[gpu_idx]`.

**Release (`release_gpu` / `release_all`):**
1. Push context current (`cuCtxPushCurrent_v2`).
2. Free allocation (`cuMemFree_v2`).
3. Pop context, then destroy it (`cuCtxDestroy_v2`).

All CUDA ops happen on the Telegram poll thread to avoid cross-thread context issues.

## Commands

| Command | Description |
|---------|-------------|
| `@bot status` | VRAM snapshot for all GPUs with utilization and temp |
| `@bot bloat` | Interactive inline keyboard: select GPU(s), then occupation % |
| `@bot release` | Interactive inline keyboard: release specific GPU or all |
| `@bot arise` | Subscribe chat to automatic low-VRAM alerts |
| `@bot dismiss` | Unsubscribe chat |
| `@bot help` | Command registry |

## Bloat Levels

20% · 50% · 70% · 90% of total VRAM.

## Inline Keyboard Protocol (callback_data)

| Data | Action |
|------|--------|
| `G:{gpu_spec}` | GPU selection (step 1 for multi-GPU) |
| `P:{pct}:{gpu_spec}` | Execute bloat at pct% on gpu_spec |
| `R:{gpu_spec}` | Release gpu_spec ("all" for all GPUs) |
| `X` | Cancel |

`gpu_spec` is either a GPU index (`"0"`, `"1"`, …) or `"all"`.

## Automatic Alerts

- Fires when `free_pct < VRAM_LOW_THRESHOLD_PCT` (default 10%) on any GPU.
- Alert suppressed for GPUs currently under active bloat (expected to be full).
- Per-GPU cooldown: 600 seconds between alerts on the same GPU.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | — | **Required.** Different token from the OpenAI bot. |
| `TELEGRAM_CHAT_ID` | — | **Required.** Same council chat as OpenAI bot. |
| `TELEGRAM_THREAD_ID` | (empty) | Optional topic thread. |
| `GPU_POLL_INTERVAL_SECS` | 60 | Seconds between GPU stat polls. |
| `VRAM_LOW_THRESHOLD_PCT` | 10 | Free VRAM % below which alert fires. |

## WDDM vs Linux / TCC Note

On **Windows (WDDM)** — which is the typical desktop driver mode — CUDA memory allocated with `cuMemAlloc` may not be fully physically resident in VRAM as shown by `nvidia-smi`. Windows WDDM allows the OS to page GPU memory, so the reported VRAM increase may be smaller than the allocation. This means pre-reservation effectiveness is limited on Windows.

On **Linux** (or Windows with **TCC mode**, typical of datacenter/server GPUs) CUDA allocations ARE immediately committed to physical VRAM and `nvidia-smi` will reflect the full reservation. **This is the intended deployment target.**

The `cuMemsetD8_v2` call after each allocation writes to every byte to maximize physical residency even under WDDM.

## Dependencies

```
python-dotenv
requests
```

No CUDA toolkit, no PyTorch. Only requires the NVIDIA GPU driver.

## Running

```bash
python gpu_vram_bot.py
```

State (`bot_data/subscribers.json`) persists subscriptions across restarts.
Bloat sessions are in-memory only — VRAM is freed automatically when the process exits.
