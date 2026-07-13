import argparse
import json
import os
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from datetime import datetime

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
        return {"stage": "none", "last_alert_drawdown": None}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
            # 向后兼容：确保存在 last_alert_drawdown 字段
            if "last_alert_drawdown" not in s:
                s["last_alert_drawdown"] = None
            return s
    except Exception:
        return {"stage": "none", "last_alert_drawdown": None}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def get_tier(drawdown):
    if drawdown >= ALERT_LEVELS[1]:
        return 40
    if drawdown >= ALERT_LEVELS[0]:
        return 30
    return None


def compute_contribution(drawdown, prev_stage=None):
    """根据当前回撤和上一次阶段计算建议的季度投资金额（欧元）。

    规则：
    - 初始/正常：<=30% -> 300
    - >=30% 且 <40% -> 450
    - >=40% -> 600
    - 特殊恢复逻辑：如果之前处于 40% 级别，回落到 <35% 但仍 >20%，建议 450（保持在 40->35 回落后的调整）
    - 完全恢复到 <=20% -> 300
    """
    # 恢复到 20% 由调用者判断（如果 drawdown <= RESTORE_THRESHOLD，会走恢复逻辑）
    # 如果之前处于 40 且当前回撤低于 RECOVERY_AFTER_40 但高于 RESTORE_THRESHOLD，返回 450
    if prev_stage == "40" and drawdown < RECOVERY_AFTER_40 and drawdown > RESTORE_THRESHOLD:
        return 450
    if drawdown >= ALERT_LEVELS[1]:
        return 600
    if drawdown >= ALERT_LEVELS[0]:
        return 450
    return 300


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
    if not dry_run and (not EMAIL_USER or not EMAIL_PASS):
        raise SystemExit("请先在 GitHub Secrets 中配置 EMAIL_USER 和 EMAIL_PASS。")

    ticker = yf.Ticker(TICKER)
    hist = ticker.history(period="max")

    if hist.empty:
        raise SystemExit("未获取到历史数据，请确认 ETF 代码是否正确。")

    max_price = hist["Close"].max()
    max_date = hist["Close"].idxmax().date().isoformat()
    if test_drawdown is not None:
        drawdown = test_drawdown
        curr_price = max_price * (1 - drawdown)
        print("[测试模式] 模拟回撤比例。")
    else:
        curr_price = hist["Close"].iloc[-1]
        drawdown = (max_price - curr_price) / max_price

    tier = get_tier(drawdown)
    state = load_state()

    # 计算先前建议与当前建议的季度投资额（欧元）
    stage_amount_map = {
        "none": 300,
        "30": 450,
        "40": 600,
        "40_recovered": 450,
    }
    prev_stage = state.get("stage", "none")
    prev_contribution = stage_amount_map.get(prev_stage, 300)
    curr_contribution = compute_contribution(drawdown, prev_stage)

    print(f"ETF: {TICKER}")
    print(f"当前价格: {curr_price:.2f}")
    print(f"历史最高价: {max_price:.2f}")
    print(f"当前回撤: {drawdown:.2%}")
    print(f"当前级别: {tier if tier is not None else '无'}")
    print(f"当前状态: {format_stage(state.get('stage'))}")

    # 先判断是否恢复到 20% 以下，恢复提醒并重置状态
    if state["stage"] != "none" and drawdown <= RESTORE_THRESHOLD:
        print("回撤已恢复到 20% 以下，发送恢复提醒并重置状态。")
        send_email(drawdown, None, curr_price, max_price, max_date, curr_contribution, prev_contribution, dry_run, restored=True, stage=state.get("stage"), prev_alert_drawdown=state.get("last_alert_drawdown"))
        state = {"stage": "none", "last_alert_drawdown": drawdown}
        save_state(state)
        return

    # 40% 级别后回落到 35% 以下，发送一次回落提醒
    if state["stage"] == "40" and drawdown < RECOVERY_AFTER_40:
        print("已从 >=40% 回落到 <35%，发送一次回落提醒。")
        send_email(drawdown, None, curr_price, max_price, max_date, curr_contribution, prev_contribution, dry_run, recovered=True, stage=state.get("stage"), prev_alert_drawdown=state.get("last_alert_drawdown"))
        state = {"stage": "40_recovered", "last_alert_drawdown": drawdown}
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
            send_email(drawdown, 30, curr_price, max_price, max_date, curr_contribution, prev_contribution, dry_run, stage=state.get("stage"), prev_alert_drawdown=state.get("last_alert_drawdown"))
            state = {"stage": "30", "last_alert_drawdown": drawdown}
        else:
            print("当前已处于 30% 或更高级别，无需重复提醒。")

    # 40% 阈值通知
    elif tier == 40:
        if state["stage"] in ["none", "30", "40_recovered"]:
            print("回撤达到 40%，发送 40% 提醒。")
            send_email(drawdown, 40, curr_price, max_price, max_date, curr_contribution, prev_contribution, dry_run, stage=state.get("stage"), prev_alert_drawdown=state.get("last_alert_drawdown"))
            state = {"stage": "40", "last_alert_drawdown": drawdown}
        else:
            print("已在 40% 级别，50% 以上不再额外提醒。")

    save_state(state)


