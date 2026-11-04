from test_browser_resilience import browser_env
from participant_copy_checks import check_copy


def test_annotated_pages_remain_responsive_and_group_code_choice_works(browser_env):
    _,page,_,base=browser_env
    check_copy(page,base+'/?group=synthetic-ui-preview',title_preview=True)
