"""入口：初始化客户端 → 查询状态 → 定位校验 → 打卡 → 推送结果。

v1.0 主路径：直接使用 App 免登 token（获取方式见 docs/protocol.md），
无需在脚本里保存学号密码。CAS 自动登录为可选增强（src/login.py）。
"""
from src.config import load_config
from src.checkin import AttnClient, query_today_task, do_checkin, beijing_now
from src.notify import notify
from src.vacation import matched_range, today_cn, invalid_ranges
from src.campus import match_campus

import datetime
import json
import os
import re
import sys
import time

import requests

# Windows 计划任务 / run.bat 追加日志时，stdout 默认走 GBK(cp936)，
# 标题里的 ✅ ❌ 编码不了会抛 UnicodeEncodeError 直接终止进程——
# 2026-09-16 晚就因签到成功后 print("…✅") 崩在 notify() 前一行，导致整晚零通知。
# 这里强制 UTF-8 并容错，保证「打印」永远不会中断主流程。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # 非 CPython / 无 reconfigure 能力时忽略
    pass


def _save_token(cfg, token: str):
    """把新 token 回写到 config.yaml（避免每次运行都重新登录）。"""
    try:
        path = cfg.get("_config_path")
        if not path:
            return
        text = open(path, encoding="utf-8").read()
        new_text, n = re.subn(
            r'(token:\s*")([^"]*)(")', rf"\g<1>{token}\g<3>", text, count=1
        )
        if n:
            open(path, "w", encoding="utf-8").write(new_text)
            print("新 token 已回写 config.yaml")
    except Exception as e:
        print(f"token 回写失败（不影响本次运行）: {e}")


def _notify(cfg, title: str, content: str) -> bool:
    """推送结果。铁律：**先推送，后打印**——打印/编码异常绝不能吞掉通知。

    2026-09-16 教训：GBK 环境下 print("签到成功 ✅") 抛 UnicodeEncodeError，
    进程在 notify() 前一行就死了，签到成功却整晚零消息。
    这里推送失败自动重试一次；仍失败时打印醒目告警（签到结果本身不受影响）。
    """
    ok = False
    for attempt in (1, 2):
        try:
            if notify(cfg, title, content):
                ok = True
                break
        except Exception as e:  # notify 内部已兜底，这里再兜一层
            print(f"[notify] 第 {attempt} 次推送异常：{e}")
        if attempt == 1:
            print("[notify] 推送未成功，重试一次…")
    try:
        print(title)
    except Exception:
        pass
    if not ok:
        print("[notify] 警告：本次结果推送失败，请检查 config.yaml 的 notify 配置（签到结果不受影响）")
    return ok


def _daily_confirm_state_path(cfg):
    """每日完成确认的去重状态文件（记当天日期即可）。

    放在 config.yaml 同级的 state/ 下；拿不到配置目录时退回用户家目录。
    云端（GitHub Actions）每次都是全新容器、磁盘不保留，天然去不了重——
    所以那边靠 _on_github_actions() 直接关掉，否则 16 档会每档都推、刷屏。
    """
    try:
        base = os.path.dirname(os.path.abspath(cfg.get("_config_path") or ""))
        if base and os.path.isdir(base):
            d = os.path.join(base, "state")
            os.makedirs(d, exist_ok=True)
            return os.path.join(d, "daily_confirm.json")
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), ".fzu-checkin-daily-confirm.json")


def _on_github_actions() -> bool:
    """是否跑在 GitHub Actions 上（云端无持久磁盘，每日确认必须关闭）。"""
    return os.environ.get("GITHUB_ACTIONS", "").lower() == "true"