def send_email(drawdown, tier, curr_price, max_price, max_date, curr_contribution, prev_contribution, dry_run=False, recovered=False, restored=False, stage=None, prev_alert_drawdown=None):
    ts = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    if restored:
        subject = f"[ETF 监控] 已恢复：{TICKER} 回撤回到 {drawdown:.2%}"
    subtitle = "ETF 投资节奏更新"
    if restored:
        subject = f"[ETF 投资节奏更新] {TICKER} 回撤回到 {drawdown:.2%}"
        note = "当前已恢复到日常投资节奏。"
    elif recovered:
        subject = f"[ETF 投资节奏更新] {TICKER} 回撤减少至 {drawdown:.2%}"
        note = "请根据当前回撤水平调整季度投资金额。"
    else:
        subject = f"[ETF 投资节奏更新] {TICKER} 当前回撤 {drawdown:.2%}"
        note = "请根据当前回撤水平调整季度投资金额。"

    plain = (
        f"{subtitle}\n"
        f"时间(UTC): {ts}\n"
        f"标的: {TICKER}\n"
        f"当前价格: {curr_price:.2f}\n"
        f"历史最高价: {max_price:.2f} (日期: {max_date})\n"
        f"当前回撤: {drawdown:.2%}\n"
        f"建议季度投资: €{curr_contribution} (此前建议: €{prev_contribution})\n\n"
        f"{note}\n\n"
        "季度投资规则（状态机变迁条件）：\n"
        "投资规则：\n"
        " - 日常状态（回撤 < 30%）：每季度 €300\n"
        " - 回撤达到或超过 30%：每季度 €450\n"
        " - 回撤达到或超过 40%：每季度 €600\n"
        " - 回撤减少到 35% 以内（< 35%）：每季度 €450\n"
        " - 回撤减少到 20% 以内（<= 20%）：每季度 €300\n\n"
        "此邮件由自动化监控脚本发送，若要调整告警阈值或停止通知，请更新仓库配置或联系维护人。"
    )

    html = f"""
    <html>
      <body>
        <h2>{subtitle}</h2>
        <p><strong>时间(UTC):</strong> {ts}</p>
        <p><strong>标的:</strong> {TICKER}</p>
        <table border="0" cellpadding="4">
            <tr><td><strong>当前价格</strong></td><td>{curr_price:.2f}</td></tr>
            <tr><td><strong>历史最高价</strong></td><td>{max_price:.2f} （{max_date}）</td></tr>
            <tr><td><strong>当前回撤</strong></td><td>{drawdown:.2%}</td></tr>
            <tr><td><strong>建议季度投资</strong></td><td>€{curr_contribution}（此前：€{prev_contribution}）</td></tr>
        </table>
        
        <br>
        <h3>季度投资规则（状态机变迁条件）</h3>
        <table border="1" cellpadding="6" style="border-collapse:collapse; text-align: left;">
            <tr style="background-color:#f2f2f2;">
                <th>状态 / 变迁方向</th>
                <th>触发条件</th>
                <th>目标投资额 (€/季度)</th>
            </tr>
            <tr>
                <td style="font-weight:bold;">日常状态</td>
                <td>回撤 &lt; 30%</td>
                <td>300</td>
            </tr>
            <tr>
                <td rowspan="2" style="font-weight:bold;">向下加码（跌）</td>
                <td>回撤 &ge; 30%</td>
                <td>450</td>
            </tr>
            <tr>
                <td>回撤 &ge; 40%</td>
                <td>600</td>
            </tr>
            <tr>
                <td rowspan="2" style="font-weight:bold;">向上恢复（涨）</td>
                <td>回撤 &lt; 35%</td>
                <td>450</td>
            </tr>
            <tr>
                <td>回撤 &le; 20%</td>
                <td>300</td>
            </tr>
        </table>
        
        <p>{note}</p>
        <hr>
        <p style="font-size:small;color:gray;">此邮件由自动化监控脚本发送。若要修改告警阈值或停止通知，请更新仓库配置或联系维护人。</p>
      </body>
    </html>
    """

    print(f"准备发送邮件: {subject}")
    if dry_run:
        print("[Dry run] 纯文本内容:\n", plain)
        print("[Dry run] HTML 内容:\n", html)
        return

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = f"ETF Monitor <{EMAIL_USER}>"
    msg['To'] = EMAIL_TO
    msg['Date'] = formatdate(localtime=False)
    msg['Message-ID'] = make_msgid()
    msg.set_content(plain)
    msg.add_alternative(html, subtype='html')

    # 选择 SSL 或 STARTTLS
    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as smtp:
                smtp.login(EMAIL_USER, EMAIL_PASS)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(EMAIL_USER, EMAIL_PASS)
                smtp.send_message(msg)
    except Exception as e:
        print("发送邮件时出错:", e)
        raise

    print(f"邮件已发送至 {EMAIL_TO}。")


def parse_args():
    parser = argparse.ArgumentParser(description="ETF 最大回撤提醒脚本")
    parser.add_argument("--test-drawdown", type=float, help="模拟回撤比例，例如0.32")
    parser.add_argument("--dry-run", action="store_true", help="仅打印提醒内容，不实际发送邮件")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    check_drawdown(test_drawdown=args.test_drawdown, dry_run=args.dry_run)
