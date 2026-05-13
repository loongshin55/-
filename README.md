# VPP Real-Time Scheduling — Level 1 Submission

## 1. 使用語言、版本與套件需求

- **使用語言**：Python
- **Python 版本**：**3.13.2**（最低需求 Python 3.10+）
- **作業系統**：macOS 26.2（Darwin 25.2.0，arm64 / Apple Silicon）
- **直譯器路徑**（本機開發環境）：`/opt/homebrew/bin/python3`（Homebrew 安裝）
- **套件需求**：僅使用 **Python 標準函式庫**，**不需要任何第三方套件**

| 模組 | 用途 |
|---|---|
| `json` | 讀寫所有 input/output JSON |
| `math` | `gcd`（frame size 驗證） |
| `os` | 路徑處理、產生 output 目錄 |
| `statistics` | `mean`、`pstdev`（計算 response time / jitter） |
| `collections.defaultdict` | k 分配、charge_flow 等多層 dict |
| `sys` | evaluator 引用 scheduler 模組 |
| `functools` | task_generator 內部 reduce |

> 沒有用到 PuLP / OR-Tools / numpy / pandas / scipy，完全以純 Python 實作。任何 Python 3.10+ 的環境（macOS、Linux、Windows）皆可直接執行。

## 2. 程式編譯方式或環境設定

純 Python，**無需編譯，也不需要 virtualenv 或 pip install**。

確認本機 Python 版本 ≥ 3.10：

```bash
python3 --version    # 應輸出 Python 3.10.x 以上；本作業開發於 3.13.2
```

若系統中 `python3` 並非 3.10+，可用以下方式安裝對應版本：

```bash
# macOS（Homebrew）
brew install python@3.13

# Ubuntu / Debian
sudo apt install python3.13

# Windows（從官網下載 installer）
# https://www.python.org/downloads/
```

## 3. 程式執行流程

由專案根目錄依序執行：

```bash
python3 src/task_generator.py    # 產生 output/task_set.json
python3 src/scheduler.py         # 產生 output/schedule_result.json 與 output/acceptance_test_log.json
python3 src/evaluator.py         # 產生 output/evaluation_results.json
```

或一行跑完整 pipeline：

```bash
python3 src/task_generator.py && python3 src/scheduler.py && python3 src/evaluator.py
```

## 4. 各程式輸入與輸出檔案說明

| Stage | 程式 | 輸入 | 輸出 |
|---|---|---|---|
| Task Generation | `src/task_generator.py` | （無，內建固定 task set） | `output/task_set.json` |
| Scheduling | `src/scheduler.py` | `output/task_set.json`、`input/processor_settings.json`、`input/price_72hr.json`、`input/demo_jobs.json` | `output/schedule_result.json`、`output/acceptance_test_log.json` |
| Evaluation | `src/evaluator.py` | 上述所有 JSON | `output/evaluation_results.json` |

### 4.1 `input/processor_settings.json`
依照附錄 A/B/C/D 所定義之傳統機組、再生能源、儲能設備參數，並包含 `charging_jobs`（Jchg）以指定儲能充電需求。

### 4.2 `input/price_72hr.json`
72 個時段的市場售電價格，附錄 E 格式。

### 4.3 `input/demo_jobs.json`（自訂的 demo 輸入）
模擬 Demo 時提供的 sporadic 與 aperiodic 工作清單，附錄 I 範圍內：5 個 sporadic（e ∈ [1,3], w ∈ [5,20]）、10 個 aperiodic（e ∈ [1,4], w ∈ [5,15]）。

### 4.4 `output/task_set.json`
包含 `periodic`（task 定義，附錄 F 欄位）、`frame_size`、`workload_density`、`expanded_job_count` 與展開後的 `expanded_jobs`。

### 4.5 `output/schedule_result.json`
依附錄 G 規定，包含 `schedule_result` 陣列（每小時 `t`、`P`、`k`、`sell`、`soc`、`missed_aperiodic`、`rejected_sporadic`），並附帶 `periodic_schedule`、`accepted_sporadic`、`rejected_sporadic_jobs`、`aperiodic_misses` 供報告分析。

### 4.6 `output/evaluation_results.json`
依附錄 H 規定的欄位（hard/soft miss rate、tardiness、response time、jitter、acceptance_test、sporadic_value_rate、post_acceptance_violation_rate、generator_cost、market_revenue、objective_value），另附 `violations` 陣列（為空表示所有 23 條限制式皆滿足）。

### 4.7 `output/acceptance_test_log.json`
每個 sporadic job 的 accept/reject 紀錄，含安排時段或拒絕原因。

## 5. 如何重現繳交的 output JSON

```bash
# 從本資料夾根目錄
python3 src/task_generator.py
python3 src/scheduler.py
python3 src/evaluator.py
```

由於 task set、scheduler、acceptance test 都是 deterministic（無隨機數），三個 JSON 輸出每次執行都會完全相同。

## 6. 報告

請見 `report.md`（已包含 Periodic task set 產生方式、排程演算法、效能分析、保留策略與心得）。
