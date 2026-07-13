import argparse
import json
import os
import smtplib
from email.message import EmailMessage

import yfinance as yf

# 监控配置
TICKER = "SPYY.DE"  # 你要监控的ETF代码，SPYY.DE 在 yfinance 中可用
STATE_FILE = ".drawdown_state.json"
ALERT_LEVELS = [0.30, 0.40]
RECOVERY_AFTER_40 = 0.35  # 在达到 >=40% 后，回撤降到 <35% 时发送一次回落提醒
RESTORE_THRESHOLD = 0.20  # 回撤降到 <=20% 时发送一次恢复提醒并重置状态

# 邮件配置
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
EMAIL_TO = os.environ.get("EMAIL_TO", EMAIL_USER)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 465))


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"stage": "none"}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"stage": "none"}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def get_tier(drawdown):
    if drawdown >= ALERT_LEVELS[1]:
        return 40
    if drawdown >= ALERT_LEVELS[0]:
        return 30
    return None


def format_stage(stage):
    if stage == "none":
        return "无"
    if stage == "30":
        return "30% 已提醒"
    if stage == "40":
        return "40% 已提醒"
    if stage == "40_recovered":
        return "从40%回落已提醒"
    return stage


def check_drawdown(test_drawdown=None, dry_run=False):
    if not EMAIL_USER or not EMAIL_PASS:
        raise SystemExit("请先在 GitHub Secrets 中配置 EMAIL_USER 和 EMAIL_PASS。")

    ticker = yf.Ticker(TICKER)
    hist = ticker.history(period="max")

    if hist.empty:
        raise SystemExit("未获取到历史数据，请确认 ETF 代码是否正确。")

    max_price = hist["Close"].max()
    if test_drawdown is not None:
        drawdown = test_drawdown
        curr_price = max_price * (1 - drawdown)
        print("[测试模式] 模拟回撤比例。")
    else:
        curr_price = hist["Close"].iloc[-1]
        drawdown = (max_price - curr_price) / max_price

    tier = get_tier(drawdown)
    state = load_state()

    print(f"ETF: {TICKER}")
    print(f"当前价格: {curr_price:.2f}")
    print(f"历史最高价: {max_price:.2f}")
    print(f"当前回撤: {drawdown:.2%}")
    print(f"当前级别: {tier if tier is not None else '无'}")
    print(f"当前状态: {format_stage(state.get('stage'))}")

    # 先判断是否恢复到 20% 以下，恢复提醒并重置状态
    if state["stage"] != "none" and drawdown <= RESTORE_THRESHOLD:
        print("回撤已恢复到 20% 以下，发送恢复提醒并重置状态。")
        send_email(drawdown, None, dry_run, restored=True)
        state = {"stage": "none"}
        save_state(state)
        return

    # 40% 级别后回落到 35% 以下，发送一次回落提醒
    if state["stage"] == "40" and drawdown < RECOVERY_AFTER_40:
        print("已从 >=40% 回落到 <35%，发送一次回落提醒。")
        send_email(drawdown, None, dry_run, recovered=True)
        state = {"stage": "40_recovered"}
        save_state(state)
        return

    # 当前未达到报警区间：如果还未重置，则保持当前状态，否则不提醒
    if tier is None:
        print("当前回撤未达到 30% 或以上，暂不提醒。")
        save_state(state)
        return

    # 30% 阈值通知
    if tier == 30:
        if state["stage"] == "none":
            print("回撤达到 30%，发送 30% 提醒。")
            send_email(drawdown, 30, dry_run)
            state = {"stage": "30"}
        else:
            print("当前已处于 30% 或更高级别，无需重复提醒。")

    # 40% 阈值通知
    elif tier == 40:
        if state["stage"] in ["none", "30", "40_recovered"]:
            print("回撤达到 40%，发送 40% 提醒。")
            send_email(drawdown, 40, dry_run)
            state = {"stage": "40"}
        else:
            print("已在 40% 级别，50% 以上不再额外提醒。")

    save_state(state)


def send_email(drawdown, tier, dry_run=False, recovered=False, restored=False):
    if restored:
        subject = f"恢复提醒：{TICKER} 回撤已回落到 {drawdown:.2%}"
        body = (
            f"{TICKER} 已恢复到 20% 以下（当前回撤 {drawdown:.2%}）。\n"
            "风险已明显下降，提醒状态已重置。"
        )
    elif recovered:
        subject = f"回落提醒：{TICKER} 从 40% 级别回落到 {drawdown:.2%}"
        body = (
            f"{TICKER} 已从 40% 级别回落到 {drawdown:.2%}。\n"
            "已发送一次回落提醒。若后续再次达到 40% 或以上，会重新发送 40% 提醒。"
        )
    else:
        subject = f"风险提醒：{TICKER} 回撤达到 {drawdown:.2%} (级别 {tier})"
        body = (
            f"警告：{TICKER} 当前回撤已达到 {drawdown:.2%}。\n"
            f"当前级别：{tier}%。\n"
            "若已持仓，请及时检查风险。"
        )

    print(f"准备发送邮件: {subject}")
    if dry_run:
        print("[Dry run] 邮件内容如下:")
        print(body)
        return

    msg = EmailMessage()
    msg.set_content(body)
    msg["Subject"] = subject
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.login(EMAIL_USER, EMAIL_PASS)
        smtp.send_message(msg)

    print(f"邮件已发送至 {EMAIL_TO}。")


def parse_args():
    parser = argparse.ArgumentParser(description="ETF 最大回撤提醒脚本")
    parser.add_argument("--test-drawdown", type=float, help="模拟回撤比例，例如0.32")
    parser.add_argument("--dry-run", action="store_true", help="仅打印提醒内容，不实际发送邮件")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    check_drawdown(test_drawdown=args.test_drawdown, dry_run=args.dry_run)