def _daily_confirm(cfg, title: str, content: str) -> bool:
    """每晚至多推一次的「完成确认」（心跳）。

    价值：让你能区分「系统活着、今晚只是没活干」和「系统已经死了三天你不知道」。
    2026-09-16 那次事故就是签到成功却因打印崩溃整晚零消息，用户完全无从判断系统状态。

    规则：
    - 开关：`notify.daily_confirm`（**默认 false**，需显式写 true 才发）。
      2026-09-17 用户拍板：「全关了吧，如果有 bug 再发通知」——
      心跳的价值（探测"程序完全没跑"）被判定为不值每晚这一条的打扰。
      ⚠️ 代价必须清楚：关掉后，「程序崩溃/电脑没开/计划任务被删」这类
      **发不出消息的故障将完全静默**，只能等下次真需要签到那晚才发现。
      签到失败 / 不在范围 / 坐标异常等**异常仍然照推**（那些走 _notify，不受本开关管），
      所以"有 bug 再通知"这条是满足的。
    - 云端强制关闭（见 _on_github_actions）
    - 去重：状态文件记当天日期，当天已推过就静默
    - **只用于「结论已确定」的分支**。像「服务器还没发布计划」这种
      不确定状态不要走这里——它自己有推送逻辑，拿来当心跳会发出假消息。
    - 不硬编码发送时刻：哪档先遇到确定结论就哪档发，因此对任意排档都自适应。
    - **签到成功/失败的分支不再补心跳**：那些分支已经把结论推给你了，
      当晚你已经知道结果，再补一条「今日已完成」就是重复（见 _mark_confirmed）。
    """
    if _on_github_actions():
        print("（GitHub Actions 环境：每日完成确认已关闭，避免每档重复推送）")
        return False
    if not (cfg.get("notify") or {}).get("daily_confirm", False):
        return False

    today = beijing_now().strftime("%Y-%m-%d")
    path = _daily_confirm_state_path(cfg)
    try:
        if os.path.exists(path):
            if (json.load(open(path, encoding="utf-8")) or {}).get("date") == today:
                print(f"每日完成确认：今天（{today}）已推送过，本次静默")
                return False
    except Exception as e:
        print(f"读取确认状态失败（按未推送处理）: {e}")

    if not _notify(cfg, title, content):
        return False

    try:
        json.dump({"date": today}, open(path, "w", encoding="utf-8"))
    except Exception as e:
        print(f"写入确认状态失败（不影响签到结果）: {e}")
    return True


def _mark_confirmed(cfg):
    """把当天标记为「今晚已经推送过结论」，后续档不再补心跳。

    2026-09-17 实测：21:35 那档签到成功、推了「签到成功 ✅」，但它走的是 _notify，
    **不写去重状态**；21:50 那档发现「已签到」→ 走 _daily_confirm → 以为今晚还没推过
    → 又推一条「今日已完成 ✅」，一晚白收两条。

    签到成功 / 签到失败 / 不在签到范围，都是当晚已送达的确定结论，
    心跳的价值（证明系统还活着）已经兑现，不必再来一条。

    ⚠️ **只在推送成功后调用**（调用点见 main 末尾）。推送失败时必须不标记，
    否则「结论没送到 + 心跳也被压掉」= 整晚零消息，正是 2026-09-16 的故障形态。
    """
    if _on_github_actions():
        return
    try:
        json.dump({"date": beijing_now().strftime("%Y-%m-%d")},
                  open(_daily_confirm_state_path(cfg), "w", encoding="utf-8"))
    except Exception as e:
        print(f"写入确认状态失败（不影响签到结果）: {e}")


def _bad_vacation_state_path(cfg):
    """假期设置提醒的去重状态文件（每晚至多提醒一次）。"""
    try:
        base = os.path.dirname(os.path.abspath(cfg.get("_config_path") or ""))
        if base and os.path.isdir(base):
            d = os.path.join(base, "state")
            os.makedirs(d, exist_ok=True)
            return os.path.join(d, "vacation_warn.json")
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), ".fzu-checkin-vacation-warn.json")


