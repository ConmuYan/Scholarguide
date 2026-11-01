"""Workspace navigation redesign: isolated service, no user records or live APIs."""
import os
from pathlib import Path
import sys
import unittest
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import test_experience_upgrade as harness
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

class SidebarRedesignTests(unittest.TestCase):
    setUpClass=classmethod(harness.ExperienceUpgradeTests.setUpClass.__func__)
    tearDownClass=classmethod(harness.ExperienceUpgradeTests.tearDownClass.__func__)
    setUp=harness.ExperienceUpgradeTests.setUp
    tearDown=harness.ExperienceUpgradeTests.tearDown

    def test_pages_use_distinct_navigation_and_remove_duplicate_module_tag(self):
        for page in ('search','chat','activity','sources','settings'):
            self.driver.get(self.base+'/'+page)
            self.wait.until(lambda d:d.find_elements(By.ID,'sg-language-toggle'))
            self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR,'.topbar .crumb'),[])
            self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR,'.sidebar .dot, .sidebar .account'),[])
            self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR,'.sidebar .side-link')),5)
            self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR,'.side-primary > .side-link')),3)
            self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR,'.side-tools > .side-link')),2)
            active=self.driver.find_element(By.CSS_SELECTOR,'.side-link.active')
            self.assertEqual(active.get_attribute('aria-current'),'page')
            self.assertEqual(urlsplit(active.get_attribute('href')).path,'/'+page)
            self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR,'.brand-mark svg')),1)
            self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR,'.side-link .nav-icon svg')),5)

    def test_navigation_links_work_with_native_clicks(self):
        self.driver.get(self.base+'/search')
        self.wait.until(lambda d:d.find_elements(By.ID,'sg-language-toggle'))
        for page in ('chat','activity','sources','settings','search'):
            self.driver.find_element(By.CSS_SELECTOR,f'.side-link[href="/{page}"]').click()
            self.wait.until(lambda d:urlsplit(d.current_url).path=='/'+page and d.find_elements(By.ID,'sg-language-toggle'))
            self.assertEqual(self.driver.find_element(By.CSS_SELECTOR,'.side-link.active').get_attribute('aria-current'),'page')

    def test_sidebar_layout_locales_themes_and_mobile_keyboard(self):
        for page in ('search','chat','activity','sources','settings'):
            self.driver.get(self.base+'/'+page)
            self.wait.until(lambda d:d.find_elements(By.ID,'sg-language-toggle'))
            for lang in ('zh-CN','zh-Hant','en'):
                self.driver.execute_script('SGI18n.set(arguments[0])',lang)
                for width in (1440,1024,390,320):
                    self.driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',{'width':width,'height':900,'deviceScaleFactor':1,'mobile':False})
                    self.wait.until(lambda d:d.execute_script('return innerWidth')==width)
                    mobile=width<=920
                    if mobile:
                        self.wait.until(lambda d:d.find_element(By.ID,'sidebar').get_attribute('inert') is not None)
                        self.driver.find_element(By.ID,'menu-toggle').click()
                        self.wait.until(lambda d:d.find_element(By.ID,'menu-toggle').get_attribute('aria-expanded')=='true')
                        self.wait.until(lambda d:d.execute_script('return document.querySelector("#sidebar").getBoundingClientRect().left >= 11'))
                    for theme in ('light','dark'):
                        self.driver.execute_script('document.documentElement.dataset.theme=arguments[0]',theme)
                        info=self.driver.execute_script('''const side=document.querySelector('#sidebar'),r=side.getBoundingClientRect();
                          return {overflow:document.documentElement.scrollWidth-document.documentElement.clientWidth,
                            ownOverflow:side.scrollWidth-side.clientWidth,left:r.left,right:r.right,client:document.documentElement.clientWidth};''')
                        self.assertLessEqual(info['overflow'],1,(page,lang,width,theme,info))
                        self.assertLessEqual(info['ownOverflow'],1,(page,lang,width,theme,info))
                        self.assertGreaterEqual(info['left'],0,(page,lang,width,theme,info))
                        self.assertLessEqual(info['right'],info['client'],(page,lang,width,theme,info))
                        output=os.environ.get('SG_NAV_SHOTS')
                        if output and width in (1440,390) and lang in ('zh-CN','en'):
                            dest=Path(output);dest.mkdir(parents=True,exist_ok=True)
                            self.driver.find_element(By.ID,'sidebar').screenshot(str(dest/f'{page}-sidebar-{width}-{lang}-{theme}.png'))
                            if page=='search':self.driver.save_screenshot(str(dest/f'search-workspace-{width}-{lang}-{theme}.png'))
                    if mobile:
                        self.driver.switch_to.active_element.send_keys(Keys.ESCAPE)
                        self.wait.until(lambda d:d.find_element(By.ID,'menu-toggle').get_attribute('aria-expanded')=='false')
                        self.assertEqual(self.driver.execute_script('return document.activeElement.id'),'menu-toggle')
                        self.assertIsNotNone(self.driver.find_element(By.ID,'sidebar').get_attribute('inert'))

if __name__=='__main__':unittest.main()
