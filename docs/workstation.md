
---

## 4. Blackwell (`sm_120`) Longevity & `NVFP4` Weight Maturity

Choosing Blackwell (`sm_120`) early for hardware longevity aligned directly with the maturation of native **4-bit floating-point (`NVFP4`)** model weights and sparse Mixture-of-Experts kernels:

* **Direct Hardware `NVFP4` Execution:** Instead of paying dequantization overhead back to FP16, the dual `GB206` tensor cores execute `NVFP4` natively—fitting `180B`-class sparse models cleanly inside `32 GB` of GDDR7 alongside the `65,536`-token KV cache.
* **GDDR7 Bandwidth Meets Sparse Active Weights:** With only `6B` active parameters per token and Multi-Token Prediction (`11/12` draft acceptance), GDDR7 memory throughput sustains `54–74.7 tok/s` without thermal or memory bottlenecks.

---

## 4. Why CachyOS (`7.3.5-2-cachyos`) Powers the Control Plane

* **Tuned Virtual Memory & MoE Streaming:** CachyOS's `sysctl` dirty-ratio tuning, `zram` memory compression, and `madvise` huge-page defaults keep `94 GiB` of DDR4 host RAM responsive while streaming cold Mixture-of-Experts weights across the dual-`x8` PCIe bus.
* **Unified Multi-Vendor GPU Support:** Prebuilt `NVIDIA 620.73.04` modules, `Mesa 26.3.1`, and `cachyos-drm-awaiter` coordinate headless Blackwell (`GB206`) CUDA initialization alongside Intel Arc (`DG2`) Wayland compositing cleanly on every boot.
* **Btrfs Snapshot Hygiene:** Copy-on-write system snapshots provide zero-downtime rollback protection across bleeding-edge kernel and inference engine updates.
