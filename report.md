# VPP Real-Time Scheduling — Level 1 報告

## 1. Periodic task set 產生方式

採用「先設計、後驗證」的策略：手動挑出 6 個 periodic task，使其同時滿足
評分標準 1-1 ~ 1-8 的所有結構性限制。task 內容如下（`task_generator.py`
中為固定常數，整個 pipeline 為 deterministic）：

| Task | r | period | e | d | w (MWh/h) | preempt |
|---|---|---|---|---|---|---|
| p1 | 1 | 6  | 3 | 3  | 12 | 1 |
| p2 | 2 | 6  | 2 | 6  | 10 | 1 |
| p3 | 1 | 12 | 3 | 3  | 14 | 0 |
| p4 | 3 | 12 | 2 | 12 | 16 | 0 |
| p5 | 1 | 24 | 2 | 12 | 8  | 1 |
| p6 | 2 | 24 | 3 | 24 | 18 | 1 |

驗證結果（由 `src/task_generator.py` 在輸出時自我檢查；不通過會直接中止）：

| 評分項 | 條件 | 本作業 |
|---|---|---|
| 1-2 | 6 ≤ \|Jp\| ≤ 10 | 6 ✓ |
| 1-3 | 展開後 > 30 jobs | 42 ✓ |
| 1-4 | r/period/e/w/deadline 範圍 + 多樣性 | 全部 ✓（periods = {6,12,24}，e=2×3、e≥3×3、w≥14×3） |
| 1-5 | D_W = Σ e_j/period_j ≥ 0.7 | 1.458 ✓ |
| 1-6 | ≥ 20% 任務 d = e | p1、p3（33%）✓ |
| 1-7 | ≥ 2 個 non-preempt 且 e ≠ 1 | p3、p4 ✓ |
| 1-8 | f ≥ max(e_j)、72 mod f = 0、2f − gcd(f,p_j) ≤ d_j | f = 3 ✓（最小合法） |

Frame size 自動搜尋（`select_frame_size`）會嘗試 max(e) 到 H 之間每個能整除
72 的整數，取首個滿足全部三條件的最小值。對本 task set 為 **f = 3**。

展開後 42 個 periodic jobs（細節請見 `output/task_set.json` 的
`expanded_jobs` 陣列）。

---

## 2. 排程演算法設計說明

### 2.1 Phase 1：Periodic placement（clock-driven static schedule）

採用 **EDF on absolute deadline**。先把 42 個 jobs 依 (abs_deadline, release)
排序，依序指派執行時段：

* **Preemptive** job 取窗格 `[r, r + d − 1]` 的最前 e 個時段。
* **Non-preemptive** job 取 `[r, r + e − 1]` 連續 e 個時段（constraint 5）。

因為 frame size = 3、d = e 的 task（p1、p3）的 absolute deadline 落在
release time + e − 1，這些 job 必須佔用其 release 起的 e 個時段。每個
frame 內仍有足夠 slack 容納 p2、p4、p5、p6，可避免衝突。

### 2.2 Phase 2：Day-ahead processor dispatch

由於每小時可以由多個 processor 共同供電（assumption 4 + constraint 1），
排程演算法把所有 periodic job 的執行時段固定後，**每小時** 的能量需求
便已確定，剩下的就是傳統機組／PV／儲能的派遣與市場售電。

採用 greedy commitment + ramp/UT/DT 感知的派遣，邏輯如下：

1. **Commitment**：兩台傳統機組的 `initial_off_time = 99` 已超過 DT，第
   t = 1 即可開機。簡單採「整段 72 小時皆 ON」的策略：因為 fixed cost
   雖高，但兩台機組可確保所有 periodic / sporadic / aperiodic 需求都
   能被滿足，且能利用 t = 21~33、46~58、68~72 等高價時段大量售電抵
   銷成本。
2. **Per-hour dispatch**（`Dispatch.dispatch()`）：
   - PV 永遠以 `capacity × forecast` 出力（free，constraint 13）。
   - 兩台 thermal 各自從 `output_min` 起跑，並受 ramp 限制
     `[prev − RD, prev + RU]`、機組容量 `[output_min, output_max]`、
     開機過渡 `[output_min, RU]` 三者交集約束。
   - 若 supply < demand：依 variable cost 由低到高加碼 thermal，仍不
     足則啟用電池放電（受 `min(discharge_max, SOC − soc_min)` 限制）。
   - 若 supply > demand 且 `price < 60`：先把昂貴 thermal 退回下限，
     再以剩餘 surplus 充電池（受 `min(charge_max, soc_max − SOC)`）。
   - 若 `price ≥ 60`：把便宜 thermal 推到 ramp 上限賺套利（只在
     `price > cost_variable` 時推到 max）。
   - 剩餘 surplus 進入 `sell[t]`（constraint 22）。
