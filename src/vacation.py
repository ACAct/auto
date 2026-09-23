"""假期跳过：根据校历/自定义日期区间，假期内不执行任何签到操作。

区间来源（config.yaml 的 vacation.skip_ranges）：
- 官方校历：福州大学 2026—2027 学年校历（福大教〔2026〕10号）
  寒假 2027-01-23 ~ 2027-02-21；暑假 2027-07-03 ~ 2027-08-29
- 个人区间：用户可自行添加（如实训、离校等），同样只跳过、不代签

健壮性（2026-09-21 修，全部有回归测试 test-vacation-skip.py）：
- **本模块绝不抛异常**。它是 main() 里排在签到之前的判断，一抛就是整晚不签到、
  且（在加入全局兜底之前）用户收不到任何消息。
- 已修的真实故障：用户少写一个「-」，YAML 把 skip_ranges 解析成映射而不是列表
  → 旧实现 `str["start"]` 抛 `TypeError: string indices must be integers`，
  在 21:35 直接崩掉整晚（实测）。现在单条映射按「一段区间」正常生效。
- 已修的静默漏判：`2026/09/21`、`2026-9-21` 这类手写日期旧实现直接忽略、
  不留任何痕迹 → 用户以为设好了，实际照签。现在这些写法都能认。
- 认不出来的条目**不再静默吞掉**：由 invalid_ranges() 汇总，main() 提示用户。
"""
import datetime
import re

# 北京时间（GitHub Actions 跑在 UTC，统一按 UTC+8 判定“今天”）
_TZ_CN = datetime.timezone(datetime.timedelta(hours=8), "Asia/Shanghai")

# 容忍 2026-09-21 / 2026-9-21 / 2026/09/21 / 2026.9.21 / 2026年9月21日
_DATE_RE = re.compile(r"^\s*(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?\s*$")


def today_cn() -> datetime.date:
    return datetime.datetime.now(_TZ_CN).date()


def parse_date(value):
    """把用户写的日期解析成 date；认不出来返回 None（绝不抛异常）。

    YAML 里不加引号时（`start: 2026-09-21`）PyYAML 会直接给出 date 对象，
    所以也要接受 date/datetime 本身。
    """
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        pass
    m = _DATE_RE.match(text)
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _range_list(cfg) -> list:
    """把 vacation.skip_ranges 归一成「区间 dict 的列表」。

    容忍三种写法：
    - 标准列表：[{name,start,end}, ...]
    - **单条映射**（用户少写了「-」）：{name,start,end} → 当作一段
    - 带键的映射：{"军训": {start,end}, ...} → 取里面的区间
    其它类型（字符串、数字等）一律当作「没配」。
    """
    raw = (cfg.get("vacation") or {}).get("skip_ranges")
    if not raw:
        return []
    if isinstance(raw, dict):
        # 有子项是 dict → 是「带键的映射」（{"军训": {start,end}}）；否则就是单条区间本身
        inner = [v for v in raw.values() if isinstance(v, dict)]
        return inner if inner else [raw]
    if isinstance(raw, (list, tuple)):
        return [r for r in raw if isinstance(r, dict)]
    return []


def matched_range(cfg: dict, today: datetime.date | None = None):
    """命中假期则返回区间名（用于提示），否则返回 None。"""
    today = today or today_cn()
    for r in _range_list(cfg):
        start = parse_date(r.get("start"))
        end = parse_date(r.get("end"))
        if start is None or end is None:
            continue  # 写坏了：这里只负责「不崩」，提示交给 invalid_ranges()
        if start > end:
            continue  # 开始晚于结束 = 不可能命中，同样交给 invalid_ranges()
        if start <= today <= end:
            return r.get("name") or f"{start} ~ {end}"
    return None


def invalid_ranges(cfg) -> list:
    """列出「写了但用不了」的假期区间（人类可读），供 main() 提示用户。

    为什么要有它：旧实现遇到写坏的区间就 `continue` 悄悄跳过，
    用户以为设好了、实际照签，而且屏幕上什么提示都没有
    —— 属于本项目最忌讳的静默失败。
    """
    problems = []
    for i, r in enumerate(_range_list(cfg), 1):
        name = str(r.get("name") or f"第 {i} 段")
        start_raw, end_raw = r.get("start"), r.get("end")
        start, end = parse_date(start_raw), parse_date(end_raw)
        if start is None or end is None:
            bad = []
            if start is None:
                bad.append(f"开始日期 {start_raw!r}" if start_raw not in (None, "") else "开始日期未填")
            if end is None:
                bad.append(f"结束日期 {end_raw!r}" if end_raw not in (None, "") else "结束日期未填")
            problems.append(f"「{name}」的 {'、'.join(bad)} 无法识别（需形如 2026-10-01）")
        elif start > end:
            problems.append(f"「{name}」开始日期 {start} 晚于结束日期 {end}，该段永远不生效")
    return problems
