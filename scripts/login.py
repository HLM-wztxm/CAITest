"""Find login in any Character.AI tab of this isolated browser profile."""
import asyncio
from urllib.parse import urlsplit
from common import Failure

TOKEN = r"""() => {
  if (location.origin !== 'https://character.ai') return null;
  try {
    const t = JSON.parse(document.getElementById('__NEXT_DATA__')?.textContent).props.pageProps.token;
    return typeof t === 'string' && !!t && !/[\r\n]/.test(t) ? t : null;
  } catch { return null; }
}"""


async def wait_for_login(context, seconds):
    from playwright.async_api import Error
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        pages = [page for page in context.pages if not page.is_closed()]
        if not pages:
            raise Failure('LOGIN_WINDOW_CLOSED')
        for page in reversed(pages):
            url = urlsplit(page.url)
            if url.scheme != 'https' or url.netloc != 'character.ai':
                continue
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                token = await asyncio.wait_for(page.evaluate(TOKEN), timeout=min(2, remaining))
                if token:
                    return page, token
            except (Error, TimeoutError):
                pass
        await asyncio.sleep(min(.5, max(0, deadline - asyncio.get_running_loop().time())))
    raise Failure('LOGIN_TIMEOUT_OR_PAGE_SCHEMA_CHANGED')
