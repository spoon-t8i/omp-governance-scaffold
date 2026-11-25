
---

## 4. Blackwell (`sm_120`) Longevity & `NVFP4` Weight Maturity

Choosing Blackwell (`sm_120`) early for hardware longevity aligned directly with the maturation of native **4-bit floating-point (`NVFP4`)** model weights and sparse Mixture-of-Experts kernels:

* **Direct Hardware `NVFP4` Execution:** Instead of paying dequantization overhead back to FP16, the dual `GB206` tensor cores execute `NVFP4` natively—fitting `180B`-class sparse models cleanly inside `32 GB` of GDDR7 alongside the `65,536`-token KV cache.
* **GDDR7 Bandwidth Meets Sparse Active Weights:** With only `6B` active parameters per token and Multi-Token Prediction (`11/12` draft acceptance), GDDR7 memory throughput sustains `54–74.7 tok/s` without thermal or memory bottlenecks.
