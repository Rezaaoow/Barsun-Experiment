"""Read-only live Staging copy/layout check; never submit consent or start a task."""
import sys,json
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from participant_copy_checks import check_copy
url=sys.argv[1];commit=sys.argv[2]
assert urlparse(url).scheme=='https' and urlparse(url).hostname=='staging.my-experiment.ir'
assert urlparse(url).path.rstrip('/')=='/lifetrade'
assert len(commit)==40 and all(ch in '0123456789abcdef' for ch in commit)
out=Path(__file__).resolve().parents[1]/'artifacts/participant-copy'
results=[]
with sync_playwright() as pw:
    for engine in ['chromium','webkit']:
        browser=getattr(pw,engine).launch(headless=True,**({'executable_path':'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'} if engine=='chromium' else {}))
        context=browser.new_context()
        try:
            result=check_copy(context.new_page(),url,artifacts=out/engine)
            results.append(dict(result,engine=engine,commit=commit,live_staging=True))
        except Exception as error:
            results.append(dict(passed=False,engine=engine,error_type=type(error).__name__))
        finally:
            context.close();browser.close()
out.mkdir(parents=True,exist_ok=True)
(out/'live-verification.json').write_text(json.dumps(results,indent=2))
print(json.dumps(results))
assert all(result['passed'] for result in results)
