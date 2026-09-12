"""Offline browser contract for field replacement and rich-text roundtrip.

Synthetic controls reproduce input rollback and paragraph placeholder movement;
they are not a recording of the platform DOM. Never visits a platform.
"""
import unittest
import os
from datetime import datetime
from patchright.async_api import async_playwright
from uploader.douyin_uploader.main import DouYinVideo
from uploader.tencent_uploader.main import TencentVideo
from uploader.xiaohongshu_uploader.main import XiaoHongShuVideo


class PublishEditorRoundtripTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(
            headless=True, channel=os.environ.get("POPCORN_TEST_BROWSER_CHANNEL", "chrome")
        )
        self.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()
        self.page.set_default_timeout(5000)
        await self.page.route("**/*", lambda route: route.abort())

    async def test_douyin_replaces_existing_date_and_commits_minutes(self):
        await self.page.set_content('''<div class="radio-test">定时发布</div>
        <input class="semi-input" placeholder="日期和时间" value="2026-09-12 22:05">
        <script>const input=document.querySelector('input');
        input.onkeydown=e=>{if(e.key==='Enter'){
          if(!/^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}$/.test(input.value))input.value='2026-09-12 22:05';
          document.body.dataset.committed=input.value;
        }};</script>''')
        app = DouYinVideo("标题", "/tmp/video.mp4", [], 0, "/tmp/cookie.json")
        await app.set_schedule_time_douyin(self.page, datetime(2026, 9, 13, 18, 30))
        self.assertEqual(await self.page.locator('body').get_attribute('data-committed'), "2026-09-13 18:30")

    async def test_tencent_replaces_default_hour_and_commits_minutes(self):
        await self.page.set_content('''<label>定时</label><label>定时发布</label>
        <input placeholder="请选择发表时间" value="2026-09-13 23:00">
        <span class="weui-desktop-picker__panel__label">09月</span>
        <table class="weui-desktop-picker__table"><tr><td><a>13</a></td></tr></table>
        <input placeholder="请选择时间" value="23:00"><div class="input-editor">描述</div>
        <script>const inputs=document.querySelectorAll('input');
        inputs[1].onkeydown=e=>{if(e.key==='Enter' && /^\\d{2}:\\d{2}$/.test(inputs[1].value)){
          inputs[0].value='2026-09-13 '+inputs[1].value;
        }};</script>''')
        app = TencentVideo("标题", "/tmp/video.mp4", [], 0, "/tmp/cookie.json")
        await app.set_schedule_time_tencent(self.page, datetime(2026, 9, 13, 18, 30))
        self.assertEqual(await self.page.locator('input').first.input_value(), "2026-09-13 18:30")

    async def test_xhs_reads_whole_body_when_placeholder_moves_to_empty_paragraph(self):
        await self.page.set_content('''<div class="tiptap ProseMirror" contenteditable="true">
        <p data-placeholder="输入正文描述"><br></p></div>
        <script>const editor=document.querySelector('[contenteditable]');
        editor.oninput=()=>{for(const p of editor.querySelectorAll('p')){
          if(p.textContent.trim())p.removeAttribute('data-placeholder');
          else p.setAttribute('data-placeholder','输入正文描述');
        }};</script>''')
        app = XiaoHongShuVideo("标题", "/tmp/video.mp4", [], 0, "/tmp/cookie.json", desc="001\n第二段")
        await app.fill_desc(self.page)
        text = await self.page.locator('[contenteditable]').inner_text()
        self.assertIn("001", text)
        self.assertIn("第二段", text)

    async def test_xhs_tag_readback_includes_all_paragraphs(self):
        await self.page.set_content('''<div contenteditable="true"><p>001</p>
        <p data-placeholder="输入正文描述"><br></p></div>
        <div id="creator-editor-topic-container"><div class="item"
        onmousedown="event.preventDefault()" onclick="document.execCommand('insertParagraph');
        document.querySelectorAll('p').forEach(p=>p.removeAttribute('data-placeholder'));
        document.querySelector('[contenteditable]').lastElementChild.setAttribute('data-placeholder','输入正文描述');">成本</div>
        <div class="item" onmousedown="event.preventDefault()" onclick="document.execCommand('insertParagraph');
        document.querySelectorAll('p').forEach(p=>p.removeAttribute('data-placeholder'));
        document.querySelector('[contenteditable]').lastElementChild.setAttribute('data-placeholder','输入正文描述');">手作</div></div>''')
        await self.page.locator('p[data-placeholder]').click()
        app = XiaoHongShuVideo("标题", "/tmp/video.mp4", ["成本", "手作"], 0, "/tmp/cookie.json", desc="001")
        await app.fill_tags(self.page)
        text = await self.page.locator('[contenteditable]').inner_text()
        self.assertIn("#成本", text)
        self.assertIn("#手作", text)

    async def test_xhs_clicks_the_exact_topic_candidate_instead_of_the_first_item(self):
        await self.page.set_content('''<div contenteditable="true"><p>001</p></div>
        <div id="creator-editor-topic-container">
          <div class="item" onclick="document.body.dataset.clicked=this.textContent">成本分析</div>
          <div class="item" onclick="document.body.dataset.clicked=this.textContent">成本</div>
        </div>''')
        await self.page.locator('[contenteditable]').click()
        app = XiaoHongShuVideo("标题", "/tmp/video.mp4", ["成本"], 0, "/tmp/cookie.json", desc="001")
        await app.fill_tags(self.page)
        self.assertEqual(await self.page.locator('body').get_attribute('data-clicked'), "成本")

    async def test_xhs_surfaces_platform_topic_rejection_before_candidate_timeout(self):
        await self.page.set_content('''<div contenteditable="true"><p>001</p></div>
        <div id="creator-editor-topic-container"></div>
        <script>document.querySelector('[contenteditable]').addEventListener('input', event => {
          if (event.currentTarget.innerText.includes('📒')) {
            const toast=document.createElement('div');
            toast.textContent='话题内不允许包含特殊符号';
            document.body.appendChild(toast);
          }
        });</script>''')
        await self.page.locator('[contenteditable]').click()
        app = XiaoHongShuVideo("标题", "/tmp/video.mp4", ["真实账本📒"], 0, "/tmp/cookie.json", desc="001")
        with self.assertRaisesRegex(RuntimeError, "话题内不允许包含特殊符号"):
            await app.fill_tags(self.page)

    async def test_xhs_still_blocks_when_editor_changes_description(self):
        await self.page.set_content('''<div contenteditable="true" oninput="this.textContent='被修改的正文'"><p><br></p></div>''')
        app = XiaoHongShuVideo("标题", "/tmp/video.mp4", [], 0, "/tmp/cookie.json", desc="001")
        with self.assertRaisesRegex(RuntimeError, "正文描述写入校验失败"):
            await app.fill_desc(self.page)


if __name__ == "__main__":
    unittest.main()
