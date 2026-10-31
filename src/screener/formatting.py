"""顯示用的數字格式。"""


def format_number(value: float) -> str:
    """最多兩位小數並去掉多餘的 0，例：123.50 → 123.5、2370.00 → 2370。"""
    return f"{value:.2f}".rstrip("0").rstrip(".")
