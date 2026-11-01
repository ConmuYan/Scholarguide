"""Offline browser sweep of the six language controls and shared responsive shell."""
import os
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_experience_upgrade as harness
from selenium.webdriver.common.by import By

class I18nWorkspaceBrowserTests(unittest.TestCase):
    setUpClass=classmethod(harness.ExperienceUpgradeTests.setUpClass.__func__)
    tearDownClass=classmethod(harness.ExperienceUpgradeTests.tearDownClass.__func__)
    setUp=harness.ExperienceUpgradeTests.setUp
    tearDown=harness.ExperienceUpgradeTests.tearDown

    def test_six_pages_have_cycle_button_and_fit_mobile_and_desktop(self):
        for page in ('/','/search','/chat','/activity','/sources','/settings'):
            self.driver.get(self.base+page)
            self.wait.until(lambda d:d.find_elements(By.ID,'sg-language-toggle'))
            button=self.driver.find_element(By.ID,'sg-language-toggle')
            self.assertEqual(button.tag_name,'button')
            self.assertTrue(self.driver.execute_script("return document.querySelector('#theme-toggle').nextElementSibling===document.querySelector('#sg-language-toggle')"))
            for lang in ('zh-CN','zh-Hant','en'):
                self.driver.execute_script('SGI18n.set(arguments[0])',lang)
                self.assertEqual(self.driver.execute_script('return document.documentElement.lang'),lang)
                for width in (1440,390,320):
                    self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',{'width':width,'height':950,'deviceScaleFactor':1,'mobile':False})
                    for theme in ('light','dark'):
                        self.driver.execute_script('document.documentElement.dataset.theme=arguments[0]',theme)
                        self.assertLessEqual(self.driver.execute_script('return document.documentElement.scrollWidth-document.documentElement.clientWidth'),1,(page,lang,width,theme))
                        bounds=self.driver.execute_script('const r=document.querySelector("#sg-language-toggle").getBoundingClientRect(); return [r.left,r.right,innerWidth]')
                        self.assertGreaterEqual(bounds[0],0,(page,lang,width,bounds))
                        self.assertLessEqual(bounds[1],bounds[2],(page,lang,width,bounds))
                        output=os.environ.get('SG_I18N_SHOTS')
                        if output and lang=='en' and width in (1440,390):
                            dest=Path(output);dest.mkdir(parents=True,exist_ok=True)
                            selector='.lp-header' if page=='/' else '.topbar'
                            self.driver.find_element(By.CSS_SELECTOR,selector).screenshot(str(dest/f'{page.strip("/") or "landing"}-header-{width}-{theme}.png'))
            self.driver.execute_script('SGI18n.set("zh-CN")')
            button.click()
            self.assertEqual(self.driver.execute_script('return SGI18n.get()'),'zh-Hant')
            button.click()
            self.assertEqual(self.driver.execute_script('return SGI18n.get()'),'en')
            button.click()
            self.assertEqual(self.driver.execute_script('return SGI18n.get()'),'zh-CN')

    def test_language_switch_keeps_request_and_research_content_unchanged(self):
        from demo import demo_run
        d=self.driver
        d.get(self.base+'/search')
        self.wait.until(lambda x:x.execute_script('return !!window.SGI18n && state.configLoaded && !state.configLoading'))
        fixture=demo_run('satisfied','A')
        fixture['papers'][0]['title']='研究原文 / Original title'
        fixture['papers'][0]['reason']='模型原文 / Original reasoning'
        fixture['papers'][0]['evidence_quote']='原始引用 / Original quote'
        for index, paper in enumerate(fixture['papers']): paper['sources']=['arxiv' if index==0 else 'openalex']
        d.execute_script('''
          state.mode='live'; state.natural=false;
          document.querySelector('#topic').value='Original English input with 中文';
          document.querySelector('#category').value='evaluation'; document.querySelector('#minimum').value='2';
          document.querySelector('#reading-goal').value='comparison';
          window.originalPayload=JSON.stringify(inputPayload());
          renderResult(arguments[0]);
          window.originalCard=document.querySelector('.paper');
          window.translationFetches=0;window.savedFetch=window.fetch;
          window.fetch=(...args)=>{window.translationFetches++;return savedFetch(...args)};
        ''',fixture)
        try:
            for lang in ('zh-CN','zh-Hant','en'):
                d.execute_script('SGI18n.set(arguments[0])',lang)
                self.assertTrue(d.execute_script('return JSON.stringify(inputPayload())===originalPayload'))
                self.assertTrue(d.execute_script('return originalCard===document.querySelector(".paper")'))
                self.assertIn('研究原文 / Original title',d.find_element(By.CSS_SELECTOR,'.paper h3').text)
                self.assertIn('模型原文 / Original reasoning',d.find_element(By.CSS_SELECTOR,'.paper-reason').text)
                self.assertIn('原始引用 / Original quote',d.find_element(By.CSS_SELECTOR,'.evidence-block blockquote').text)
                self.assertFalse(d.find_elements(By.CSS_SELECTOR,'.paper:first-child .evidence-missing'))
                self.assertTrue(d.execute_script('return document.querySelector("#source-filter [data-source=all]").textContent === SGI18n.t("全部来源 ({0})", {0:state.lastResult.papers.length})'))
                self.assertTrue(d.execute_script('return document.querySelector("#coverage-status").textContent.startsWith(SGI18n.t("已交付 {0}/{1}",{0:state.lastResult.papers.length,1:state.lastResult.request.target_count||5}))'))
            self.assertEqual(d.execute_script('return translationFetches'),0)
        finally:d.execute_script('window.fetch=savedFetch')

    def test_landing_copy_is_concise_and_still_labels_illustration(self):
        self.driver.get(self.base+'/')
        self.wait.until(lambda d:d.find_elements(By.ID,'sg-language-toggle'))
        self.driver.execute_script('SGI18n.set("zh-CN")')
        self.assertEqual(' '.join(self.driver.find_element(By.ID,'preview-caption').text.split()),'FIG. 01 研究流程演示 · 虚拟样例')
        self.assertFalse(self.driver.find_elements(By.CSS_SELECTOR,'.lp-hero-note'))
        self.assertFalse(self.driver.find_elements(By.CSS_SELECTOR,'.sg-boundary-note'))
        self.assertIn('每一份推荐',self.driver.find_element(By.ID,'boundaries-title').text)
        self.assertLess(len(self.driver.find_element(By.CSS_SELECTOR,'.lp-source-intro').text),40)
        self.driver.execute_script('SGI18n.set("en")')
        self.assertIn('Illustrative examples',self.driver.find_element(By.ID,'preview-caption').text)
        self.assertIn('Choose sources',self.driver.find_element(By.CSS_SELECTOR,'.lp-source-intro').text)
        output=os.environ.get('SG_I18N_SHOTS')
        if output:
            self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',{'width':1440,'height':1100,'deviceScaleFactor':1,'mobile':False})
            self.driver.save_screenshot(str(Path(output)/'landing-en.png'))

if __name__=='__main__':unittest.main()
