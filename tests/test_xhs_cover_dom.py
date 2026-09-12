"""Offline Chromium regression for the observed XHS hover-only cover editor.

Requires the Runtime's Patchright Chromium. No platform access or credentials.
"""
import tempfile
import unittest
from pathlib import Path

from patchright.async_api import async_playwright
from uploader.xiaohongshu_uploader.main import XiaoHongShuVideo


# Observed 2026-09-12: outer card follows video orientation; its hidden
# operator is always `default column center`, even for a landscape video.
PAGE = '''<style>
.cover {display:flex;width:632px;height:151px}
.cover > .default {width:180px;height:135px;position:relative;background:#ddd}
.operator {display:none;position:absolute;inset:0}
.cover > .default:hover > .operator {display:block}
.cover-edit-entry {position:absolute;bottom:12px;left:5px;width:102px;height:34px}
.d-modal {position:fixed;inset:0;background:white}
</style><div class="cover-plugin-preview"><span>设置封面</span>
<div class="cover cover--ORIENTATION"><div class="default ORIENTATION">
<div class="operator default column center"><div class="cover-edit-stack">
<div class="cover-edit-entry" onclick="openEditor()">
<span class="cover-edit-entry-text">编辑封面</span></div></div></div></div>
<div class="recommendations">智能推荐封面<button>应用</button></div></div></div>
<script>
const steps={push: step => document.body.dataset.steps=(document.body.dataset.steps||'')+step+','};
function openEditor() {
 if(document.querySelector('.d-modal')) return;
 steps.push('editor');
 document.body.insertAdjacentHTML('beforeend', `<div class="d-modal cover-modal">
 <div class="category"><div class="item" onclick="crop()"><span>裁剪</span></div></div>
 <div id="ratios"></div>
 <input class="upload-input" type="file" accept="image/*"
 onchange="steps.push('upload:'+this.files[0].name)">
 <button onclick="steps.push('confirm');this.parentElement.remove()">完成</button></div>`);
}
function crop(){steps.push('crop');document.querySelector('#ratios').innerHTML=
 `<button onclick="steps.push('3:4')">3:4</button><button>4:3</button>`;}
</script>'''


class CoverDomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        # 本地源码测试复用已安装 Chrome；候选 Runtime 仍使用其自带 Chromium。
        self.browser = await self.playwright.chromium.launch(headless=True, channel="chrome")
        self.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()
        await self.page.route('**/*', lambda route: route.abort())
        self.files = tempfile.TemporaryDirectory()
        self.addCleanup(self.files.cleanup)
        self.cover = Path(self.files.name) / 'cover-portrait.png'
        self.cover.write_bytes(b'offline file input fixture')

    async def assert_portrait_cover_applied(self, orientation):
        await self.page.set_content(PAGE.replace('ORIENTATION', orientation))
        app = XiaoHongShuVideo('debug', 'unused.mp4', [], 0, 'unused.json')
        await app.set_thumbnail(self.page, str(self.cover))
        self.assertEqual(
            await self.page.locator('body').get_attribute('data-steps'),
            'editor,crop,3:4,upload:cover-portrait.png,confirm,',
        )
        self.assertEqual(await self.page.locator('.d-modal').count(), 0)

    async def test_landscape_video_opens_outer_card_and_applies_portrait_cover(self):
        await self.assert_portrait_cover_applied('row')

    async def test_portrait_video_still_applies_portrait_cover(self):
        await self.assert_portrait_cover_applied('column')