3. **k 分配 post-pass**（`Dispatch.allocate_k()`）：為每個 job/charging
   demand 從各 processor 抽取電能。優先順序：
   - Job：PV → 便宜 thermal → 貴 thermal → battery discharge。
   - 充電需求：PV → 便宜 thermal → 貴 thermal（**不含電池**，
     constraint 21）。
   - 剩餘 PV/thermal 出力即 `sell`。電池放電只供應 job（constraint 20）。

### 2.3 Phase 3：Sporadic acceptance test

對每個 sporadic job（demo `s1..s5`，r 在 8/20/33/45/58）執行：

1. 將窗格 `[r, r + d − 1]` 切成所有可能的長度 e 連續區塊（採 non-preemptive
   解釋，符合「緊急用電」實務）。
2. 計算每小時的 **保留容量** = `Σ (output_max − P)` over thermal/PV/battery
   discharge headroom，作為「能否再加 w MWh」的保守上界。
3. 取第一個整個區塊都符合 `spare ≥ w` 的時段接受，否則 reject。
4. Accept 後 `commit_job` 更新 `demand`，並 **重跑 dispatch()** 讓後續
   acceptance test 看到最新狀態（dispatch 是純函數，重跑代價低）。
5. Accept/Reject 與原因記錄在 `output/acceptance_test_log.json`。

本作業的 5 個 sporadic 全部 accept，**sporadic_value_rate = 1.0**。

### 2.4 Phase 4：Aperiodic best-effort（soft deadline）

10 個 aperiodic 依 release time FIFO 排入，每個取 release 後最早 e 個有
足夠 spare 的時段（preemptive 解釋）。若 completion ≤ r + d − 1 則
soft deadline 滿足，否則記為 miss + tardiness。本次全部於 soft deadline
前完成，**soft_deadline_miss_rate = 0**。

---

## 3. 效能分析

由 `output/evaluation_results.json` 摘要：

| 指標 | 數值 |
|---|---|
| Hard deadline miss rate | 0.0 |
| Soft deadline miss rate | 0.0 |
| Average tardiness | 0 |
| Max tardiness | 0 |
| Average response time | 2.37 hr |
| Max response time | 4 hr |
| Completion-time jitter | 0.0 |
| Sporadic accepted / total | 5 / 5 |
| Sporadic value rate | 1.0 |
| Post-acceptance violation rate | 0.0 |
| Generator cost | 442 018.4 |
| Market revenue | 410 861.2 |
| Objective F | 31 157.2 |
| 總售電量 | 5 805.8 MWh |
| 總儲能充電 | 90.0 MWh |
| 23 條限制式違反數 | **0** |

### 3.1 為何 average response time 短

所有 periodic 與 sporadic job 都採 ASAP 安排；e = 1 的 job response = 1
小時，e = 3 的 d = e 任務 response = 3 小時。平均 2.37 反映了 task set
裡 e=2/3 jobs 的混合。完成時間抖動為 0，代表同一 task 各 instance 的
response 完全一致（schedule 完全 periodic）。

### 3.2 限制式驗證

`src/evaluator.py` 內逐條檢查 constraints 1～23，並列入
`evaluation_results.json` 的 `violations` 陣列。實測 **violations = 0**。

---

## 4. 日前保留策略效能分析

### 4.1 保留策略演算法說明

本作業並未採取「事先空白保留某些時段」的策略，而是利用 **動態 slack
推導**：原始 day-ahead 排程把每台 thermal 從 `output_min` 起跑、PV 與
電池當作 must-run / 待命，使每小時都有可量化的 headroom
= `Σ (output_max − P_current)`。這正好等同於對 sporadic 預留的最大可
接納量，無需顯式 reserve。優點：

* **零浪費**：保留量隨後續 commit 自動縮減，不會留下未使用的「固定
  reserve」。
* **可解釋**：`hour_spare_capacity()` 函式對任何時間 t 都能即時計算
  剩餘容量，作為 acceptance test 的判斷依據。
* **可組合**：acceptance test 接受新 job 後直接 commit_job +
  re-dispatch，下一個 acceptance test 就會看到最新狀態。

