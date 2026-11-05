from test_browser_resilience import browser_env
from participant_copy_checks import check_copy


def test_annotated_pages_remain_responsive_and_group_code_choice_works(browser_env):
    client,page,_,base=browser_env
    import barsan_experiment as main
    from urllib.parse import urlparse
    group=client.post('/api/admin/group-links',headers={'X-Admin-Token':main.ADMIN_TOKEN},json={'scope':'test','label':'Synthetic copy check'}).json()
    check_copy(page,base+'/?'+urlparse(group['invitation_url']).query,title_preview=True)