def _warn_bad_vacation(cfg, problems):
    """假期区间写坏了 → 必须让用户知道。

    为什么需要：旧实现遇到写坏的区间就 `continue` 悄悄跳过，用户以为设好了、
    实际照签（离校期间还会变成代签），而屏幕上什么提示都没有。
    这正是本项目最忌讳的静默失败，所以改成「每晚最多提醒一次」。

    不抛异常——这是提示，不是签到逻辑；提示失败绝不能影响签到。
    """
    msg = "；".join(problems)
    print(f"⚠️ 假期设置有问题（{len(problems)} 处），这些日子不会被跳过：{msg}")
    if _on_github_actions():
        # 云端无持久磁盘，去了重就是 16 档刷屏；那边只留日志，提醒交给本机跑
        return
    path = _bad_vacation_state_path(cfg)
    today = beijing_now().strftime("%Y-%m-%d")
    try:
        if os.path.exists(path) and (json.load(open(path, encoding="utf-8")) or {}).get("date") == today:
            return
    except Exception:
        pass
    if not _notify(
        cfg,
        "智汇福大晚点名：假期设置写错了 ⚠️",
        f"config.yaml 的 vacation.skip_ranges 有 {len(problems)} 处无法识别，"
        f"这些日子不会被跳过（仍会照常签到）：\n{msg}\n"
        "改好后这条提醒自动消失（每晚最多提醒一次）。",
    ):
        return
    try:
        json.dump({"date": today}, open(path, "w", encoding="utf-8"))
    except Exception as e:
        print(f"写入假期提醒状态失败（不影响签到）: {e}")


