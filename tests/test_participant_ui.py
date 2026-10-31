from pathlib import Path
import re

HTML_PATH = Path(__file__).resolve().parents[1] / "app" / "index.html"
HTML = HTML_PATH.read_text(encoding="utf-8")


def section_by_id(source: str, element_id: str) -> str:
    match = re.search(
        rf'<section\\b[^>]*id="{re.escape(element_id)}"[^>]*>([\\s\\S]*?)</section>',
        source,
        flags=re.IGNORECASE,
    )
    assert match, f"Missing section #{element_id}"
    return match.group(1)


def function_body(source: str, start: str, end: str) -> str:
    start_at = source.find(start)
    end_at = source.find(end, start_at + len(start))
    assert start_at >= 0 and end_at > start_at, f"Could not isolate {start}"
    return source[start_at:end_at]


def test_welcome_screen_discloses_only_brand_and_continue():
    welcome = section_by_id(HTML, "welcome")
    assert "<h1>بازی باشگاه رانندگان بارسان</h1>" in welcome
    assert 'id="welcome-continue"' in welcome
    for forbidden in ("اولتیماتوم", "Frame", "Control", "مرحله", "ماژول"):
        assert forbidden not in welcome


def test_removed_consent_warning_does_not_return():
    removed = (
        "برای حفظ حریم خصوصی، لطفاً نام، نام خانوادگی، شمارهٔ ملی، "
        "شمارهٔ تلفن یا اطلاعات هویتی دیگری را در پاسخ‌های خود وارد نکنید. "
        "فقط از کد پژوهشی اختصاص‌یافته به خودتان استفاده کنید."
    )
    assert removed not in HTML


def test_role_labels_use_separated_words():
    assert "پیشنهاد دهنده" in HTML
    assert "پاسخ دهنده" in HTML
    assert "پیشنهاددهنده" not in HTML
    assert "پاسخ‌دهنده" not in HTML


def test_participant_game_and_practice_copy():
    assert (
        "شما در این بازی برای کسب امتیاز باشگاه رانندگان با یک بازیکن دیگر بازی می‌کنید."
        in HTML
    )
    assert (
        "قبل از شروع بازی اصلی، دو دور تمرینی اجرا می‌شود اما امتیازها و داده‌های "
        "این دو دور در امتیاز نهایی و برنامه پژوهشی لحاظ نمی‌شوند."
        in HTML
    )
    assert "دور‌های تمرینی به پایان رسید. الان بازی اصلی شروع می‌شود." in HTML


def test_five_round_summary_has_only_requested_metrics_and_no_section_header():
    summary = function_body(
        HTML,
        "function utRenderBlockSummary() {",
        "async function utFinishUltimatum()",
    )
    assert 'utMakeScreen({header:false})' in summary
    assert "در ۵ دور قبلی شما:" in summary
    assert "امتیاز به دست آوردید" in summary
    assert "امتیاز از دست دادید" in summary
    assert "دور بعدی" in summary
    for forbidden in ("پایان این بخش", "پایان این بلوک", "شروع بخش بازی"):
        assert forbidden not in summary


def test_final_score_is_single_titled_and_prominent():
    finish = function_body(
        HTML,
        "async function utFinishUltimatum()",
        "function ut",
    )
    assert finish.count('"امتیاز نهایی"') == 1
    assert '"امتیاز نهایی شما"' not in finish
    assert '"ut-summary-box ut-final-score-box"' in finish
    assert ".ut-card .ut-final-score-box .ut-metric-value" in HTML


def test_participant_copy_and_score_layout_are_centered():
    assert "#screen .panel{text-align:center}" in HTML
    assert ".ut-card,.ut-card *{text-align:center!important}" in HTML
