from pathlib import Path
import re

HTML_PATH = Path(__file__).resolve().parents[1] / "app" / "index.html"
HTML = HTML_PATH.read_text(encoding="utf-8")


def section_by_id(source: str, element_id: str) -> str:
    match = re.search(
        rf'<section\b[^>]*id="{re.escape(element_id)}"[^>]*>([\s\S]*?)</section>',
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


def test_welcome_screen_shows_public_heading_and_ordered_step_list_only():
    welcome = section_by_id(HTML, "welcome")
    assert "<h1>به برنامهٔ پژوهشی بارسان خوش آمدید</h1>" in welcome
    assert '<ol class="welcome-steps" id="welcome-steps"' in welcome
    assert 'id="welcome-continue"' in welcome
    assert 'id="welcome-plan-status"' in welcome
    # The status node is hidden except for a loading/error condition; there is no prose description.
    assert 'class="welcome-note hidden"' in welcome
    assert "بخش‌های برنامهٔ شما در ادامه" not in welcome
    for forbidden in (
        "اولتیماتوم",
        "Ultimatum",
        "Frame",
        "Control",
        "dd_lifetrade",
        "ultimatum_frame",
        "ultimatum_control",
    ):
        assert forbidden not in welcome


def test_public_workflow_labels_hide_internal_module_names_and_descriptions():
    labels = function_body(
        HTML,
        "function participantWorkflowTitle(moduleCode){",
        "async function loadWelcomeWorkflow(){",
    )
    assert "questionnaire:'پرسشنامه'" in labels
    assert "dd_lifetrade:'بازی انتخاب'" in labels
    assert "ultimatum:'بازی باشگاه رانندگان بارسان'" in labels
    assert "ultimatum_frame:'بازی باشگاه رانندگان بارسان'" in labels
    assert "ultimatum_control:'بازی باشگاه رانندگان بارسان'" in labels

    loader = function_body(
        HTML,
        "async function loadWelcomeWorkflow(){",
        "loadWelcomeWorkflow();",
    )
    assert "/api/experiment/workflow?invite=" in loader
    assert "/api/experiment/workflow?group=" in loader
    assert "encodeURIComponent(workflow.invitationToken)" in loader
    assert "encodeURIComponent(workflow.groupInvitationToken)" in loader
    assert "step.description" not in loader
    assert "step.title" not in loader
    assert "if(payload.legacy_default&&!includesUltimatum)" in loader
    assert "steps.push({module_code:'ultimatum'})" in loader


def test_dd_and_life_trade_instruction_title_is_participant_facing():
    intro = function_body(
        HTML,
        "function showInstructionPage(taskKey, pageIndex){",
        "function showFixation(){",
    )
    assert "const title='بازی انتخاب';" in intro
    assert "5-Trial DD Task" not in intro
    assert "Life-Trade Reward" not in intro


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


def test_block_transition_has_no_section_heading():
    intro = function_body(
        HTML,
        "function utRenderBlockIntro() {",
        "async function utStartMainTrial(trial) {",
    )
    for forbidden in ("شروع بخش بازی", "پایان این بخش", "پایان این بلوک"):
        assert forbidden not in intro


def test_final_score_is_single_titled_and_prominent():
    start = HTML.find("async function utFinishUltimatum()")
    assert start >= 0
    finish = HTML[start:]
    assert finish.count('"امتیاز نهایی"') == 1
    assert '"امتیاز نهایی شما"' not in finish
    assert '"ut-summary-box ut-final-score-box"' in finish
    assert ".ut-card .ut-final-score-box .ut-metric-value" in HTML


def test_titles_and_copy_use_centered_vertical_content_groups_in_all_modules():
    show_info = function_body(HTML, "function showInfo(", "function showInstructionPage(")
    questionnaire_intro = function_body(
        HTML,
        "function showQuestionnaireIntro(",
        "async function startQuestionnaireRun(",
    )
    questionnaire_question = function_body(
        HTML,
        "function showQuestionnaireQuestion(",
        "async function completeQuestionnaire(",
    )
    ultimatum_screen = function_body(HTML, "function utMakeScreen(", "function utAddRoleBox(")

    assert "main.className='instruction-main'" in show_info
    assert "main.className='instruction-main'" in questionnaire_intro
    assert "main.className='questionnaire-main'" in questionnaire_question
    assert 'utNode("div","ut-main")' in ultimatum_screen

    assert ".instruction-main{flex:1 1 auto" in HTML
    assert "justify-content:center" in HTML
    assert ".questionnaire-main{flex:1 1 auto" in HTML
    assert ".ut-main{flex:1 1 auto" in HTML
    assert ".screen,.screen.trial-screen{align-items:center!important;justify-content:center!important}" in HTML


def test_participant_copy_and_score_layout_are_centered():
    assert "#screen .panel{text-align:center}" in HTML
    assert ".ut-card,.ut-card .ut-body,.ut-card .ut-header,.ut-card .ut-copy,.ut-card h1,.ut-card h2,.ut-card p" in HTML

def test_questionnaire_answer_persists_custom_workflow_draft_without_forward_draft_request():
    answer = function_body(
        HTML,
        "async function chooseQuestionnaireAnswer(",
        "async function enterWorkflowStep(",
    )
    assert "step_id:workflow.workflowMode?workflow.currentStepId:null" in answer

    navigation = function_body(
        HTML,
        "async function moveQuestionnaire(",
        "function showQuestionnaireQuestion(",
    )
    assert "if(direction<0)await saveQuestionnairePosition(target);" in navigation
    assert navigation.count("saveQuestionnairePosition(target)") == 1

    completion = function_body(
        HTML,
        "async function completeQuestionnaire(){",
        "async function beginParticipantFlow(){",
    )
    assert "saveQuestionnairePosition(" not in completion