def main():
    # 启动时间戳：run.log 由计划任务长期追加，没有时间信息就只能靠行序推演，
    # 一次崩溃发生在几点、是哪一次运行都无从判断（2026-09-16 排查的最大障碍）。
    print(f"===== 启动：北京时间 {beijing_now():%Y-%m-%d %H:%M:%S} =====")
    cfg = load_config()

    # GitHub 定时档实测常被延迟 1-4 小时（晚间档可能整批拖到凌晨）：
    # 窗口外到达的运行一律静默跳过（不登录、不推送）；
    # 卡在窗口边缘（21:30-21:34）的运行等满 21:35 再签，避开服务器刚开窗的边界；
    # FZU_CHECKIN_FORCE=1（本地部署脚本试运行用）：绕过窗口限制，只验证连通不真签
    force = bool(os.environ.get("FZU_CHECKIN_FORCE"))
    now = beijing_now()
    if datetime.time(21, 30) <= now.time() < datetime.time(21, 35) and not force:
        wait = (datetime.datetime.combine(now.date(), datetime.time(21, 35)) - now).total_seconds()
        print(f"当前北京时间 {now:%H:%M}，等待 {int(wait) + 1} 秒到 21:35 再执行")
        time.sleep(max(wait, 0) + 1)
        now = beijing_now()
    in_window = datetime.time(21, 35) <= now.time() <= datetime.time(23, 59, 59)
    if not in_window and not force:
        print(f"北京时间 {now:%H:%M} 不在晚点名窗口（21:35-23:59），判定为延迟触发的补跑，静默跳过。")
        return

    # 假期自动跳过：命中校历/自定义区间时什么都不做（默认静默）
    # 先检查「写坏的区间」再判断是否命中——两类情况都要让用户知道，
    # 而且必须排在假期分支之前，否则假期当天会把坏配置一起吞掉。
    bad_vac = invalid_ranges(cfg)
    if bad_vac:
        _warn_bad_vacation(cfg, bad_vac)
    vac = matched_range(cfg)
    if vac:
        if (cfg.get("vacation") or {}).get("notify"):
            _notify(cfg, f"智汇福大晚点名：假期中，已跳过（{vac}）", "假期期间自动签到暂停。")
        else:
            print(f"假期中（{vac}），跳过本次签到。今天是 {today_cn()}。")
        return

    # 定位签到护栏：签前校验配置坐标必须落在任一校区范围内（软约束，防误填/防离校代签）。
    # 服务端 check.action 仍会做精确多边形围栏，这里只是「快速失败」——
    # 明显不对的坐标直接不发签到请求，并把原因推给用户，避免"按了没反应"。
    try:
        lng = float((cfg.get("checkin") or {}).get("longitude"))
        lat = float((cfg.get("checkin") or {}).get("latitude"))
    except (TypeError, ValueError):
        lng = lat = None
    if lng is None or lat is None:
        title = "智汇福大晚点名：坐标未配置 ❌"
        print("未读到有效坐标，本次未签到（请填 config.yaml 的 checkin.longitude / latitude）")
        if not force:
            _notify(cfg, title,
                    "未读到有效坐标，本次未签到。请在 config.yaml 的 checkin.longitude / latitude 填入你的坐标。")
        return
    campus = match_campus(lng, lat, cfg)
    if campus is None:
        title = "智汇福大晚点名：坐标不在校区范围，已拦截 ❌"
        print(f"坐标=({lng}, {lat}) 未命中任何校区")
        if force:
            print("[试运行] 坐标不在任何校区范围内，正式运行会被拦截，请先核对坐标")
        else:
            _notify(cfg, title,
                   "配置的坐标不在任何校区范围内，今日签到已拦截。请核对 config.yaml 的 "
                   "checkin.longitude / latitude；若你在其他校区，可在 campus.bounds 里补上该校区范围。")
        return
    print(f"定位护栏通过：坐标 ({lng}, {lat}) 命中「{campus}」")

    token = (cfg.get("user") or {}).get("token", "")
    if not token:
        username = (cfg.get("user") or {}).get("username", "")
        password = (cfg.get("user") or {}).get("password", "")
        if not (username and password):
            raise SystemExit(
                "config.yaml 未配置 token，且学号/密码不全（学号密码=智汇福大 App 登录账号）"
            )
        from src.login import login as sso_login

        try:
            token = sso_login(username, password)
        except Exception as e:  # noqa: BLE001
            # 登录失败必须推到人手上，绝不静默崩掉。
            # 2026-09-21：改过统一身份认证密码、但 config.yaml 里还是旧密码 →
            # 这里直接抛异常穿透到 __main__ 外面，run.log 里只有 traceback，
            # 用户整晚收不到任何消息，还以为签上了。异常通知是明确定过要保留的。
            _notify(
                cfg,
                "智汇福大晚点名：登录失败 ❌",
                f"用学号密码登录统一身份认证失败：{e}\n"
                "最常见原因：密码已修改，但 config.yaml 里还是旧密码。\n"
                "本次无法自动签到，请手动打开 App 确认。",
            )
            return
        print("SSO 登录成功，已获取新 token")
        _save_token(cfg, token)

    client = AttnClient(token)
    try:
        status = query_today_task(client, cfg)
    except requests.HTTPError as e:
        # init 返回 500 的两种原因：
        #  a) 跨午夜时段（约 0:00-1:00）服务器尚未发布当日计划（属正常，稍后自愈）
        #  b) token 已失效/过期（服务器对无效 token 也返回 500 而不是 401）
        # token 失效时若还有学号密码，立即重新登录换新 token 重试一次——否则会
        # 「明明有签到计划却报无计划」，白丢一晚。
        username = (cfg.get("user") or {}).get("username", "")
        password = (cfg.get("user") or {}).get("password", "")
        if username and password:
            print(f"init 返回 {e}，尝试重新登录换 token 后重试…")
            status = None
            try:
                from src.login import login as sso_login

                token = sso_login(username, password)
                _save_token(cfg, token)
                client = AttnClient(token)
                status = query_today_task(client, cfg)
                print("重新登录成功，已用新 token 继续。")
            except Exception as e2:
                # init 500 + 重登也失败：最常见的原因是凭据本身不对（改过密码但没同步），
                # 而不是"服务器还没发计划"。标题必须说真话，否则用户会朝错的方向排查。
                if beijing_now().time() < datetime.time(21, 45):
                    _notify(
                        cfg,
                        "智汇福大晚点名：登录失败 ❌",
                        f"init 失败：{e}\n重新登录也失败：{e2}\n"
                        "可能是 ①统一身份认证密码已修改（config.yaml 里还是旧密码）"
                        "②服务器波动。\n本次无法自动签到，请手动打开 App 确认。",
                    )
                else:
                    print(f"登录失败（兜底档静默，避免一晚连推）：init={e}；重登={e2}")
                return
            if status is None:
                return
        else:
            title = "智汇福大晚点名：服务器暂未返回今日计划"
            # 21:45 前视为首跑，推送提醒一次即可；21:50/22:05 兜底跑静默，避免一晚连推三条
            if beijing_now().time() < datetime.time(21, 45):
                _notify(cfg, title, "服务器暂时无今日计划数据（跨天时段/服务波动），本次跳过。稍后自动重试无需操作。")
            else:
                print(f"{title} {e}（兜底跑：仍无计划数据，静默跳过，不再重复推送）")
            return
    init_data = status["init"]

    if force:
        # 本地部署脚本试运行：只验证配置 / 登录 / 服务器连通，不真签、不推送。
        # 无论是否在签到窗口内都保持只读——避免"试运行"意外产生真实打卡。
        state = "已签到（无需操作）" if not status["need_checkin"] else "待签到（到点会自动执行）"
        print("[试运行] 配置读取 ✅  登录/Token ✅  服务器连通 ✅")
        print(f"[试运行] 今日状态：{state}")
        print("[试运行] 仅验证，不签到、不推送；正式签到由计划任务在 21:35 起自动完成")
        return

    if not status["need_checkin"]:
        # 已签到：每晚至多推一条「今日已完成」当心跳。
        # 以前这里是彻底静默，代价是——首档签到成功但推送丢失时（2026-09-16），
        # 整晚一条消息都没有，你根本分不清「系统正常但没活干」还是「系统已经死了」。
        _daily_confirm(
            cfg,
            "智汇福大晚点名：今日已完成 ✅",
            "今日晚点名已签到，无需任何操作。\n"
            "（每晚一条的完成确认，用来确认自动签到还在正常运行。\n"
            "不想收到：在 config.yaml 的 notify: 下面加一行 daily_confirm: false）",
        )
        return

    if not init_data.get("schoolData"):
        # 今日无考勤计划也是「确定结论」，同样纳入每晚一条的完成确认
        _daily_confirm(
            cfg,
            "智汇福大晚点名：今日无考勤计划",
            "服务器未返回签到范围（可能今日无晚点名），未执行打卡。\n"
            "（每晚一条的完成确认，用来确认自动签到还在正常运行。\n"
            "不想收到：在 config.yaml 的 notify: 下面加一行 daily_confirm: false）",
        )
        return

    try:
        ok = do_checkin(client, cfg, init_data)
    except Exception as e:  # 接口报错（如不在时段/范围）要带原因推送
        if _notify(cfg, "智汇福大晚点名：签到失败 ❌", f"自动签到失败：{e}\n请手动打开 App 签到。"):
            _mark_confirmed(cfg)
        return

    if ok:
        delivered = _notify(cfg, "智汇福大晚点名：签到成功 ✅", "今日晚点名已自动签到成功。")
    else:
        delivered = _notify(cfg, "智汇福大晚点名：不在签到范围 ❌", "定位校验未命中任何校区，请确认配置的经纬度。")

    # 只有「结论确实送到用户手上」才标记当晚已确认。
    # 推送失败时不标记 → 后续档的 _daily_confirm 会补一条心跳当兜底报警，
    # 正好覆盖 2026-09-16 那种「签到成功但推送丢了、整晚零消息」的故障。
    if delivered:
        _mark_confirmed(cfg)


