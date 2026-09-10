"""真实无头 Chrome + 本地拦截页面验证，不连接抖音也不使用真实账号。"""

import json
import os
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from astrbot_plugin_douyin.core.models import PluginError
from astrbot_plugin_douyin.core.settings import Settings
from astrbot_plugin_douyin.douyin.session import BrowserSession
from astrbot_plugin_douyin.series_diagnostics import DiagnosticBuffer
from playwright.async_api import async_playwright

HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<video src="data:video/mp4;base64,AA==" style="width:320px;height:180px"></video>
<button data-e2e="video-player-digg" onclick="like()">点赞</button>
<button data-e2e="video-player-comment">评论</button>
<textarea placeholder="评论"></textarea><button data-e2e="comment-post" onclick="comment()">发布</button>
<button data-e2e="im-entry" onclick="document.querySelector('#inbox').hidden=false">私信</button>
<div id="inbox" hidden><div data-conversation-id="33333">朋友</div>
<div data-conversation-panel="33333"><div data-e2e="im-input" contenteditable="true"></div>
<button data-e2e="im-send" onclick="message()">发送消息</button><div id="messages"></div></div></div>
<script>
fetch('/aweme/v1/web/aweme/detail/?aweme_id=123456');
fetch('/aweme/v1/web/comment/list/?aweme_id=123456&cursor=0');
async function like(){await fetch('/aweme/v1/web/commit/item/digg/',{method:'POST',body:new URLSearchParams({aweme_id:'123456',type:'1'})});}
async function comment(){await fetch('/aweme/v1/web/comment/publish/',{method:'POST',body:new URLSearchParams({aweme_id:'123456',text:document.querySelector('textarea').value})});}
function message(){const el=document.createElement('div');el.dataset.messageId='55555';el.dataset.direction='outgoing';el.dataset.status='sent';el.textContent=document.querySelector('[data-e2e="im-input"]').innerText;document.querySelector('#messages').appendChild(el);}
</script></body></html>"""


@pytest.fixture
async def session(tmp_path):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            channel=os.environ.get("DOUYIN_TEST_BROWSER_CHANNEL"), headless=True
        )
        context = await browser.new_context()
        state = {
            "liked": 0,
            "like_calls": 0,
            "comment_calls": 0,
            "uid": "12345",
            "reject_comment": False,
        }

        async def route_request(route):
            path = urlsplit(route.request.url).path
            if path == "/aweme/v1/web/query/user/":
                data = {"status_code": 0, "user": {"uid": state["uid"]}}
            elif path.endswith("/aweme/detail/"):
                data = {
                    "status_code": 0,
                    "aweme_detail": {
                        "aweme_id": "123456",
                        "desc": "fixture视频",
                        "user_digged": state["liked"],
                        "video": {"duration": 5000, "play_addr": {"url_list": []}},
                        "author": {
                            "uid": "98765",
                            "sec_uid": "SEC_USER",
                            "nickname": "作者",
                        },
                    },
                }
            elif path.endswith("/item/digg/"):
                state["like_calls"] += 1
                state["liked"] = int(parse_qs(route.request.post_data)["type"][0])
                data = {"status_code": 0}
            elif path.endswith("/comment/publish/"):
                state["comment_calls"] += 1
                data = {
                    "status_code": 1 if state["reject_comment"] else 0,
                    "comment": {
                        "cid": "77777",
                        "text": parse_qs(route.request.post_data)["text"][0],
                    },
                }
            elif path.endswith("/comment/list/"):
                data = {
                    "status_code": 0,
                    "comments": [
                        {"cid": "22222", "aweme_id": "123456", "text": "评论正文"}
                    ],
                    "cursor": 20,
                    "has_more": 0,
                }
            elif "/aweme/" in path:
                await route.abort()
                return
            else:
                await route.fulfill(status=200, content_type="text/html", body=HTML)
                return
            await route.fulfill(
                status=200, content_type="application/json", body=json.dumps(data)
            )

        await context.route("**/*", route_request)
        page = await context.new_page()
        settings = Settings(enabled=True, expected_account_ref="user:12345")
        session = BrowserSession(tmp_path, settings, DiagnosticBuffer())
        session._page = page
        page.on("response", session._schedule_capture)
        await page.goto("https://www.douyin.com/video/123456")
        yield session, state
        await session.close()
        await context.close()
        await browser.close()


async def test_browser_like_sets_desired_state_without_toggle(session):
    browser, state = session
    first = await browser.set_like("123456", True)
    second = await browser.set_like("123456", True)
    assert first["status"] == second["status"] == "verified"
    assert state["like_calls"] == 1
    assert second["code"] == "ALREADY_IN_DESIRED_STATE"


async def test_browser_reads_scoped_comment_response(session):
    browser, _ = session
    result = await browser.read_comments("123456")
    assert result["comments"][0]["comment_ref"] == "22222"
    assert result["video_ref"] == "123456"


async def test_comment_submission_not_claimed_publicly_visible(session):
    browser, state = session
    result = await browser.post_comment("123456", "测试评论")
    assert result["status"] == "submitted"
    assert result["visibility_verified"] is False
    assert result["comment_ref"] == "77777" and state["comment_calls"] == 1


async def test_platform_rejection_is_not_success(session):
    browser, state = session
    state["reject_comment"] = True
    assert (await browser.post_comment("123456", "测试评论"))["status"] == "failed"


async def test_switched_browser_account_prevents_click(session):
    browser, state = session
    state["uid"] = "99999"
    with pytest.raises(PluginError) as exc:
        await browser.set_like("123456", True)
    assert exc.value.code == "ACCOUNT_MISMATCH"
    assert state["like_calls"] == 0


async def test_ambiguous_controls_fail_closed(session):
    browser, _ = session
    await browser._page.evaluate(
        "document.body.insertAdjacentHTML('beforeend','<button data-e2e=video-player-digg>另一个点赞</button>')"
    )
    with pytest.raises(PluginError) as exc:
        await browser._unique('[data-e2e="video-player-digg"]')
    assert exc.value.code == "UI_UNSUPPORTED"


async def test_nickname_is_not_a_valid_mention_identity(session):
    browser, state = session
    with pytest.raises(PluginError) as exc:
        await browser.post_comment(
            "123456",
            "测试",
            mentions=[{"target_ref": "user:88888", "nickname": "同名"}],
        )
    assert exc.value.code == "MENTION_IDENTITY_UNAVAILABLE"
    assert state["comment_calls"] == 0


async def test_message_requires_conversation_identity_and_sent_receipt(session):
    browser, _ = session
    result = await browser.send_message("conversation:33333", "测试私信")
    assert result["status"] == "verified"
    assert result["message_ref"] == "55555"


async def test_contact_unresolved_cannot_share(session):
    browser, _ = session
    with pytest.raises(PluginError) as exc:
        await browser.share_video("123456", "user:88888")
    assert exc.value.code == "CONTACT_IDENTITY_UNAVAILABLE"


async def test_local_comment_pagination_does_not_drop_remainder(session):
    browser, _ = session
    await browser._account_identity()
    rows = [{"comment_ref": str(number)} for number in range(7)]
    first = browser._comment_page(
        {"comments": rows, "cursor": "20", "has_more": False}, 3, "123456"
    )
    second = await browser.read_comments("123456", limit=3, cursor=first["cursor"])
    third = await browser.read_comments("123456", limit=3, cursor=second["cursor"])
    combined = first["comments"] + second["comments"] + third["comments"]
    assert [row["comment_ref"] for row in combined] == [str(n) for n in range(7)]
    assert third["cursor"] == "20" and third["has_more"] is False


async def test_http_200_waiting_page_is_not_treated_as_ready(session):
    browser, _ = session
    await browser._page.set_content("<html><body>Please wait...</body></html>")
    with pytest.raises(PluginError) as exc:
        await browser._check_challenge()
    assert exc.value.code == "PAGE_NOT_READY"


@pytest.mark.parametrize(
    "comment,http_status",
    [
        ({}, 200),
        ({"cid": "invalid", "text": "测试评论"}, 200),
        ({"cid": "77777", "text": "其他正文"}, 200),
        ({"cid": "77777", "text": "测试评论", "aweme_id": "999999"}, 200),
        ({"cid": "77777", "text": "测试评论"}, 500),
        ({"cid": "77777"}, 200),
    ],
)
async def test_comment_incomplete_or_mismatched_ack_stays_unknown(
    session, comment, http_status
):
    browser, _ = session

    async def publish(route):
        await route.fulfill(
            status=http_status, json={"status_code": 0, "comment": comment}
        )

    await browser._page.context.route("**/comment/publish/", publish)
    result = await browser.post_comment("123456", "测试评论")
    assert result["status"] == "unknown_result"
    assert result["code"] == "COMMENT_RECEIPT_UNVERIFIED"


@pytest.fixture
async def share_session(session):
    browser, _ = session
    browser._load_video = AsyncMock()
    browser._contacts["user:88888"] = {"uid": "88888"}
    await browser._page.set_content("""<button data-e2e="video-player-share">分享</button>
    <div role="dialog"><button data-uid="88888" onclick="this.setAttribute('aria-selected','true')">目标</button>
    <div data-aweme-id="123456">卡片</div><button id="send" onclick="sendCard()">发送</button></div>
    <div id="old" data-share-receipt data-receipt-id="old-receipt" data-aweme-id="123456" data-target-uid="88888" data-status="sent">旧回执</div>
    <script>function sendCard(){document.querySelector('#old').hidden=false;}</script>""")
    return browser


@pytest.mark.parametrize("hidden", [False, True])
async def test_share_cannot_verify_an_old_receipt_even_if_it_becomes_visible(
    share_session, hidden
):
    browser = share_session
    await browser._page.locator("#old").evaluate(
        "(el,hidden)=>el.hidden=hidden", hidden
    )
    result = await browser.share_video("123456", "user:88888")
    assert result["status"] == "unknown_result"
    assert result["code"] == "SHARE_RECEIPT_MISSING"


async def test_share_requires_fresh_id_with_matching_video_and_target(share_session):
    browser = share_session
    await browser._page.evaluate(
        """() => {window.sendCard=()=>{const row=document.querySelector('#old').cloneNode(true);row.id='new';row.dataset.receiptId='new-receipt';document.body.appendChild(row);};} """
    )
    result = await browser.share_video("123456", "user:88888")
    assert result["status"] == "verified", result
    assert result["message_ref"] == "new-receipt"


async def test_share_new_dom_without_stable_id_cannot_verify(share_session):
    browser = share_session
    await browser._page.evaluate(
        """() => {window.sendCard=()=>{const row=document.querySelector('#old').cloneNode(true);row.id='new';delete row.dataset.receiptId;document.body.appendChild(row);};} """
    )
    assert (await browser.share_video("123456", "user:88888"))[
        "status"
    ] == "unknown_result"


async def test_status_uses_main_account_while_login_popup_is_controlled(session):
    browser, _ = session
    await browser.remote_frame()
    main = browser._page
    popup = await main.context.new_page()
    await popup.goto("https://sso.douyin.com/login")
    await browser._remote_adopt_popup(popup)
    assert (await browser.status())["account_ref"] == "user:12345"
    assert browser._page is popup and not main.is_closed()
