class JobFailed(Exception):
    """job 未能完成。訊息會寫入 log，CLI 以結束碼 1 結束。"""
