from datetime import date

from app.dtek.models import Outage, OutageKind
from app.engine.events import OutageChange, OutageEvent, ScheduleChange, ScheduleEvent
from app.notify.messages import next_outage_text, render_outage, render_power, render_schedule, scheduled_end
from app.power.state import PowerStatus, PowerTransition
from tests.conftest import day, emergency, kyiv, one, slots, snapshot

OCT1, OCT2 = date(2026, 10, 1), date(2026, 10, 2)


def test_tomorrow_published_lists_periods():
    tomorrow = one(OCT2, [8, 9, 20])
    text = render_schedule(ScheduleEvent(OCT2, ScheduleChange.PUBLISHED, tomorrow, None),
                           snapshot([one(OCT1), tomorrow]), kyiv(2026, 10, 1, 20), "uk")
    assert text == "📅 <b>Графік на завтра опубліковано</b> (02.10)\n\n🔴 08:00 – 10:00\n🔴 20:00 – 21:00"


def test_today_update_shows_only_what_is_left():
    today = one(OCT1, [9, 17, 18])
    text = render_schedule(ScheduleEvent(OCT1, ScheduleChange.CHANGED, today, None),
                           snapshot([today]), kyiv(2026, 10, 1, 12), "en")
    assert text == "📅 <b>Today's schedule updated</b> (01.10)\n\n🔴 17:00 – 19:00"


def test_today_cleared():
    text = render_schedule(ScheduleEvent(OCT1, ScheduleChange.CLEARED, one(OCT1), one(OCT1, [16])),
                           snapshot([one(OCT1)]), kyiv(2026, 10, 1, 6, 51), "en")
    assert text == "📅 <b>Today's schedule updated</b> (01.10)\n\n✅ No more outages today"


def test_tomorrow_withdrawn():
    text = render_schedule(ScheduleEvent(OCT2, ScheduleChange.CLEARED, None, one(OCT2, [8])),
                           snapshot([one(OCT1)]), kyiv(2026, 10, 1, 21), "uk")
    assert text == "📅 <b>Відключення на завтра скасовано</b> (02.10)\n\n✅ Відключень не заплановано"


def test_two_lines_are_listed_separately():
    tomorrow = day(OCT2, ("GPV3.1", slots([8])), ("GPV2.2", slots()))
    text = render_schedule(ScheduleEvent(OCT2, ScheduleChange.PUBLISHED, tomorrow, None),
                           snapshot([tomorrow]), kyiv(2026, 10, 1, 20), "uk")
    assert text == ("📅 <b>Графік на завтра опубліковано</b> (02.10)\n\n"
                    "<b>Лінія 1</b> · Черга 3.1\n🔴 08:00 – 09:00\n\n"
                    "<b>Лінія 2</b> · Черга 2.2\n✅ Відключень не заплановано")


def test_period_running_past_midnight():
    today, tomorrow = one(OCT1, [22, 23]), one(OCT2, [0, 1])
    text = render_schedule(ScheduleEvent(OCT1, ScheduleChange.CHANGED, today, None),
                           snapshot([today, tomorrow]), kyiv(2026, 10, 1, 20), "en")
    assert "🔴 22:00 – 02:00 (tomorrow)" in text


def test_emergency_started_escapes_dtek_text():
    outage = Outage(OutageKind.EMERGENCY, "Ремонт <ПЛ> & *ТП_1*", "11:04 01.10.2026", "15:04 01.10.2026")
    text = render_outage(OutageEvent(OutageChange.STARTED, outage, None), snapshot(), kyiv(2026, 10, 1, 11, 5), "en")
    assert text == ("🚨 <b>Emergency outage</b>\n\n"
                    "Reason: Ремонт &lt;ПЛ&gt; &amp; *ТП_1*\n"
                    "Since: 11:04\n"
                    "Restoration: <b>until 15:04</b>")


def test_restoration_moved_to_tomorrow():
    text = render_outage(
        OutageEvent(OutageChange.RESTORATION_CHANGED, emergency(end="02:00 02.10.2026"), emergency()),
        snapshot(), kyiv(2026, 10, 1, 14), "uk",
    )
    assert text.startswith("🕐 <b>Змінився час відновлення</b>\n\n15:04 → <b>до завтра 02:00</b>")


def test_suspended_schedules_warning():
    text = render_outage(OutageEvent(OutageChange.STARTED, emergency(suspended=True), None),
                         snapshot(), kyiv(2026, 10, 1, 11, 5), "uk")
    assert text.endswith("⚠️ Графіки погодинних відключень зараз не діють, час відновлення може змінюватися.")


def test_outage_ended_mentions_next_scheduled_outage():
    text = render_outage(OutageEvent(OutageChange.ENDED, None, emergency()),
                         snapshot([one(OCT1, [16, 17, 18])]), kyiv(2026, 10, 1, 12), "en")
    assert text == "✅ <b>DTEK no longer reports an outage at the address</b>\n\n📅 Next outage: <b>16:00 – 19:00</b>"


def test_next_outage_falls_back_to_tomorrow():
    snap = snapshot([one(OCT1, [9]), one(OCT2, [6, 7])])
    assert next_outage_text(snap, kyiv(2026, 10, 1, 12), "en") == "tomorrow 06:00 – 08:00"
    assert next_outage_text(snapshot([one(OCT1)]), kyiv(2026, 10, 1, 12), "en") is None


def test_power_outage_within_schedule():
    snap = snapshot([one(OCT1, [16, 17, 18])])
    transition = PowerTransition(PowerStatus.OFF, kyiv(2026, 10, 1, 16, 2), kyiv(2026, 9, 29, 2, 50), "100%", False)
    text = render_power(transition, snap, kyiv(2026, 10, 1, 16, 2), "en")
    assert text == ("🔴 <b>POWER OUTAGE</b>\n\n⏱ Uptime: <b>2d 13h 12m</b>\n🔋 Battery: <b>100%</b>\n\n"
                    "⚡ Scheduled\n🕐 Restoration: <b>19:00</b>")


def test_power_outage_outside_schedule_with_dtek_notice():
    snap = snapshot([one(OCT1, [16])], outage=emergency())
    transition = PowerTransition(PowerStatus.OFF, kyiv(2026, 10, 1, 11, 3), kyiv(2026, 10, 1, 9), "98%", True)
    text = render_power(transition, snap, kyiv(2026, 10, 1, 11, 10), "en")
    assert "⚠️ Unscheduled outage\n🚨 DTEK: Аварійні ремонтні роботи, until 15:04" in text
    assert text.endswith("ℹ️ Noticed after a reconnect, power went off at 11:03")


def test_power_restored():
    snap = snapshot([one(OCT1, [9, 20])])
    transition = PowerTransition(PowerStatus.ON, kyiv(2026, 10, 1, 10, 54), kyiv(2026, 10, 1, 9), "87%", False)
    assert render_power(transition, snap, kyiv(2026, 10, 1, 10, 54), "en") == (
        "🟢 <b>POWER RESTORED</b>\n\n⏱ Outage duration: <b>1h 54m</b>\n🔋 Battery: <b>87%</b>\n\n"
        "📅 Next outage: <b>20:00 – 21:00</b>"
    )


def test_scheduled_end_across_midnight():
    snap = snapshot([one(OCT1, [23]), one(OCT2, [0])])
    assert scheduled_end(snap, kyiv(2026, 10, 1, 23, 10), "en") == "tomorrow 01:00"