從實測數據觀察：5 個 sporadic 全部接受（用掉的 w 累積為 65 MWh，散落
在 t = 8/20/33/45/58 附近），10 個 aperiodic 全部完成且未 miss。原因
是 thermal_1 + thermal_2 + 兩台 PV 最大可提供約 80 + 45 + 60 + 80 = 265 MW，
而本 task set 最大瞬時 periodic demand 約為 t = 1 的 12 + 14 + 8 = 34
MWh，slack 充足。

### 4.2 目標函數權衡分析

三個目標的實測：

* `f1 = 0`（aperiodic miss = 0，懲罰金 0）。
* `f2 = 442 018.4`（傳統機組成本，其中固定成本占大宗：
  72 h × (1 200 + 600) = 129 600，其餘為變動成本 ≈ 312 k）。
* `f3 = −410 861.2`（售電收益）。

整體 `F = 0 + 442 018 − 410 861 = 31 157.2`（單位 $）。

**權衡 1：commitment vs. 成本**
若把任一 thermal 在 price = 0 的時段（t = 11~19、t = 35~43）關機，可以
省下 fixed cost 1 200 × 區段長度，但 DT = 2 + UT = 2/3 與 ramp 限制會
讓重新啟動非常昂貴；另外關機期間若有 sporadic 突發，無法馬上開機接案
（DT 還沒過），會直接降低 `sporadic_value_rate`，導致 5 分項目得分下
降。本作業選擇 **不關機** 來保持 `f1 = 0` 與 sporadic = 1.0。

**權衡 2：售電 vs. 充電**
低價時段（t = 11~19、35~43）我們選擇 **充電** 而不是 **強行售電**，因為
0 元賣不抵 cost_variable（42 / 70）。實際上由於 dispatch 已把昂貴
thermal 退到 output_min 並讓便宜 thermal 也退下來，多餘出力會優先充
電池（最多 90 MWh），剩下才被 sell（在價格 = 0 時 sell 為零）。

**權衡 3：sporadic value vs. 系統成本**
全收 5 個 sporadic 約增加 65 MWh 的 demand，幾乎完全由原本要 sell 的
overhead 吸收，因此**並未顯著增加 thermal 成本**（多數 hour thermal 不需
要再 ramp up）。這是「保留策略」的最大收益點：sporadic_value_rate 從可
能的 0~1 段拉到滿分 3 點，僅多耗 0~5k$ 的變動成本。

**權衡 4：jitter vs. response time**
本排程把所有 periodic instances 都對齊 ASAP 放置，jitter = 0；但這也讓
response time = e 等於最低值，無法再壓縮，因此沒有需要在「降低 response
time」與「降低 jitter」之間 trade-off。

---

## 5. 討論與心得

### 使用 AI 輔助說明

* 工具：**Claude（Anthropic）** 之 CLI（Claude Code）。
* 協作方式：
  1. 先讓 AI 完整讀完 RTSPJT 作業說明 PDF，逐段對齊每個 assumption、notation
     與 23 條 constraints。
  2. 共同設計 periodic task set（手算驗證 D_W、frame size 同時滿足
     1-2~1-8 全部規則）。
  3. AI 撰寫 `task_generator.py`、`scheduler.py`、`evaluator.py`，自行
     驗證 23 條 constraint。設計過程中發現 `prev_out` 在重新派遣時未重
     置的 bug，由 AI 主動診斷並修正。
  4. 報告大綱由人類擬定，AI 補上技術細節與權衡分析。
* Prompt 策略：採 **「精準上下文 + 完整任務目標 + 自我驗證」**：
  - 一次性把整份 PDF 餵入，避免 AI 漏看附錄。
  - 明確列出評分標準 1-1 ~ 6-2 對應的程式模組與檔案輸出。
  - 要求 AI 在 `evaluator.py` 中對每條 constraint 做自我檢查並列出
    violations，作為交付前的最後門檻。
* 實作心得：
  - 一個正確、模組化的 evaluator 是這次最大的回報：因為 23 條 constraints
    互相牽涉，沒有它幾乎不可能手動驗證每小時都合法。
  - 「保留策略」其實不一定要事先空出時段，把動態 spare capacity 當成
    隱式 reserve，在 acceptance test 時即時計算，效果更好且不浪費資源。
  - Frame size 與 d = e 任務的張力是這次最容易踩坑的地方：必須選 d = e
    的 task 之 e 等於 `2f − gcd(f, period)`，否則直接違反 1-8。
