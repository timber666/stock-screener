"""盤中即時系統（REALTIME_PLAN.md）。

依賴只能單向（REALTIME_PLAN.md 4.2、PROJECT_PLAN.md 10.4）：這裡可以匯入共用模組，每日系統、共用模組與研究程式
都不匯入這個套件。需要 shioaji 的程式只有 shioaji_source.py，而且在連線時才匯入，其餘模組在排程用的環境也能測試。
"""