def _notify_fatal(exc) -> None:
    """最后一道网：任何**未预料到**的异常，也绝不能让用户整晚零消息。

    2026-09-21 立。main() 里逐条路径都补了告警，但只要还剩一条没预料到的
    路径（第三方库报错、配置结构出乎意料、磁盘/编码问题…），用户看到的
    仍然是「今晚没消息」——而「没消息」既可能是签到成功，也可能是程序
    从没跑起来，两者在用户那一侧分不出来。这正是要消灭的东西。

    异常本身照旧向上抛：run.log 保留完整 traceback，退出码非 0
    （计划任务/CI 能看出失败），这里只负责「多送一条推送」。
    """
    try:
        import traceback

        tb = traceback.format_exc()
        try:
            print(tb)
        except Exception:
            pass
        cfg = load_config()
    except BaseException:
        print("[fatal] 连配置都读不出来，无法推送告警（run.log 里有 traceback）")
        return
    last = (tb.strip().splitlines() or ["（无 traceback）"])[-1].strip()
    try:
        _notify(
            cfg,
            "智汇福大晚点名：运行异常 ❌",
            f"自动签到遇到未预料的错误，本次没有完成：\n"
            f"{type(exc).__name__}: {exc}\n最后一行：{last}\n"
            "请手动打开 App 确认签到状态。",
        )
    except BaseException as e2:  # 告警自己崩了也不能影响退出码
        print(f"[fatal] 告警推送也失败：{e2}")


if __name__ == "__main__":
    try:
        main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as e:  # noqa: BLE001 —— 兜底网，见 _notify_fatal
        _notify_fatal(e)
        raise
